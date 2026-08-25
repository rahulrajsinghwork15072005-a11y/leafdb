import os
import random
import shutil
import tempfile
import unittest

os.environ["LEAFDB_FSYNC"] = "0"

from leafdb.btree import (
    BTree,
    DuplicateKey,
    Internal,
    Leaf,
    MAX_VALUE_SIZE,
    node_from_bytes,
)
from leafdb.catalog import Catalog, CatalogError, TableMeta
from leafdb.engine import Database, SQLError
from leafdb.pager import PAGE_SIZE, Pager
from leafdb import rows as rows_mod
from leafdb.sqlparse import (
    BinOp,
    Column,
    Literal,
    ParseError,
    tokenize,
    parse_script,
    parse_statement,
)
from leafdb.wal import WAL, recover


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def db_path(self, name="test.db"):
        return os.path.join(self.tmp, name)


class TestPager(TempDirCase):
    def test_fresh_pager_has_page_zero(self):
        p = Pager(self.db_path())
        self.assertEqual(p.num_pages, 1)
        self.assertEqual(p.get(0), bytes(PAGE_SIZE))
        p.close()

    def test_allocate_increments(self):
        p = Pager(self.db_path())
        a = p.allocate()
        b = p.allocate()
        self.assertEqual((a, b), (1, 2))
        self.assertEqual(p.num_pages, 3)
        p.close()

    def test_write_read_roundtrip(self):
        p = Pager(self.db_path())
        n = p.allocate()
        data = b"hello page"
        p.write(n, data)
        self.assertTrue(p.get(n).startswith(data))
        p.close()

    def test_page_overflow_rejected(self):
        p = Pager(self.db_path())
        with self.assertRaises(ValueError):
            p.write(0, bytes(PAGE_SIZE + 1))
        p.close()

    def test_persistence_across_close_and_reopen(self):
        path = self.db_path()
        p = Pager(path)
        n = p.allocate()
        p.write(n, b"persist-me")
        p.close()
        p2 = Pager(path)
        self.assertTrue(p2.get(n).startswith(b"persist-me"))
        p2.close()

    def test_lru_eviction_keeps_correct_data(self):
        p = Pager(self.db_path(), cache_size=2)
        pages = [p.allocate() for _ in range(4)]
        for i, n in enumerate(pages):
            p.write(n, bytes([i]) * 8)
        for i, n in enumerate(pages):
            self.assertTrue(p.get(n).startswith(bytes([i]) * 8))
        p.close()

    def test_dirty_eviction_writes_through(self):
        path = self.db_path()
        p = Pager(path, cache_size=1)
        a = p.allocate()
        b = p.allocate()
        p.write(a, b"AAAA")
        p.write(b, b"BBBB")
        p.close()
        p2 = Pager(path)
        self.assertTrue(p2.get(a).startswith(b"AAAA"))
        self.assertTrue(p2.get(b).startswith(b"BBBB"))
        p2.close()

    def test_transaction_touch_tracking(self):
        p = Pager(self.db_path())
        p.begin_txn()
        n = p.allocate()
        p.write(n, b"x")
        self.assertIn(n, p.touched)
        pages, num, images = p.collect_commit()
        self.assertIn(n, pages)
        self.assertIn(n, images)
        self.assertIsNone(p.touched)
        p.close()

    def test_rollback_restores_original_bytes(self):
        path = self.db_path()
        p = Pager(path)
        n = p.allocate()
        p.write(n, b"original")
        p.flush(fsync=True)
        p.begin_txn()
        p.write(n, b"changed!")
        p.rollback_txn()
        self.assertTrue(p.get(n).startswith(b"original"))
        p.close()
        p2 = Pager(path)
        self.assertTrue(p2.get(n).startswith(b"original"))
        p2.close()

    def test_stats_shape(self):
        p = Pager(self.db_path())
        st = p.stats()
        for key in ("cache_hits", "cache_misses", "hit_rate", "cached_pages", "dirty_pages", "file_pages"):
            self.assertIn(key, st)
        p.close()


