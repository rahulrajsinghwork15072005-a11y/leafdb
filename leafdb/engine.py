import copy
import os
import threading
import time
from collections import OrderedDict

from . import rows as rows_mod
from .btree import BTree, DuplicateKey, Leaf, MAX_VALUE_SIZE
from .catalog import Catalog, CatalogError, TableMeta
from .executor import ExecutionError, Executor, Scope, compile_expr, eval_expr
from .planner import build_select_plan, find_equi_predicates, flatten_and
from .pager import Pager
from .sqlparse import (
    BeginStmt,
    BinOp,
    Column,
    CommitStmt,
    CreateIndexStmt,
    CreateTableStmt,
    DeleteStmt,
    DropIndexStmt,
    DropTableStmt,
    ExplainStmt,
    InsertStmt,
    Literal,
    ParseError,
    RollbackStmt,
    SelectStmt,
    SQLError,
    Star,
    UnaryOp,
    UpdateStmt,
    parse_script,
    parse_statement,
)
from .wal import WAL, recover as _wal_recover


class Result:
    def __init__(self, cols=None, rows=None, message=None, plan=None, elapsed_ms=0.0):
        self.cols = cols or []
        self.rows = rows or []
        self.message = message
        self.plan = plan
        self.elapsed_ms = elapsed_ms


class TransactionError(SQLError):
    pass


class _FastPathFallback(Exception):
    pass


