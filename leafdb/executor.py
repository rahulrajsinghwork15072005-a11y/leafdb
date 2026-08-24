import re

from .planner import build_select_plan
from .sqlparse import (
    Between,
    BinOp,
    Column,
    Func,
    InList,
    IsNull,
    Like,
    Literal,
    SQLError,
    Star,
    UnaryOp,
    walk_func_nodes,
)
from . import rows as rows_mod


class ExecutionError(SQLError):
    pass


class CompileError(SQLError):
    pass


_LIKE_CACHE = {}


def like_regex(pattern):
    rx = _LIKE_CACHE.get(pattern)
    if rx is None:
        parts = []
        for ch in pattern:
            if ch == "%":
                parts.append(".*")
            elif ch == "_":
                parts.append(".")
            else:
                parts.append(re.escape(ch))
        rx = re.compile("^" + "".join(parts) + "$", re.IGNORECASE | re.DOTALL)
        _LIKE_CACHE[pattern] = rx
    return rx


class Scope:
    """Maps column references to (source_slot, column_index) positions."""

    def __init__(self):
        self.sources = []
        self._by_alias = {}
        self.agg_aliases = {}

    def add(self, alias, meta):
        if alias in self._by_alias:
            raise ExecutionError(f"duplicate table alias {alias!r}")
        self._by_alias[alias] = len(self.sources)
        self.sources.append((alias, meta))

    def add_agg_alias(self, label, func_node):
        if label:
            self.agg_aliases[label.lower()] = func_node

    def nslots(self):
        return len(self.sources)

    def resolve(self, col):
        if col.table is not None:
            idx = self._by_alias.get(col.table)
            if idx is None:
                raise ExecutionError(f"unknown table or alias {col.table!r}")
            meta = self.sources[idx][1]
            if col.name not in meta.colnames():
                raise ExecutionError(f"table {col.table!r} has no column {col.name!r}")
            return idx, meta.column_index(col.name)
        hits = []
        for i, (_alias, meta) in enumerate(self.sources):
            if col.name in meta.colnames():
                hits.append((i, meta.column_index(col.name)))
        if not hits:
            raise ExecutionError(f"unknown column {col.name!r}")
        if len(hits) > 1:
            raise ExecutionError(f"ambiguous column {col.name!r}; qualify it with a table alias")
        return hits[0]