class TestRows(unittest.TestCase):
    def test_roundtrip_basic(self):
        types = ["INT", "TEXT"]
        buf = rows_mod.encode_row(types, (42, "hello"))
        self.assertEqual(rows_mod.decode_row(types, buf), (42, "hello"))

    def test_nulls_preserved_by_position(self):
        types = ["INT", "TEXT", "INT"]
        buf = rows_mod.encode_row(types, (None, "a", None))
        self.assertEqual(rows_mod.decode_row(types, buf), (None, "a", None))

    def test_unicode_text(self):
        buf = rows_mod.encode_row(["TEXT"], ("héllo नमस्ते",))
        self.assertEqual(rows_mod.decode_row(["TEXT"], buf), ("héllo नमस्ते",))

    def test_empty_text(self):
        buf = rows_mod.encode_row(["TEXT"], ("",))
        self.assertEqual(rows_mod.decode_row(["TEXT"], buf), ("",))

    def test_negative_ints(self):
        buf = rows_mod.encode_row(["INT"], (-12345,))
        self.assertEqual(rows_mod.decode_row(["INT"], buf), (-12345,))

    def test_arity_mismatch_raises(self):
        with self.assertRaises(ValueError):
            rows_mod.encode_row(["INT"], (1, 2))

    def test_coerce_int_accepts_integral_float(self):
        self.assertEqual(rows_mod.coerce_value("INT", "c", 3.0), 3)

    def test_coerce_int_rejects_fractional_float(self):
        with self.assertRaises(TypeError):
            rows_mod.coerce_value("INT", "c", 3.5)

    def test_coerce_int_rejects_str(self):
        with self.assertRaises(TypeError):
            rows_mod.coerce_value("INT", "c", "5")

    def test_coerce_text_rejects_int(self):
        with self.assertRaises(TypeError):
            rows_mod.coerce_value("TEXT", "c", 7)

    def test_coerce_allows_none(self):
        self.assertIsNone(rows_mod.coerce_value("INT", "c", None))


class BTreeHarness(TempDirCase):
    def make_tree(self, name="btree.db"):
        pager = Pager(os.path.join(self.tmp, name))
        tree = BTree(pager)
        root = pager.allocate()
        pager.write(root, Leaf().to_bytes())
        self.addCleanup(pager.close)
        return pager, tree, root

    def encode(self, v):
        return v.to_bytes(4, "big")