class Database:
    _registry_lock = threading.Lock()
    _sessions = {}
    _writer_locks = {}
    _shared_wal_end = {}

    def __init__(self, path, cache_size=64, statement_cache_size=256):
        self.path = os.path.abspath(path)
        self.wal_path = self.path + ".wal"
        if not os.path.exists(self.path):
            with open(self.path, "wb"):
                pass
        applied = _wal_recover(self.wal_path, self.path)
        self.pager = Pager(self.path, cache_size=cache_size)
        self.wal = WAL(self.wal_path)
        if applied:
            self.wal.reset()
        self.btree = BTree(self.pager)
        self.catalog = Catalog.load(self.pager)
        self.executor = Executor(self)
        self.indexes = {}
        self.in_txn = False
        self._snapshot = None
        self._stmt_cache = OrderedDict()
        self._stmt_cache_size = statement_cache_size
        self._lock = threading.RLock()
        self._open = True
        with Database._registry_lock:
            Database._writer_locks.setdefault(self.path, threading.RLock())
            self._writer_lock = Database._writer_locks[self.path]
            Database._sessions.setdefault(self.path, []).append(self)
        self._wal_pos = self.wal.size()
        self.catalog_version = 0
        self._rebuild_indexes()

    def refresh_from_wal(self, force=False):
        """Adopt batches other sessions committed since our last look (MVCC).

        Never called inside an explicit transaction, so a session's snapshot
        stays stable for its duration; between statements sessions run at
        READ COMMITTED visibility. In-process writers publish the new log end
        to a shared table, so the common nothing-new case costs zero syscalls
        (pass force=True to stat the file for cross-process visibility).
        """
        if not self._open or self.in_txn:
            return
        from .catalog import CATALOG_PAGE
        shared_end = Database._shared_wal_end.get(self.path)
        if shared_end is None:
            with Database._registry_lock:
                shared_end = Database._shared_wal_end.setdefault(
                    self.path, self._wal_pos)
        elif shared_end <= self._wal_pos:
            return
        end = shared_end if force is False else None
        batches = self.wal.read_batches_from(self._wal_pos, end=end)
        for new_pos, images, num_pages in batches:
            for pg, img in images.items():
                self.pager.pool.put(pg, img, dirty=False)
                self.pager.pool.dirty.discard(pg)
            if num_pages > self.pager.num_pages:
                self.pager.num_pages = num_pages
            self._wal_pos = new_pos
            if CATALOG_PAGE in images:
                self.catalog = Catalog.load(self.pager)
                self.indexes = {}
                self._rebuild_indexes()
                self._stmt_cache.clear()
                self.catalog_version += 1

    def bulk_insert(self, table, rows):
        """Fast path: insert many row tuples without SQL parsing per row."""
        with self._lock:
            if not self.in_txn:
                with self._writer_lock:
                    self._begin_locked()
                    try:
                        n = self.executor_insert_bulk(table, rows)
                    except Exception:
                        self._rollback_locked()
                        raise
                    self._commit_locked()
                    return n
            return self.executor_insert_bulk(table, rows)

    def executor_insert_bulk(self, table, rows):
        meta = self.catalog.get(table)
        types = meta.types()
        ncols = len(types)
        pk_i = meta.pk_index()
        colnames = meta.colnames()
        count = 0
        for values in rows:
            full = list(values) if len(values) == ncols else None
            if full is None:
                raise SQLError(f"bulk_insert expects {ncols} values, got {len(values)}")
            if pk_i is not None and full[pk_i] is None:
                full[pk_i] = meta.next_rowid
            rid = meta.next_rowid if pk_i is None else self.coerce_pk(full[pk_i])
            if pk_i is not None:
                full[pk_i] = rid
            coerced = tuple(rows_mod.coerce_value(t, c, v)
                            for t, c, v in zip(types, colnames, full))
            blob = rows_mod.encode_row(types, coerced)
            if len(blob) > MAX_VALUE_SIZE:
                raise SQLError(f"row too large ({len(blob)} bytes)")
            try:
                meta.root_page = self.btree.insert(meta.root_page, rid, blob)
            except DuplicateKey:
                raise SQLError(f"UNIQUE constraint failed: {meta.name}.{pk} ({rid})"
                               .replace("{pk}", str(meta.pk_column() or "rowid")))
            meta.next_rowid = max(meta.next_rowid, rid + 1)
            meta.row_count += 1
            self._index_add(meta, rid, coerced)
            count += 1
        self.catalog.save(self.pager)
        return count

    def lookup_meta(self, name):
        return self.catalog.get(name)

    def close(self, checkpoint=True):
        with self._lock:
            self._close_locked(checkpoint)

    def _close_locked(self, checkpoint):
        if not self._open:
            return
        if self.in_txn:
            self.rollback()
        try:
            if checkpoint:
                others = [s for s in Database._sessions.get(self.path, ())
                          if s is not self and s._open]
                if not others:
                    self.checkpoint()
        finally:
            self._open = False
            with Database._registry_lock:
                sessions = Database._sessions.get(self.path, [])
                if self in sessions:
                    sessions.remove(self)
        self.pager.close()
        self.wal.close()

    def checkpoint(self):
        with self._lock:
            self._checkpoint_locked()

    def _checkpoint_locked(self):
        if self.in_txn:
            raise TransactionError("cannot checkpoint inside a transaction")
        with Database._registry_lock:
            others = [s for s in Database._sessions.get(self.path, ())
                      if s is not self and s._open]
        if others:
            raise TransactionError(
                f"cannot checkpoint: {len(others)} other session(s) may still need the WAL")
        self.pager.flush(fsync=True)
        self.wal.reset()
        self._wal_pos = len(b"LEAFWAL01\n")

    def execute(self, sql):
        with self._lock:
            return self._execute_locked(sql)

    def _execute_locked(self, sql):
        if not self.in_txn:
            self.refresh_from_wal()
        stmt = self._stmt_cache.get(sql)
        if stmt is not None:
            self._stmt_cache.move_to_end(sql)
            return self._run_one(stmt)
        try:
            stmt = parse_statement(sql)
        except SQLError:
            return self.execute_script(sql)[0]
        self._stmt_cache[sql] = stmt
        while len(self._stmt_cache) > self._stmt_cache_size:
            self._stmt_cache.popitem(last=False)
        return self._run_one(stmt)

    def execute_script(self, sql):
        with self._lock:
            if not self.in_txn:
                self.refresh_from_wal()
            stmts = parse_script(sql)
            return [self._run_one(stmt) for stmt in stmts]

    _WRITE_STMTS = None

    def _run_one(self, stmt):
        if isinstance(stmt, BeginStmt):
            self.begin()
            return Result(message="transaction started")
        if isinstance(stmt, CommitStmt):
            self.commit()
            return Result(message="committed")
        if isinstance(stmt, RollbackStmt):
            self.rollback()
            return Result(message="rolled back")
        if self.in_txn:
            if Database._WRITE_STMTS is None:
                Database._WRITE_STMTS = (
                    InsertStmt, UpdateStmt, DeleteStmt, CreateTableStmt,
                    CreateIndexStmt, DropTableStmt, DropIndexStmt,
                )
            if isinstance(stmt, Database._WRITE_STMTS):
                with self._writer_lock:
                    return self._dispatch(stmt)
            return self._dispatch(stmt)
        if isinstance(stmt, SelectStmt):
            return self._dispatch(stmt)
        with self._writer_lock:
            self.begin()
            try:
                result = self._dispatch(stmt)
            except Exception:
                self.rollback()
                raise
            self.commit()
            return result

    def _pk_fast_path(self, sel):
        """Bypass planner+executor for `SELECT items FROM t [AS a] WHERE pk = literal`."""
        if sel.joins or sel.distinct or sel.group_by or sel.having is not None \
                or sel.order_by or sel.limit is not None or sel.offset:
            return None
        where = sel.where
        if not (isinstance(where, BinOp) and where.op == "="):
            return None
        left, right = where.left, where.right
        if isinstance(left, Column) and isinstance(right, Literal):
            col, val = left, right.value
        elif isinstance(left, Literal) and isinstance(right, Column):
            col, val = right, left.value
        else:
            return None
        meta = self.catalog.get(sel.table)
        alias = sel.alias or sel.table
        pk = meta.pk_column()
        if col.table not in (None, alias) or pk is None or col.name != pk:
            return None
        try:
            key = self.coerce_pk(val)
        except SQLError:
            return None
        blob = self.btree.search(meta.root_page, key)
        row = rows_mod.decode_row(meta.types(), blob) if blob is not None else None

        cols = []
        for expr, label in sel.items:
            if isinstance(expr, Star):
                cols.extend(meta.colnames())
            else:
                cols.append(label)

        if row is None:
            return cols, []

        out_row = []

        def literal_of(e):
            if isinstance(e, Literal):
                return e.value
            if isinstance(e, UnaryOp) and e.op == "NEG" and isinstance(e.operand, Literal) \
                    and not isinstance(e.operand.value, bool):
                return -e.operand.value
            raise _FastPathFallback()

        try:
            for expr, _label in sel.items:
                if isinstance(expr, Star):
                    out_row.extend(row)
                elif isinstance(expr, Column):
                    if expr.table not in (None, alias):
                        return None
                    out_row.append(row[meta.column_index(expr.name)])
                else:
                    out_row.append(literal_of(expr))
        except _FastPathFallback:
            return None
        except (CatalogError, ExecutionError):
            return None
        return cols, [tuple(out_row)]

    def _dispatch(self, stmt):
        t0 = time.perf_counter()
        if isinstance(stmt, ExplainStmt):
            inner = stmt.stmt
            if not isinstance(inner, SelectStmt):
                raise SQLError("EXPLAIN supports SELECT statements")
            plan = build_select_plan(inner, self.lookup_meta)
            lines = plan.explain_lines()
            ms = (time.perf_counter() - t0) * 1000.0
            return Result(cols=["plan"], rows=[(line,) for line in lines], plan=lines, elapsed_ms=ms)
        if isinstance(stmt, SelectStmt):
            fast = self._pk_fast_path(stmt)
            if fast is not None:
                ms = (time.perf_counter() - t0) * 1000.0
                return Result(cols=fast[0], rows=fast[1], elapsed_ms=ms)
            plan = build_select_plan(stmt, self.lookup_meta)
            cols, out_rows = self.executor.run_select(stmt, plan)
            ms = (time.perf_counter() - t0) * 1000.0
            return Result(cols=cols, rows=out_rows, plan=plan.explain_lines(), elapsed_ms=ms)
        if isinstance(stmt, CreateTableStmt):
            msg = self._create_table(stmt)
        elif isinstance(stmt, CreateIndexStmt):
            msg = self._create_index(stmt)
        elif isinstance(stmt, DropTableStmt):
            msg = self._drop_table(stmt)
        elif isinstance(stmt, DropIndexStmt):
            msg = self._drop_index(stmt)
        elif isinstance(stmt, InsertStmt):
            msg = self._insert(stmt)
        elif isinstance(stmt, UpdateStmt):
            msg = self._update(stmt)
        elif isinstance(stmt, DeleteStmt):
            msg = self._delete(stmt)
        else:
            raise SQLError(f"unsupported statement {type(stmt).__name__}")
        ms = (time.perf_counter() - t0) * 1000.0
        return Result(message=msg, elapsed_ms=ms)

    def begin(self):
        with self._lock:
            self._begin_locked()

    def _begin_locked(self):
        if self.in_txn:
            raise TransactionError("transaction already active")
        self.pager.begin_txn()
        self._snapshot = None
        self.in_txn = True

    def _ensure_snapshot(self):
        if self._snapshot is None and self.in_txn:
            self._snapshot = {
                "indexes": copy.deepcopy(self.indexes),
                "metas": {name: m.to_dict() for name, m in self.catalog.tables.items()},
            }

    def commit(self):
        with self._lock:
            self._commit_locked()

    def _commit_locked(self):
        if not self.in_txn:
            raise TransactionError("no transaction active")
        with self._writer_lock:
            pages, num_pages, images = self.pager.collect_commit()
            if images or pages:
                self._wal_pos = self.wal.append_batch(images, num_pages)
                with Database._registry_lock:
                    Database._shared_wal_end[self.path] = max(
                        Database._shared_wal_end.get(self.path, 0),
                        self._wal_pos)
        self._snapshot = None
        self.in_txn = False

    def rollback(self):
        with self._lock:
            self._rollback_locked()

    def _rollback_locked(self):
        if not self.in_txn:
            raise TransactionError("no transaction active")
        self.pager.rollback_txn()
        if self._snapshot is not None:
            snap_catalog = Catalog()
            for name, m in self._snapshot["metas"].items():
                snap_catalog.tables[name] = TableMeta.from_dict(m)
            self.catalog = snap_catalog
            self.indexes = self._snapshot["indexes"]
        self._snapshot = None
        self.in_txn = False

    def _create_table(self, stmt):
        self._ensure_snapshot()
        name = stmt.name
        seen = set()
        pks = []
        cols = []
        for c in stmt.columns:
            norm = self.catalog.validate_column_def(c)
            if norm["name"] in seen:
                raise CatalogError(f"duplicate column {norm['name']!r}")
            seen.add(norm["name"])
            if norm["pk"]:
                if norm["type"] != rows_mod.INT:
                    raise CatalogError("PRIMARY KEY column must be INT")
                pks.append(norm["name"])
            cols.append(norm)
        if len(pks) > 1:
            raise CatalogError("only one PRIMARY KEY column is supported")
        meta = TableMeta(name=name, columns=cols, root_page=None)
        root = self.pager.allocate()
        self.pager.write(root, Leaf().to_bytes())
        meta.root_page = root
        self.catalog.add(meta)
        self.catalog.save(self.pager)
        return f"table {name!r} created"

    def _create_index(self, stmt):
        self._ensure_snapshot()
        meta = self.catalog.get(stmt.table)
        if stmt.column not in meta.colnames():
            raise CatalogError(f"table {meta.name!r} has no column {stmt.column!r}")
        if stmt.column == meta.pk_column():
            raise CatalogError("primary key is already indexed")
        if stmt.column in meta.indexes:
            raise CatalogError(f"column {stmt.column!r} is already indexed")
        meta.indexes[stmt.column] = stmt.name
        idx = {}
        ci = meta.column_index(stmt.column)
        for rid, row in self.scan_table(meta):
            v = row[ci]
            if v is not None:
                idx.setdefault(v, set()).add(rid)
        self.indexes[(meta.name, stmt.column)] = idx
        self.catalog.save(self.pager)
        return f"index {stmt.name!r} created on {meta.name}({stmt.column})"

    def _drop_table(self, stmt):
        self._ensure_snapshot()
        meta = self.catalog.get(stmt.name)
        for col in list(meta.indexes):
            self.indexes.pop((meta.name, col), None)
        del self.catalog.tables[meta.name]
        self.catalog.save(self.pager)
        return f"table {meta.name!r} dropped"

    def _drop_index(self, stmt):
        self._ensure_snapshot()
        for meta in self.catalog.tables.values():
            for col, iname in meta.indexes.items():
                if iname == stmt.name:
                    del meta.indexes[col]
                    self.indexes.pop((meta.name, col), None)
                    self.catalog.save(self.pager)
                    return f"index {stmt.name!r} dropped"
        raise CatalogError(f"no such index: {stmt.name!r}")

    def _insert(self, stmt):
        self._ensure_snapshot()
        meta = self.catalog.get(stmt.table)
        types = meta.types()
        ncols = len(types)
        pk_i = meta.pk_index()
        declared = stmt.columns
        if declared is not None:
            if len(set(declared)) != len(declared):
                raise SQLError("duplicate column in INSERT column list")
            for c in declared:
                if c not in meta.colnames():
                    raise CatalogError(f"table {meta.name!r} has no column {c!r}")
            width = len(declared)
        else:
            width = ncols
        count = 0
        for exprs in stmt.rows:
            if len(exprs) != width:
                raise SQLError(
                    f"INSERT expects {width} value(s), got {len(exprs)}"
                )
            values = [self._literal_value(e) for e in exprs]
            full = [None] * ncols
            if declared is not None:
                for c, v in zip(declared, values):
                    full[meta.column_index(c)] = v
            else:
                full = list(values)
            if pk_i is not None and full[pk_i] is None:
                full[pk_i] = meta.next_rowid
            if pk_i is None:
                rid = meta.next_rowid
            else:
                rid = self.coerce_pk(full[pk_i])
                full[pk_i] = rid
            coerced = tuple(
                rows_mod.coerce_value(t, cn, v)
                for t, cn, v in zip(types, meta.colnames(), full)
            )
            if pk_i is not None and coerced[pk_i] is None:
                raise SQLError("primary key cannot be NULL")
            blob = rows_mod.encode_row(types, coerced)
            if len(blob) > MAX_VALUE_SIZE:
                raise SQLError(f"row too large ({len(blob)} bytes)")
            if self.btree.search(meta.root_page, rid) is not None:
                pk_name = meta.pk_column() or "rowid"
                raise SQLError(f"UNIQUE constraint failed: {meta.name}.{pk_name} ({rid})")
            try:
                meta.root_page = self.btree.insert(meta.root_page, rid, blob)
            except DuplicateKey:
                raise SQLError(f"UNIQUE constraint failed: {meta.name}.{meta.pk_column() or 'rowid'} ({rid})")
            meta.next_rowid = max(meta.next_rowid, rid + 1)
            meta.row_count += 1
            self._index_add(meta, rid, coerced)
            count += 1
        self.catalog.save(self.pager)
        return f"{count} row(s) inserted into {meta.name}"

    def _update(self, stmt):
        self._ensure_snapshot()
        meta = self.catalog.get(stmt.table)
        types = meta.types()
        pk_col = meta.pk_column()
        scope = Scope()
        scope.add(meta.name, meta)
        for cname, _expr in stmt.assignments:
            if cname not in meta.colnames():
                raise CatalogError(f"table {meta.name!r} has no column {cname!r}")
            if pk_col and cname == pk_col:
                raise SQLError("cannot UPDATE the primary key")
        matched = list(self._matching_rows(meta, stmt.where))
        assign_fns = [(cname, meta.column_index(cname), compile_expr(expr, scope))
                      for cname, expr in stmt.assignments]
        n = 0
        for rid, row in matched:
            new = list(row)
            for cname, j, fn in assign_fns:
                v = fn([row], None)
                new[j] = rows_mod.coerce_value(types[j], cname, v)
            blob = rows_mod.encode_row(types, tuple(new))
            if len(blob) > MAX_VALUE_SIZE:
                raise SQLError(f"row too large ({len(blob)} bytes)")
            meta.root_page = self.btree.upsert(meta.root_page, rid, blob)
            self._index_remove(meta, rid, row)
            self._index_add(meta, rid, tuple(new))
            n += 1
        self.catalog.save(self.pager)
        return f"{n} row(s) updated"

    def _delete(self, stmt):
        self._ensure_snapshot()
        meta = self.catalog.get(stmt.table)
        matched = list(self._matching_rows(meta, stmt.where))
        n = 0
        for rid, row in matched:
            meta.root_page, found = self.btree.delete(meta.root_page, rid)
            if found:
                meta.row_count -= 1
                self._index_remove(meta, rid, row)
                n += 1
        self.catalog.save(self.pager)
        return f"{n} row(s) deleted from {meta.name}"

    def _matching_rows(self, meta, where):
        alias = meta.name
        if where is None:
            yield from self.scan_table(meta)
            return
        conjuncts = flatten_and(where)
        shortcut = None
        pk = meta.pk_column()
        for col, val in find_equi_predicates(conjuncts):
            if not isinstance(col, Column):
                continue
            if col.table not in (None, alias):
                continue
            if col.name not in meta.colnames():
                continue
            if pk and col.name == pk:
                shortcut = ("pk", col.name, val)
                break
            if col.name in meta.indexes:
                shortcut = ("index", col.name, val)
                break
        candidates = None
        if shortcut is not None:
            kind, cname, val = shortcut
            if kind == "pk":
                try:
                    rid = self.coerce_pk(val)
                except SQLError:
                    rid = None
                if rid is not None:
                    blob = self.btree.search(meta.root_page, rid)
                    candidates = [(rid, rows_mod.decode_row(meta.types(), blob))] if blob is not None else []
            else:
                candidates = self.index_lookup(meta, cname, val)
        scope = Scope()
        scope.add(alias, meta)
        where_fn = compile_expr(where, scope)
        source = candidates if candidates is not None else self.scan_table(meta)
        for rid, row in source:
            if where_fn([row], None) is True:
                yield rid, row

    def _literal_value(self, expr):
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, UnaryOp) and expr.op == "NEG" and isinstance(expr.operand, Literal):
            v = expr.operand.value
            if isinstance(v, bool):
                raise SQLError("boolean literal is not a valid INSERT value")
            return -v
        raise SQLError("INSERT values must be literals")

    def coerce_pk(self, value):
        if isinstance(value, bool):
            raise SQLError("primary key cannot be boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise SQLError(f"primary key must be an integer, got {value!r}")

    def scan_table(self, meta):
        types = meta.types()
        for rid, blob in self.btree.range_scan(meta.root_page):
            yield rid, rows_mod.decode_row(types, blob)

    def index_lookup(self, meta, col, val):
        idx = self.indexes.get((meta.name, col))
        if idx is None:
            return []
        types = meta.types()
        ci = meta.column_index(col)
        out = []
        for rid in sorted(idx.get(val, ())):
            blob = self.btree.search(meta.root_page, rid)
            if blob is not None:
                out.append((rid, rows_mod.decode_row(types, blob)))
        return out

    def _index_add(self, meta, rid, row):
        ci_map = {c: meta.column_index(c) for c in meta.indexes}
        for cname, ci in ci_map.items():
            v = row[ci]
            if v is None:
                continue
            self.indexes.setdefault((meta.name, cname), {}).setdefault(v, set()).add(rid)

    def _index_remove(self, meta, rid, row):
        for cname in meta.indexes:
            ci = meta.column_index(cname)
            v = row[ci]
            if v is None:
                continue
            idx = self.indexes.get((meta.name, cname))
            if idx is not None:
                bucket = idx.get(v)
                if bucket is not None:
                    bucket.discard(rid)

    def _rebuild_indexes(self):
        self.indexes = {}
        for meta in self.catalog.tables.values():
            for cname in meta.indexes:
                idx = {}
                ci = meta.column_index(cname)
                for rid, row in self.scan_table(meta):
                    v = row[ci]
                    if v is not None:
                        idx.setdefault(v, set()).add(rid)
                self.indexes[(meta.name, cname)] = idx

    def vacuum(self, table):
        meta = self.catalog.get(table)
        new_root, n = self.btree.vacuum(meta.root_page)
        meta.root_page = new_root
        self.catalog.save(self.pager)
        return f"vacuumed {n} row(s) in {meta.name}"

    def stats(self):
        return {
            "tables": sorted(self.catalog.tables),
            "pager": self.pager.stats(),
            "in_transaction": self.in_txn,
        }


__all__ = ["Database", "Result", "SQLError", "ParseError", "CatalogError"]