def eval_expr(expr, ctx, scope, agg_env=None):
    """Interpreted evaluation. Kept for DML paths (UPDATE/DELETE filtering);
    SELECT uses compiled closures from compile_expr instead."""
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, Column):
        if scope is None or ctx is None:
            raise ExecutionError(f"column reference {expr.name!r} is not allowed here")
        try:
            i, j = scope.resolve(expr)
        except ExecutionError:
            if agg_env is not None and expr.table is None and expr.name.lower() in scope.agg_aliases:
                return agg_env[id(scope.agg_aliases[expr.name.lower()])]
            raise
        row = ctx[i]
        return None if row is None else row[j]
    if isinstance(expr, Func):
        if agg_env is not None and id(expr) in agg_env:
            return agg_env[id(expr)]
        raise ExecutionError(
            f"aggregate {expr.name}() is only valid in a grouped query, SELECT list or HAVING"
        )
    if isinstance(expr, IsNull):
        v = eval_expr(expr.operand, ctx, scope, agg_env)
        return (v is not None) if expr.negated else (v is None)
    if isinstance(expr, Between):
        v = eval_expr(expr.operand, ctx, scope, agg_env)
        lo = eval_expr(expr.lo, ctx, scope, agg_env)
        hi = eval_expr(expr.hi, ctx, scope, agg_env)
        if v is None or lo is None or hi is None:
            return None
        _order_cmp(v, lo)
        _order_cmp(v, hi)
        result = lo <= v <= hi
        return (not result) if expr.negated else result
    if isinstance(expr, InList):
        v = eval_expr(expr.operand, ctx, scope, agg_env)
        if v is None:
            return None
        saw_null = False
        found = False
        for item in expr.items:
            cand = eval_expr(item, ctx, scope, agg_env)
            if cand is None:
                saw_null = True
                continue
            if _eq(v, cand):
                found = True
                break
        result = True if found else (None if saw_null else False)
        return (not result) if expr.negated and result is not None else result
    if isinstance(expr, Like):
        v = eval_expr(expr.operand, ctx, scope, agg_env)
        pat = eval_expr(expr.pattern, ctx, scope, agg_env)
        if v is None or pat is None:
            return None
        if isinstance(v, bool):
            v = int(v)
        if not isinstance(v, str):
            v = str(v)
        if not isinstance(pat, str):
            raise ExecutionError("LIKE pattern must be text")
        result = bool(like_regex(pat).match(v))
        return (not result) if expr.negated else result
    if isinstance(expr, UnaryOp):
        if expr.op == "NOT":
            v = eval_expr(expr.operand, ctx, scope, agg_env)
            return None if v is None else (not v)
        v = eval_expr(expr.operand, ctx, scope, agg_env)
        if v is None:
            return None
        _require_number(v, "unary minus")
        return -v
    if isinstance(expr, BinOp):
        op = expr.op
        if op in ("AND", "OR"):
            left = eval_expr(expr.left, ctx, scope, agg_env)
            right = eval_expr(expr.right, ctx, scope, agg_env)
            return _kleene(op, left, right)
        left = eval_expr(expr.left, ctx, scope, agg_env)
        right = eval_expr(expr.right, ctx, scope, agg_env)
        if left is None or right is None:
            return None
        if op == "=":
            return _eq(left, right)
        if op == "<>":
            return not _eq(left, right)
        if op in ("<", "<=", ">", ">="):
            c = _order_cmp(left, right)
            return {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[op]
        if op in ("+", "-", "*", "/"):
            _require_number(left, f"'{op}'")
            _require_number(right, f"'{op}'")
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if right == 0:
                return None
            q = left / right
            if isinstance(left, int) and isinstance(right, int):
                return int(q)
            return q
    raise ExecutionError(f"cannot evaluate expression of type {type(expr).__name__}")


def compile_expr(expr, scope, agg_aliases=None, allow_agg=False):
    """Compile an AST expression into a closure f(parts, agg_env) -> value.

    Column references are resolved once at compile time into (slot, index)
    pairs; per-row evaluation becomes two subscripts instead of an AST walk.
    """
    if isinstance(expr, Literal):
        value = expr.value
        return lambda parts, env: value

    if isinstance(expr, Column):
        try:
            i, j = scope.resolve(expr)
        except ExecutionError:
            if allow_agg and agg_aliases and expr.table is None \
                    and expr.name.lower() in agg_aliases:
                fid = id(agg_aliases[expr.name.lower()])

                def read_alias(parts, env):
                    if env is None or fid not in env:
                        raise ExecutionError(
                            f"aggregate alias {expr.name!r} used outside grouping")
                    return env[fid]
                return read_alias
            raise

        def read_col(parts, env):
            row = parts[i]
            return None if row is None else row[j]
        return read_col

    if isinstance(expr, Func):
        fid = id(expr)

        def read_agg(parts, env):
            if env is None or fid not in env:
                raise ExecutionError(
                    f"aggregate {expr.name}() is only valid in a grouped query, "
                    f"SELECT list or HAVING")
            return env[fid]
        return read_agg

    if isinstance(expr, IsNull):
        inner = compile_expr(expr.operand, scope, agg_aliases, allow_agg)
        negated = expr.negated

        def is_null_fn(parts, env):
            v = inner(parts, env)
            return (v is not None) if negated else (v is None)
        return is_null_fn

    if isinstance(expr, Between):
        inner = compile_expr(expr.operand, scope, agg_aliases, allow_agg)
        lo_fn = compile_expr(expr.lo, scope, agg_aliases, allow_agg)
        hi_fn = compile_expr(expr.hi, scope, agg_aliases, allow_agg)
        negated = expr.negated

        def between_fn(parts, env):
            v = inner(parts, env)
            if v is None:
                return None
            lo = lo_fn(parts, env)
            hi = hi_fn(parts, env)
            if lo is None or hi is None:
                return None
            _order_cmp(v, lo)
            _order_cmp(v, hi)
            r = lo <= v <= hi
            return (not r) if negated else r
        return between_fn

    if isinstance(expr, InList):
        inner = compile_expr(expr.operand, scope, agg_aliases, allow_agg)
        item_fns = [compile_expr(it, scope, agg_aliases, allow_agg) for it in expr.items]
        negated = expr.negated

        def in_fn(parts, env):
            v = inner(parts, env)
            if v is None:
                return None
            saw_null = False
            for fn in item_fns:
                cand = fn(parts, env)
                if cand is None:
                    saw_null = True
                elif _eq(v, cand):
                    r = True
                    return (not r) if negated else r
            if saw_null:
                return None
            r = False
            return (not r) if negated else r
        return in_fn

    if isinstance(expr, Like):
        inner = compile_expr(expr.operand, scope, agg_aliases, allow_agg)
        pat_fn = compile_expr(expr.pattern, scope, agg_aliases, allow_agg)
        negated = expr.negated
        pattern_literal = expr.pattern.value if isinstance(expr.pattern, Literal) else None
        rx = like_regex(pattern_literal) if isinstance(pattern_literal, str) else None

        def like_fn(parts, env):
            v = inner(parts, env)
            p = pat_fn(parts, env)
            if v is None or p is None:
                return None
            if isinstance(v, bool):
                v = int(v)
            if not isinstance(v, str):
                v = str(v)
            if not isinstance(p, str):
                raise ExecutionError("LIKE pattern must be text")
            r = bool((rx or like_regex(p)).match(v))
            return (not r) if negated else r
        return like_fn

    if isinstance(expr, UnaryOp):
        inner = compile_expr(expr.operand, scope, agg_aliases, allow_agg)
        if expr.op == "NOT":

            def not_fn(parts, env):
                v = inner(parts, env)
                return None if v is None else (not v)
            return not_fn

        def neg_fn(parts, env):
            v = inner(parts, env)
            if v is None:
                return None
            _require_number(v, "unary minus")
            return -v
        return neg_fn

    if isinstance(expr, BinOp):
        op = expr.op
        lfn = compile_expr(expr.left, scope, agg_aliases, allow_agg)
        rfn = compile_expr(expr.right, scope, agg_aliases, allow_agg)

        if op == "AND":
            def and_fn(parts, env):
                l = lfn(parts, env)
                if l is not None and not l:
                    return False
                r = rfn(parts, env)
                if r is not None and not r:
                    return False
                if l is None or r is None:
                    return None
                return True
            return and_fn

        if op == "OR":
            def or_fn(parts, env):
                l = lfn(parts, env)
                if l is not None and bool(l):
                    return True
                r = rfn(parts, env)
                if r is not None and bool(r):
                    return True
                if l is None or r is None:
                    return None
                return False
            return or_fn

        if op == "=":
            def eq_fn(parts, env):
                l = lfn(parts, env)
                r = rfn(parts, env)
                if l is None or r is None:
                    return None
                return _eq(l, r)
            return eq_fn

        if op == "<>":
            def neq_fn(parts, env):
                l = lfn(parts, env)
                r = rfn(parts, env)
                if l is None or r is None:
                    return None
                return not _eq(l, r)
            return neq_fn

        if op in ("<", "<=", ">", ">="):
            def cmp_fn(parts, env):
                l = lfn(parts, env)
                r = rfn(parts, env)
                if l is None or r is None:
                    return None
                c = _order_cmp(l, r)
                if op == "<":
                    return c < 0
                if op == "<=":
                    return c <= 0
                if op == ">":
                    return c > 0
                return c >= 0
            return cmp_fn

        if op in ("+", "-", "*", "/"):
            simple = {"+": lambda a, b: a + b,
                      "-": lambda a, b: a - b,
                      "*": lambda a, b: a * b}.get(op)

            def arith_fn(parts, env):
                l = lfn(parts, env)
                r = rfn(parts, env)
                if l is None or r is None:
                    return None
                if isinstance(l, bool) or not isinstance(l, (int, float)):
                    raise ExecutionError(f"'{op}' requires numeric operands")
                if isinstance(r, bool) or not isinstance(r, (int, float)):
                    raise ExecutionError(f"'{op}' requires numeric operands")
                if simple is not None:
                    return simple(l, r)
                if r == 0:
                    return None
                q = l / r
                return int(q) if isinstance(l, int) and isinstance(r, int) else q
            return arith_fn

    raise CompileError(f"cannot compile expression of type {type(expr).__name__}")


def _require_number(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ExecutionError(f"{what} requires a number, got {type(v).__name__}")


def _eq(l, r):
    if isinstance(l, str) != isinstance(r, str):
        return False
    try:
        return bool(l == r)
    except TypeError:
        raise ExecutionError(f"cannot compare {type(l).__name__} with {type(r).__name__}")


def _order_cmp(l, r):
    if isinstance(l, str) != isinstance(r, str):
        raise ExecutionError(f"cannot order {type(l).__name__} against {type(r).__name__}")
    try:
        if l < r:
            return -1
        if l > r:
            return 1
        return 0
    except TypeError:
        raise ExecutionError("cannot order these values")


def _kleene(op, l, r):
    lb = None if l is None else bool(l)
    rb = None if r is None else bool(r)
    if op == "AND":
        if lb is False or rb is False:
            return False
        if lb is None or rb is None:
            return None
        return True
    if lb is True or rb is True:
        return True
    if lb is None or rb is None:
        return None
    return False


def compute_aggregate(func, rows_ctx, scope):
    """Interpreted aggregate computation (used by tests and DML tooling)."""
    name = func.name
    if func.star:
        if name != "COUNT":
            raise ExecutionError(f"{name}(*) is not supported")
        return len(rows_ctx)
    argfn = compile_expr(func.arg, scope)
    values = [v for v in (argfn(parts, None) for parts in rows_ctx) if v is not None]
    return _accumulate(name, values)


def _accumulate(name, values):
    if name == "COUNT":
        return len(values)
    if not values:
        return None
    if name == "SUM":
        total = values[0]
        for v in values[1:]:
            _require_number(total, "SUM")
            _require_number(v, "SUM")
            total = total + v
        return total
    if name == "AVG":
        total = 0
        for v in values:
            _require_number(v, "AVG")
            total += v
        return total / len(values)
    best = values[0]
    for v in values[1:]:
        c = _order_cmp(best, v)
        if name == "MIN" and c > 0:
            best = v
        elif name == "MAX" and c < 0:
            best = v
    return best


def _group_key_value(v):
    if isinstance(v, str):
        return ("t", v)
    if v is None:
        return ("n",)
    return ("i", v)


def sort_wrap(v):
    if v is None:
        return (0, 0, 0)
    if isinstance(v, bool):
        return (2, 0, int(v))
    if isinstance(v, str):
        return (1, 0, v)
    return (1, 1, v)


def walk_in_items(sel):
    for expr, _label in sel.items:
        if walk_func_nodes(expr):
            return True
    if sel.having is not None and walk_func_nodes(sel.having):
        return True
    for expr, _d in sel.order_by:
        if walk_func_nodes(expr):
            return True
    return False


class Executor:
    """Pipeline: scan -> staged filters -> joins (+ staged filters) ->
    grouping/aggregation -> HAVING -> projection -> DISTINCT -> sort -> LIMIT.

    All expressions are compiled to closures once per statement.
    """

    def __init__(self, db):
        self.db = db

    def run_select(self, sel, plan=None):
        if plan is None:
            plan = build_select_plan(sel, self.db.lookup_meta)
        scope = Scope()
        scan = plan.scan
        scope.add(scan["alias"], scan["meta"])
        contexts = [[row] for _rid, row in self._scan(scan["meta"], scan)]

        staged = {}
        for stage, conj in getattr(plan, "filters", ()) or ():
            staged.setdefault(stage, []).append(conj)

        def apply_stage(s):
            nonlocal contexts
            conjuncts = staged.get(s)
            if not conjuncts:
                return
            for conj in conjuncts:
                fn = compile_expr(conj, scope)
                contexts = [parts for parts in contexts if fn(parts, None) is True]

        apply_stage(0)
        for jidx, jinfo in enumerate(plan.joins, start=1):
            scope.add(jinfo["alias"], jinfo["meta"])
            contexts = self._join(contexts, jinfo, scope)
            apply_stage(jidx)

        grouped = bool(sel.group_by) or walk_in_items(sel)
        if grouped:
            for expr, label in sel.items:
                if isinstance(expr, Func):
                    scope.add_agg_alias(label, expr)
            units = self._aggregate(sel, contexts, scope)
        else:
            if sel.having is not None:
                raise ExecutionError("HAVING requires GROUP BY or an aggregate")
            units = [{"parts": parts, "agg_env": None} for parts in contexts]

        cols = []
        for expr, label in sel.items:
            if isinstance(expr, Star):
                for _alias, meta in scope.sources:
                    cols.extend(meta.colnames())
            else:
                cols.append(label)

        item_fns = [
            None if isinstance(expr, Star) else compile_expr(expr, scope)
            for expr, _label in sel.items
        ]

        order_specs = []
        for oexpr, desc in sel.order_by:
            if isinstance(oexpr, Literal) and isinstance(oexpr.value, int) \
                    and not isinstance(oexpr.value, bool):
                pos = oexpr.value - 1
                if pos < 0:
                    raise ExecutionError(f"ORDER BY position {oexpr.value} is out of range")
                order_specs.append(("idx", pos))
            elif isinstance(oexpr, Column) and oexpr.table is None and oexpr.name in cols:
                order_specs.append(("idx", cols.index(oexpr.name)))
            else:
                order_specs.append(("fn", compile_expr(
                    oexpr, scope,
                    agg_aliases=scope.agg_aliases if grouped else None,
                    allow_agg=grouped)))

        star_slots = [(si, len(meta.columns))
                      for si, (_alias, meta) in enumerate(scope.sources)]
        has_star = any(fn is None for fn in item_fns)

        projected = []
        for unit in units:
            out_row = []
            for fn, (expr, _label) in zip(item_fns, sel.items):
                if fn is None:
                    for si, width in star_slots:
                        prow = unit["parts"][si]
                        out_row.extend([None] * width if prow is None else prow[:width])
                else:
                    out_row.append(fn(unit["parts"], unit["agg_env"]))
            keys = []
            for kind, val in order_specs:
                if kind == "fn":
                    keys.append(val(unit["parts"], unit["agg_env"]))
                else:
                    if val >= len(out_row):
                        raise ExecutionError("ORDER BY position is out of range")
                    keys.append(out_row[val])
            projected.append((tuple(out_row), keys))

        if sel.distinct:
            seen = set()
            uniq = []
            for item in projected:
                if item[0] not in seen:
                    seen.add(item[0])
                    uniq.append(item)
            projected = uniq

        for pos in reversed(range(len(sel.order_by))):
            desc = sel.order_by[pos][1]
            projected.sort(key=lambda item, p=pos: sort_wrap(item[1][p]), reverse=desc)

        start = sel.offset or 0
        end = None if sel.limit is None else start + sel.limit
        sliced = projected[start:end]
        return cols, [row for row, _keys in sliced]

    def _scan(self, meta, scan):
        types = meta.types()
        if scan["type"] == "pk":
            if scan["value"] is None:
                return []
            try:
                key = self.db.coerce_pk(scan["value"])
            except SQLError:
                return []
            blob = self.db.btree.search(meta.root_page, key)
            if blob is None:
                return []
            return [(key, rows_mod.decode_row(types, blob))]
        if scan["type"] == "index":
            return self.db.index_lookup(meta, scan["index_col"], scan["value"])
        return list(self.db.scan_table(meta))

    def _join(self, left_parts_list, jinfo, scope):
        meta = jinfo["meta"]
        jtype = jinfo["jtype"]
        on_fn = compile_expr(jinfo["on"], scope)
        right_rows = list(self.db.scan_table(meta))
        left_width = scope.nslots() - 1
        out = []
        matched_left = set()
        matched_right = set()

        def match(parts, left_i, rid_r, rrow):
            cand = parts + [rrow]
            if on_fn(cand, None) is True:
                out.append(cand)
                matched_left.add(left_i)
                matched_right.add(rid_r)
                return True
            return False

        use_hash = jinfo["algo"] == "HASH JOIN" and jinfo["equi"]
        if use_hash:
            equi = jinfo["equi"]
            acc_fn = compile_expr(equi["acc"], scope)
            nidx = meta.column_index(equi["new"].name)
            if jinfo["build_side"] == "right":
                table = {}
                for rid_r, rrow in right_rows:
                    k = rrow[nidx]
                    if k is not None:
                        table.setdefault(k, []).append((rid_r, rrow))
                for i, parts in enumerate(left_parts_list):
                    lk = acc_fn(parts, None)
                    if lk is not None:
                        for rid_r, rrow in table.get(lk, []):
                            match(parts, i, rid_r, rrow)
            else:
                table = {}
                for i, parts in enumerate(left_parts_list):
                    lk = acc_fn(parts, None)
                    if lk is not None:
                        table.setdefault(lk, []).append((i, parts))
                for rid_r, rrow in right_rows:
                    rk = rrow[nidx]
                    if rk is not None:
                        for i, parts in table.get(rk, []):
                            match(parts, i, rid_r, rrow)
        else:
            for i, parts in enumerate(left_parts_list):
                for rid_r, rrow in right_rows:
                    match(parts, i, rid_r, rrow)

        if jtype in ("LEFT", "FULL"):
            for i, parts in enumerate(left_parts_list):
                if i not in matched_left:
                    out.append(parts + [None])
        if jtype in ("RIGHT", "FULL"):
            for rid_r, rrow in right_rows:
                if rid_r not in matched_right:
                    out.append([None] * left_width + [rrow])
        return out

    def _aggregate(self, sel, filtered, scope):
        func_nodes = []
        seen_ids = set()

        def collect(e):
            for f in walk_func_nodes(e):
                if id(f) not in seen_ids:
                    seen_ids.add(id(f))
                    func_nodes.append(f)

        for expr, _label in sel.items:
            collect(expr)
        if sel.having is not None:
            collect(sel.having)
        for expr, _d in sel.order_by:
            collect(expr)

        compiled_aggs = []
        for f in func_nodes:
            argfn = None if f.star else compile_expr(f.arg, scope)
            compiled_aggs.append((id(f), f.name, f.star, argfn, getattr(f, "distinct", False)))

        group_key_fns = [compile_expr(g, scope) for g in sel.group_by]
        having_fn = None
        if sel.having is not None:
            having_fn = compile_expr(sel.having, scope,
                                     agg_aliases=scope.agg_aliases, allow_agg=True)

        groups = {}
        order = []
        if group_key_fns:
            for parts in filtered:
                key = tuple(_group_key_value(fn(parts, None)) for fn in group_key_fns)
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(parts)
        else:
            groups[()] = list(filtered)
            order.append(())

        empty_parts = [None] * scope.nslots()
        units = []
        for key in order:
            members = groups[key]
            agg_env = {}
            for fid, name, star, argfn, distinct in compiled_aggs:
                if star:
                    agg_env[fid] = len(members)
                    continue
                values = [v for v in (argfn(p, None) for p in members) if v is not None]
                if distinct:
                    values = list(dict.fromkeys(values))
                agg_env[fid] = _accumulate(name, values)
            rep = members[0] if members else empty_parts
            if having_fn is not None and having_fn(rep, agg_env) is not True:
                continue
            units.append({"parts": rep, "rows_ctx": members, "agg_env": agg_env})
        return units