class TestBTree(BTreeHarness):
    def test_empty_search_returns_none(self):
        _p, t, root = self.make_tree()
        self.assertIsNone(t.search(root, 1))

    def test_insert_then_search_hit(self):
        _p, t, root = self.make_tree()
        root = t.insert(root, 10, self.encode(99))
        self.assertEqual(t.search(root, 10), self.encode(99))

    def test_search_miss_returns_none(self):
        _p, t, root = self.make_tree()
        root = t.insert(root, 10, self.encode(1))
        self.assertIsNone(t.search(root, 11))

    def test_duplicate_key_raises(self):
        _p, t, root = self.make_tree()
        root = t.insert(root, 5, self.encode(1))
        with self.assertRaises(DuplicateKey):
            t.insert(root, 5, self.encode(2))

    def test_ordered_range_scan_sorted(self):
        _p, t, root = self.make_tree()
        for k in range(100):
            root = t.insert(root, k, self.encode(k))
        got = [k for k, _v in t.range_scan(root)]
        self.assertEqual(got, list(range(100)))

    def test_shuffled_inserts_stay_sorted(self):
        _p, t, root = self.make_tree()
        ks = list(range(500))
        random.Random(3).shuffle(ks)
        for k in ks:
            root = t.insert(root, k, self.encode(k))
        got = [k for k, _v in t.range_scan(root)]
        self.assertEqual(got, list(range(500)))

    def test_range_lo_only(self):
        _p, t, root = self.make_tree()
        for k in range(50):
            root = t.insert(root, k, self.encode(k))
        got = [k for k, _v in t.range_scan(root, lo=40)]
        self.assertEqual(got, list(range(40, 50)))

    def test_range_hi_only(self):
        _p, t, root = self.make_tree()
        for k in range(50):
            root = t.insert(root, k, self.encode(k))
        got = [k for k, _v in t.range_scan(root, hi=9)]
        self.assertEqual(got, list(range(10)))

    def test_range_inclusive_bounds(self):
        _p, t, root = self.make_tree()
        for k in range(50):
            root = t.insert(root, k, self.encode(k))
        got = [k for k, _v in t.range_scan(root, lo=10, hi=19)]
        self.assertEqual(got, list(range(10, 20)))

    def test_delete_existing(self):
        _p, t, root = self.make_tree()
        for k in range(30):
            root = t.insert(root, k, self.encode(k))
        root, found = t.delete(root, 15)
        self.assertTrue(found)
        self.assertIsNone(t.search(root, 15))
        got = [k for k, _v in t.range_scan(root)]
        self.assertNotIn(15, got)

    def test_delete_missing_returns_false(self):
        _p, t, root = self.make_tree()
        root = t.insert(root, 1, self.encode(1))
        root, found = t.delete(root, 999)
        self.assertFalse(found)

    def test_upsert_inserts_and_updates(self):
        _p, t, root = self.make_tree()
        root = t.upsert(root, 7, self.encode(1))
        root = t.upsert(root, 8, self.encode(2))
        root = t.upsert(root, 7, self.encode(111))
        self.assertEqual(t.search(root, 7), self.encode(111))
        self.assertEqual(t.search(root, 8), self.encode(2))

    def test_value_too_large_raises(self):
        _p, t, root = self.make_tree()
        with self.assertRaises(ValueError):
            t.insert(root, 1, bytes(MAX_VALUE_SIZE + 1))

    def test_multi_level_depth_grows(self):
        import leafdb.btree as btmod
        _p, t, root = self.make_tree("deep.db")
        original_page_size = btmod.PAGE_SIZE
        btmod.PAGE_SIZE = 160
        try:
            for k in range(500, 0, -1):
                root = t.insert(root, k, self.encode(k % 256))
            st = t.stats(root)
        finally:
            btmod.PAGE_SIZE = original_page_size
        self.assertGreaterEqual(st["depth"], 3)
        self.assertEqual(st["keys"], 500)

    def test_persistence_across_reopen(self):
        name = "bt_persist.db"
        pager = Pager(os.path.join(self.tmp, name))
        tree = BTree(pager)
        root = pager.allocate()
        pager.write(root, Leaf().to_bytes())
        for k in range(300):
            root = tree.insert(root, k, self.encode(k))
        pager.flush(fsync=True)
        pager.close()
        pager2 = Pager(os.path.join(self.tmp, name))
        tree2 = BTree(pager2)
        self.assertEqual(tree2.search(root, 299), self.encode(299))
        self.assertEqual([k for k, _ in tree2.range_scan(root)][:5], [0, 1, 2, 3, 4])
        pager2.close()

    def test_bulk_load_matches_sequential_inserts(self):
        _p, t, root = self.make_tree("bulk.db")
        pairs = [(k, self.encode(k)) for k in range(1000, 0, -1)]
        blroot = t.bulk_load(pairs)
        seq = dict()
        for k in range(1000, 0, -1):
            root = t.insert(root, k, self.encode(k))
        got_bulk = {k: v for k, v in t.range_scan(blroot)}
        got_seq = {k: v for k, v in t.range_scan(root)}
        self.assertEqual(got_bulk, got_seq)

    def test_vacuum_keeps_remaining_rows(self):
        _p, t, root = self.make_tree("vac.db")
        for k in range(400):
            root = t.insert(root, k, self.encode(k))
        for k in range(0, 400, 2):
            root, _f = t.delete(root, k)
        new_root, n = t.vacuum(root)
        self.assertEqual(n, 200)
        got = [k for k, _ in t.range_scan(new_root)]
        self.assertEqual(got, list(range(1, 400, 2)))

    def test_oracle_random_operations(self):
        for seed in (1, 2, 3, 4, 5):
            with self.subTest(seed=seed):
                _p, t, root = self.make_tree(f"oracle_{seed}.db")
                rng = random.Random(seed)
                oracle = {}
                for step in range(1500):
                    k = rng.randint(0, 400)
                    op = rng.random()
                    if op < 0.6:
                        v = self.encode(rng.randint(0, 999))
                        try:
                            root = t.insert(root, k, v)
                            oracle[k] = v
                        except DuplicateKey:
                            pass
                    elif op < 0.85:
                        root, found = t.delete(root, k)
                        self.assertEqual(found, k in oracle)
                        oracle.pop(k, None)
                    else:
                        got = t.search(root, k)
                        self.assertEqual(got, oracle.get(k))
                    if step % 300 == 299:
                        t.check(root)
                t.check(root)
                got = {k: v for k, v in t.range_scan(root)}
                self.assertEqual(got, oracle)

    def test_tree_stays_balanced_after_mass_deletes(self):
        _p, t, root = self.make_tree("massdel.db")
        import leafdb.btree as btmod
        original = btmod.PAGE_SIZE
        btmod.PAGE_SIZE = 160
        try:
            for k in range(800):
                root = t.insert(root, k, self.encode(k))
            st_full = t.stats(root)
            for k in range(0, 800, 4):
                root, f = t.delete(root, k)
                assert f
            t.check(root)
            st_thin = t.stats(root)
            for k in range(1, 800, 4):
                root, f = t.delete(root, k)
            t.check(root)
        finally:
            btmod.PAGE_SIZE = original
        self.assertEqual(st_thin["keys"], 600)
        self.assertGreaterEqual(st_full["depth"], 3)


class TestWAL(TempDirCase):
    def db_file(self, name="wal.db"):
        path = os.path.join(self.tmp, name)
        open(path, "wb").close()
        return path

    def test_header_written(self):
        w = WAL(os.path.join(self.tmp, "w.wal"))
        self.assertGreaterEqual(w.pending_bytes(), 0)
        w.reset()
        size = os.path.getsize(w.path)
        self.assertLess(size, 32)
        w.close()

    def test_recover_applies_committed_batch(self):
        dbp = self.db_file()
        walp = dbp + ".wal"
        w = WAL(walp)
        page_img = b"committed-data".ljust(PAGE_SIZE, b"\x00")
        w.append_batch({1: page_img}, num_pages=4)
        w.close()
        applied = recover(walp, dbp)
        self.assertEqual(applied, 1)
        with open(dbp, "rb") as f:
            f.seek(PAGE_SIZE)
            self.assertTrue(f.read(14).startswith(b"committed-data"))
        self.assertEqual(os.path.getsize(dbp), 4 * PAGE_SIZE)

    def test_uncommitted_batch_discarded(self):
        dbp = self.db_file()
        walp = dbp + ".wal"
        from leafdb.wal import _frame
        with open(walp, "wb") as f:
            f.write(b"LEAFWAL01\n")
            f.write(_frame(1, b"orphan".ljust(PAGE_SIZE, b"\x00")))
        applied = recover(walp, dbp)
        self.assertEqual(applied, 0)
        self.assertEqual(os.path.getsize(dbp), 0)

    def test_torn_tail_discarded_but_good_batches_applied(self):
        dbp = self.db_file()
        walp = dbp + ".wal"
        w = WAL(walp)
        w.append_batch({1: b"good".ljust(PAGE_SIZE, b"\x00")}, num_pages=2)
        w.file.write(b"\x00" * 7)
        w.close()
        applied = recover(walp, dbp)
        self.assertEqual(applied, 1)
        with open(dbp, "rb") as f:
            f.seek(PAGE_SIZE)
            self.assertTrue(f.read(4).startswith(b"good"))

    def test_multiple_batches_all_applied(self):
        dbp = self.db_file()
        walp = dbp + ".wal"
        w = WAL(walp)
        w.append_batch({1: b"one".ljust(PAGE_SIZE, b"\x00")}, num_pages=2)
        w.append_batch({1: b"two".ljust(PAGE_SIZE, b"\x00")}, num_pages=2)
        w.close()
        applied = recover(walp, dbp)
        self.assertEqual(applied, 2)
        with open(dbp, "rb") as f:
            f.seek(PAGE_SIZE)
            self.assertTrue(f.read(3).startswith(b"two"))

    def test_reset_clears_log(self):
        dbp = self.db_file()
        walp = dbp + ".wal"
        w = WAL(walp)
        w.append_batch({1: b"x".ljust(PAGE_SIZE, b"\x00")}, num_pages=2)
        self.assertGreater(w.pending_bytes(), 0)
        w.reset()
        self.assertEqual(w.pending_bytes(), 0)
        w.close()


class TestSQLParse(unittest.TestCase):
    def test_tokenize_kinds(self):
        toks = tokenize("SELECT x, 'it''s' FROM t1;")
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, ["kw", "ident", "op", "str", "kw", "ident", "op", "eof"])
        self.assertEqual(toks[3].value, "it's")

    def test_keywords_case_insensitive(self):
        toks = tokenize("select * from T where X = 1")
        self.assertEqual(toks[0].value, "SELECT")
        self.assertEqual(toks[4].value, "WHERE")

    def test_select_full_shape(self):
        stmt = parse_statement(
            "SELECT u.name AS nm, COUNT(*) AS cnt FROM users u "
            "LEFT JOIN orders o ON o.user_id = u.id "
            "WHERE u.age >= 21 GROUP BY u.name HAVING cnt > 2 "
            "ORDER BY cnt DESC LIMIT 5 OFFSET 2;"
        )
        self.assertEqual(stmt.distinct, False)
        self.assertEqual(stmt.table, "users")
        self.assertEqual(stmt.alias, "u")
        self.assertEqual(len(stmt.joins), 1)
        self.assertEqual(stmt.joins[0].jtype, "LEFT")
        self.assertEqual(len(stmt.group_by), 1)
        self.assertIsNotNone(stmt.having)
        self.assertEqual(stmt.order_by[0][1], True)
        self.assertEqual(stmt.limit, 5)
        self.assertEqual(stmt.offset, 2)

    def test_precedence_or_loosest(self):
        stmt = parse_statement("SELECT a FROM t WHERE a = 1 OR a = 2 AND b = 3")
        e = stmt.where
        self.assertIsInstance(e, BinOp)
        self.assertEqual(e.op, "OR")
        self.assertIsInstance(e.right, BinOp)
        self.assertEqual(e.right.op, "AND")

    def test_not_binds_above_comparison_operands(self):
        stmt = parse_statement("SELECT a FROM t WHERE NOT a = 1 AND b = 2")
        self.assertIsInstance(stmt.where, BinOp)
        self.assertEqual(stmt.where.op, "AND")
        self.assertIsInstance(stmt.where.left.operand, BinOp)

    def test_is_null_variants(self):
        stmt = parse_statement("SELECT a FROM t WHERE a IS NULL OR b IS NOT NULL")
        self.assertEqual(stmt.where.op, "OR")
        self.assertFalse(stmt.where.left.negated)
        self.assertTrue(stmt.where.right.negated)

    def test_qualified_columns(self):
        stmt = parse_statement("SELECT o.user_id FROM orders o WHERE o.id = 3")
        col = stmt.items[0][0]
        self.assertEqual((col.table, col.name), ("o", "user_id"))

    def test_insert_shapes(self):
        stmt = parse_statement("INSERT INTO t (a, b) VALUES (1, 'x'), (2, NULL)")
        self.assertEqual(stmt.columns, ["a", "b"])
        self.assertEqual(len(stmt.rows), 2)
        self.assertIsNone(stmt.rows[1][1].value)

    def test_create_table_pk_and_index(self):
        ct = parse_statement("CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
        self.assertTrue(ct.columns[0]["pk"])
        ci = parse_statement("CREATE INDEX idx_email ON users(email)")
        self.assertEqual(ci.column, "email")

    def test_update_and_delete(self):
        up = parse_statement("UPDATE users SET name = 'x', age = 30 WHERE id = 1")
        self.assertEqual(len(up.assignments), 2)
        de = parse_statement("DELETE FROM users WHERE id = 1")
        self.assertEqual(de.table, "users")

    def test_explain_wraps(self):
        st = parse_statement("EXPLAIN SELECT * FROM t")
        self.assertEqual(type(st.stmt).__name__, "SelectStmt")

    def test_begin_commit_rollback(self):
        names = [type(parse_statement(s)).__name__ for s in ("BEGIN", "COMMIT", "ROLLBACK")]
        self.assertEqual(names, ["BeginStmt", "CommitStmt", "RollbackStmt"])

    def test_syntax_errors_raise(self):
        bads = [
            "SELECT FROM t",
            "INSERT INTO t VALUES (",
            "CREATE TABLE t (x BLOB)",
            "SELECT a FROM t WHERE",
            "UPDATE t SET",
        ]
        for sql in bads:
            with self.subTest(sql=sql):
                with self.assertRaises(ParseError):
                    parse_statement(sql)

    def test_parse_script_splits_statements(self):
        stmts = parse_script("BEGIN; SELECT x FROM t; COMMIT;;")
        self.assertEqual(len(stmts), 3)

    def test_star_and_distinct(self):
        stmt = parse_statement("SELECT DISTINCT * FROM t")
        self.assertTrue(stmt.distinct)
        self.assertEqual(stmt.items[0][0].__class__.__name__, "Star")


class TestEngine(TempDirCase):
    def fresh(self, name="eng.db"):
        return Database(self.db_path(name))

    def test_schema_persists_across_reopen(self):
        path = self.db_path()
        db = Database(path)
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        db.close()
        db2 = Database(path)
        meta = db2.lookup_meta("t")
        self.assertEqual(meta.colnames(), ["id", "name"])
        self.assertEqual(meta.pk_column(), "id")
        db2.close()

    def test_insert_select_roundtrip(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'ada'), (2, 'grace')")
        res = db.execute("SELECT id, name FROM t ORDER BY id")
        self.assertEqual(res.rows, [(1, "ada"), (2, "grace")])
        db.close()

    def test_autoincrement_ids(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t (v) VALUES ('a')")
        db.execute("INSERT INTO t (v) VALUES ('b')")
        res = db.execute("SELECT id FROM t ORDER BY id")
        self.assertEqual(res.rows, [(1,), (2,)])
        db.close()

    def test_explicit_pk_advances_autoincrement(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (10)")
        db.execute("INSERT INTO t (id) VALUES (NULL)")
        res = db.execute("SELECT id FROM t ORDER BY id")
        self.assertEqual(res.rows, [(10,), (11,)])
        db.close()

    def test_duplicate_pk_rejected(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        with self.assertRaises(SQLError):
            db.execute("INSERT INTO t VALUES (1)")
        res = db.execute("SELECT COUNT(*) AS n FROM t")
        self.assertEqual(res.rows[0][0], 1)
        db.close()

    def test_text_pk_rejected_at_ddl(self):
        db = self.fresh()
        with self.assertRaises(Exception):
            db.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
        db.close()

    def test_type_mismatch_rejected(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, age INT)")
        with self.assertRaises(TypeError):
            db.execute("INSERT INTO t VALUES (1, 'old')")
        db.close()

    def test_where_numeric_filter(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
        db.execute("INSERT INTO t VALUES (1, 5), (2, 50), (3, 500)")
        res = db.execute("SELECT id FROM t WHERE n > 10 ORDER BY id")
        self.assertEqual(res.rows, [(2,), (3,)])
        db.close()

    def test_null_excluded_by_comparisons(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
        db.execute("INSERT INTO t VALUES (1, 1), (2, NULL), (3, 0)")
        res = db.execute("SELECT id FROM t WHERE n <> 1 ORDER BY id")
        self.assertEqual(res.rows, [(3,)])
        res = db.execute("SELECT id FROM t WHERE NOT (n = 1) ORDER BY id")
        self.assertEqual(res.rows, [(3,)])
        db.close()

    def test_is_null_filters(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
        db.execute("INSERT INTO t VALUES (1, NULL), (2, 4)")
        self.assertEqual(db.execute("SELECT id FROM t WHERE n IS NULL").rows, [(1,)])
        self.assertEqual(db.execute("SELECT id FROM t WHERE n IS NOT NULL").rows, [(2,)])
        db.close()

    def test_null_or_logic(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
        db.execute("INSERT INTO t VALUES (1, NULL), (2, 0)")
        res = db.execute("SELECT id FROM t WHERE n = 0 OR n IS NULL ORDER BY id")
        self.assertEqual(res.rows, [(1,), (2,)])
        db.close()

    def test_arithmetic_null_propagation(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, n INT)")
        db.execute("INSERT INTO t VALUES (1, NULL), (2, 21)")
        res = db.execute("SELECT n * 2 AS dbl FROM t ORDER BY 1 DESC")
        self.assertEqual(res.rows, [(42,), (None,)])
        res = db.execute("SELECT 10 / 0 FROM t WHERE id = 1")
        self.assertEqual(res.rows, [(None,)])
        db.close()

    def test_aggregates_full_set(self):
        db = self.fresh()
        db.execute("CREATE TABLE s (id INT PRIMARY KEY, v INT)")
        db.execute("INSERT INTO s VALUES (1, 10), (2, 20), (3, NULL), (4, 30)")
        r = db.execute(
            "SELECT COUNT(*) AS c, COUNT(v) AS cv, SUM(v) AS s, AVG(v) AS a, MIN(v) AS mn, MAX(v) AS mx FROM s"
        )
        self.assertEqual(r.rows[0], (4, 3, 60, 20.0, 10, 30))
        db.close()

    def test_global_aggregates_on_empty_table(self):
        db = self.fresh()
        db.execute("CREATE TABLE s (id INT PRIMARY KEY, v INT)")
        r = db.execute("SELECT COUNT(*) AS c, SUM(v) AS sv FROM s")
        self.assertEqual(r.rows, [(0, None)])
        db.close()

    def test_group_by_having(self):
        db = self.fresh()
        db.execute("CREATE TABLE o (id INT PRIMARY KEY, city TEXT, amt INT)")
        db.execute(
            "INSERT INTO o VALUES (1,'pune',10),(2,'pune',20),(3,'delhi',5),"
            "(4,'delhi',5),(5,'goa',1)"
        )
        r = db.execute(
            "SELECT city, SUM(amt) AS total FROM o GROUP BY city HAVING total >= 15 ORDER BY total DESC"
        )
        self.assertEqual(r.rows, [("pune", 30)])
        r = db.execute(
            "SELECT city, COUNT(*) AS cnt FROM o GROUP BY city HAVING SUM(amt) > 5 ORDER BY cnt DESC, city"
        )
        self.assertEqual(r.rows, [("delhi", 2), ("pune", 2)])
        db.close()

    def test_order_limit_offset_with_nulls(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        db.execute("INSERT INTO t VALUES (1, NULL), (2, 30), (3, 10), (4, 20), (5, NULL)")
        r = db.execute("SELECT id, v FROM t ORDER BY v ASC, id ASC LIMIT 2 OFFSET 1")
        self.assertEqual(r.rows, [(5, None), (3, 10)])
        r = db.execute("SELECT id FROM t ORDER BY v DESC LIMIT 1")
        self.assertEqual(r.rows, [(2,)])
        db.close()

    def test_distinct(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, c TEXT)")
        db.execute("INSERT INTO t VALUES (1,'a'),(2,'a'),(3,'b'),(4,NULL),(5,NULL)")
        r = db.execute("SELECT DISTINCT c FROM t ORDER BY c")
        self.assertEqual(r.rows, [(None,), ("a",), ("b",)])
        db.close()

    def seed_join_tables(self, db):
        db.execute_script(
            """
            CREATE TABLE users (id INT PRIMARY KEY, city TEXT);
            CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amt INT);
            INSERT INTO users VALUES (1,'pune'),(2,'delhi'),(3,'goa');
            INSERT INTO orders VALUES (10,1,5),(11,1,7),(12,2,9);
            """
        )

    def test_inner_join(self):
        db = self.fresh()
        self.seed_join_tables(db)
        r = db.execute(
            "SELECT u.city, o.amt FROM orders o JOIN users u ON o.user_id = u.id ORDER BY o.id"
        )
        self.assertEqual(r.rows, [("pune", 5), ("pune", 7), ("delhi", 9)])
        db.close()

    def test_left_join_unmatched_nulls(self):
        db = self.fresh()
        self.seed_join_tables(db)
        r = db.execute(
            "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON o.user_id = u.id ORDER BY u.id, o.id"
        )
        self.assertEqual(r.rows, [(1, 10), (1, 11), (2, 12), (3, None)])
        db.close()

    def test_right_join(self):
        db = self.fresh()
        self.seed_join_tables(db)
        r = db.execute(
            "SELECT o.id, u.id FROM orders o RIGHT JOIN users u ON o.user_id = u.id ORDER BY o.id, u.id"
        )
        self.assertIn((None, 3), r.rows)
        self.assertEqual(len(r.rows), 4)
        db.close()

    def test_full_outer_join(self):
        db = self.fresh()
        self.seed_join_tables(db)
        db.execute("INSERT INTO orders VALUES (13, 99, 1)")
        r = db.execute(
            "SELECT u.id, o.id FROM users u FULL JOIN orders o ON o.user_id = u.id ORDER BY u.id, o.id"
        )
        self.assertIn((3, None), r.rows)
        self.assertIn((None, 13), r.rows)
        self.assertEqual(len(r.rows), 5)
        db.close()

    def test_hash_join_large_sides_consistent(self):
        db = self.fresh()
        db.execute("CREATE TABLE a (id INT PRIMARY KEY, k INT)")
        db.execute("CREATE TABLE b (id INT PRIMARY KEY, k INT)")
        stmts = []
        for i in range(1, 301):
            stmts.append(f"INSERT INTO a VALUES ({i}, {i % 25})")
        for i in range(1, 301):
            stmts.append(f"INSERT INTO b VALUES ({i}, {(i * 7) % 25})")
        db.execute_script("BEGIN;\n" + ";\n".join(stmts) + ";COMMIT;")
        r = db.execute(
            "SELECT COUNT(*) AS n FROM a JOIN b ON a.k = b.k"
        )
        expected = sum(1 for i in range(1, 301) for j in range(1, 301) if i % 25 == (j * 7) % 25)
        self.assertEqual(r.rows[0][0], expected)
        db.close()

    def test_qualified_alias_required_for_ambiguity(self):
        db = self.fresh()
        self.seed_join_tables(db)
        with self.assertRaises(SQLError):
            db.execute("SELECT id FROM orders o JOIN users u ON o.user_id = u.id")
        db.close()

    def test_update_rows_and_pk_guard(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT, n INT)")
        db.execute("INSERT INTO t VALUES (1,'a',1),(2,'b',2)")
        msg = db.execute("UPDATE t SET v = 'z', n = n * 10 WHERE id <= 1").message
        self.assertIn("1 row(s) updated", msg)
        self.assertEqual(db.execute("SELECT v, n FROM t WHERE id = 1").rows, [("z", 10)])
        with self.assertRaises(SQLError):
            db.execute("UPDATE t SET id = 5 WHERE id = 1")
        db.close()

    def test_delete_rows(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        db.execute("INSERT INTO t VALUES (1,1),(2,2),(3,3)")
        msg = db.execute("DELETE FROM t WHERE id <> 2").message
        self.assertIn("2 row(s) deleted", msg)
        self.assertEqual(db.execute("SELECT id FROM t").rows, [(2,)])
        db.close()

    def test_secondary_index_created_used_and_maintained(self):
        db = self.fresh()
        db.execute("CREATE TABLE u (id INT PRIMARY KEY, email TEXT, city TEXT)")
        db.execute("INSERT INTO u VALUES (1,'a@x.com','pune'),(2,'b@x.com','delhi')")
        db.execute("CREATE INDEX idx_city ON u(city)")
        plan = db.execute("EXPLAIN SELECT * FROM u WHERE city = 'delhi'")
        self.assertTrue(any("INDEX SCAN" in row[0] for row in plan.rows))
        self.assertEqual(db.execute("SELECT id FROM u WHERE city = 'delhi'").rows, [(2,)])
        db.execute("INSERT INTO u VALUES (3,'c@x.com','pune')")
        self.assertEqual(db.execute("SELECT id FROM u WHERE city = 'pune' ORDER BY id").rows, [(1,), (3,)])
        db.execute("UPDATE u SET city = 'goa' WHERE id = 3")
        self.assertEqual(db.execute("SELECT id FROM u WHERE city = 'pune'").rows, [(1,)])
        db.execute("DELETE FROM u WHERE id = 1")
        self.assertEqual(db.execute("SELECT id FROM u WHERE city = 'pune'").rows, [])
        db.close()

    def test_transactions_commit_across_reopen(self):
        path = self.db_path()
        db = Database(path)
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        db.execute_script("BEGIN; INSERT INTO t VALUES (1, 11); COMMIT;")
        db.close()
        db2 = Database(path)
        self.assertEqual(db2.execute("SELECT v FROM t WHERE id = 1").rows, [(11,)])
        db2.close()

    def test_rollback_restores_state(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        db.execute("INSERT INTO t VALUES (1, 1)")
        db.execute_script("BEGIN; DELETE FROM t; INSERT INTO t VALUES (9, 99); ROLLBACK;")
        r = db.execute("SELECT id, v FROM t ORDER BY id")
        self.assertEqual(r.rows, [(1, 1)])
        db.close()

    def test_crash_recovery_replays_wal(self):
        path = self.db_path()
        db = Database(path)
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'survives')")
        db.close(checkpoint=False)
        self.assertTrue(os.path.exists(path + ".wal"))
        db2 = Database(path)
        self.assertEqual(db2.execute("SELECT v FROM t").rows, [("survives",)])
        db2.close()

    def test_uncommitted_changes_lost_after_crash(self):
        path = self.db_path()
        db = Database(path)
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        db.execute_script("BEGIN; INSERT INTO t VALUES (1, 'lost');")
        db.close(checkpoint=False)
        db2 = Database(path)
        self.assertEqual(db2.execute("SELECT COUNT(*) AS n FROM t").rows, [(0,)])
        db2.close()

    def test_explain_lists_plan_steps(self):
        db = self.fresh()
        self.seed_join_tables(db)
        r = db.execute(
            "EXPLAIN SELECT u.city, COUNT(*) AS c FROM orders o JOIN users u "
            "ON o.user_id = u.id GROUP BY u.city ORDER BY c DESC LIMIT 3"
        )
        text = "\n".join(row[0] for row in r.rows)
        self.assertIn("HASH JOIN", text)
        self.assertIn("GROUP AGGREGATE", text)
        self.assertIn("SORT", text)
        self.assertIn("LIMIT", text)
        db.close()

    def test_pk_lookup_plan_and_result(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        db.execute("INSERT INTO t VALUES (42, 'deep')")
        plan = db.execute("EXPLAIN SELECT * FROM t WHERE id = 42")
        self.assertIn("PK LOOKUP", plan.rows[0][0])
        self.assertEqual(db.execute("SELECT v FROM t WHERE id = 42").rows, [("deep",)])
        db.close()

    def test_vacuum_preserves_data(self):
        db = self.fresh()
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        stmts = [f"INSERT INTO t VALUES ({i}, {i})" for i in range(200)]
        db.execute_script("BEGIN;\n" + ";\n".join(stmts) + ";\nCOMMIT;")
        db.execute("DELETE FROM t WHERE id <= 99")
        msg = db.vacuum("t")
        self.assertIn("100 row(s)", msg)
        self.assertEqual(db.execute("SELECT COUNT(*) AS n FROM t").rows[0][0], 100)
        db.close()

    def test_execute_script_runs_many_statements(self):
        db = self.fresh()
        results = db.execute_script(
            "CREATE TABLE t (id INT PRIMARY KEY, v INT);"
            "INSERT INTO t VALUES (1, 1);"
            "INSERT INTO t VALUES (2, 2);"
            "SELECT COUNT(*) AS n FROM t;"
        )
        self.assertEqual(len(results), 4)
        self.assertEqual(results[-1].rows, [(2,)])
        db.close()

    def test_catalog_helpers(self):
        cat = Catalog()
        m = TableMeta(name="t", columns=[{"name": "id", "type": "INT", "pk": True}], root_page=3)
        cat.add(m)
        with self.assertRaises(CatalogError):
            cat.add(m)
        with self.assertRaises(CatalogError):
            cat.get("missing")
        self.assertEqual(cat.get("t").pk_index(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

