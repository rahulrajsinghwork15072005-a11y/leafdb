from .sqlparse import BinOp, Column, Func, IsNull, Literal, UnaryOp, walk_func_nodes


def flatten_and(expr):
    if isinstance(expr, BinOp) and expr.op == "AND":
        return flatten_and(expr.left) + flatten_and(expr.right)
    return [expr]


def _column_matches(col, alias, colnames):
    if not isinstance(col, Column):
        return False
    if col.table is not None:
        return col.table == alias and col.name in colnames
    return col.name in colnames


def find_equi_predicates(conjuncts):
    out = []
    for c in conjuncts:
        if isinstance(c, BinOp) and c.op == "=":
            if isinstance(c.left, Column) and isinstance(c.right, Literal):
                out.append((c.left, c.right.value))
            elif isinstance(c.left, Literal) and isinstance(c.right, Column):
                out.append((c.right, c.left.value))
    return out


class Plan:
    def __init__(self):
        self.scan = None
        self.joins = []
        self.filters = []
        self.steps = []

    def explain_lines(self):
        return list(self.steps)


def _conjunct_tables(conj, sources):
    """Set of aliases a conjunct references against {alias: meta}, or None if
    any column fails to resolve unambiguously."""
    from .sqlparse import Between, InList, IsNull, Like, UnaryOp, walk_func_nodes

    if walk_func_nodes(conj):
        return None
    cols = []

    def gather(e):
        if isinstance(e, Column):
            cols.append(e)
        elif isinstance(e, BinOp):
            gather(e.left)
            gather(e.right)
        elif isinstance(e, UnaryOp):
            gather(e.operand)
        elif isinstance(e, IsNull):
            gather(e.operand)
        elif isinstance(e, Between):
            gather(e.operand)
            gather(e.lo)
            gather(e.hi)
        elif isinstance(e, InList):
            gather(e.operand)
            for it in e.items:
                gather(it)
        elif isinstance(e, Like):
            gather(e.operand)

    gather(conj)
    aliases = set()
    for col in cols:
        if col.table is not None:
            meta = sources.get(col.table)
            if meta is None or col.name not in meta.colnames():
                return None
            aliases.add(col.table)
        else:
            hits = [a for a, m in sources.items() if col.name in m.colnames()]
            if len(hits) != 1:
                return None
            aliases.add(hits[0])
    return aliases


def assign_filter_stages(sel, lookup_meta):
    """Map each WHERE conjunct to the earliest pipeline stage where every
    table it references has been joined. Stage 0 = right after the base scan;
    stage k = after k joins. Unresolvable/aggregate conjuncts go to the last
    stage so errors surface exactly where they did before pushdown existed."""
    base = lookup_meta(sel.table)
    base_alias = sel.alias or sel.table
    full = {base_alias: base}
    for j in sel.joins:
        full[j.alias or j.table] = lookup_meta(j.table)

    avail = [{base_alias: base}]
    running = {base_alias: base}
    for j in sel.joins:
        running[j.alias or j.table] = lookup_meta(j.table)
        avail.append(dict(running))

    total_stages = len(sel.joins)
    staged = {}
    for conj in flatten_and(sel.where) if sel.where is not None else []:
        stage = total_stages
        if not walk_func_nodes(conj) and _conjunct_tables(conj, full) is not None:
            for s in range(total_stages + 1):
                refs = _conjunct_tables(conj, avail[s])
                if refs is not None and refs <= set(avail[s]):
                    stage = s
                    break
        staged.setdefault(stage, []).append(conj)
    return staged


def build_select_plan(sel, lookup_meta):
    base = lookup_meta(sel.table)
    base_alias = sel.alias or sel.table
    plan = Plan()
    known = {base_alias: base}
    scan = {
        "table": sel.table,
        "alias": base_alias,
        "meta": base,
        "type": "seq",
        "index_col": None,
        "value": None,
        "est_rows": max(base.row_count, 1),
    }

    conjuncts = flatten_and(sel.where) if sel.where is not None else []
    eqs = find_equi_predicates(conjuncts)
    pk_col = base.pk_column()
    for col, value in eqs:
        if pk_col and _column_matches(col, base_alias, base.colnames()) and col.name == pk_col:
            scan["type"] = "pk"
            scan["index_col"] = pk_col
            scan["value"] = value
            scan["est_rows"] = 1
            break
    if scan["type"] == "seq":
        for col, value in eqs:
            if _column_matches(col, base_alias, base.colnames()) and col.name in base.indexes:
                scan["type"] = "index"
                scan["index_col"] = col.name
                scan["value"] = value
                scan["est_rows"] = max(1, base.row_count // 100)
                break

    if scan["type"] == "pk":
        plan.steps.append(
            f"PK LOOKUP {scan['table']} AS {base_alias} ON {pk_col}={scan['value']!r} (est 1 row)"
        )
    elif scan["type"] == "index":
        plan.steps.append(
            f"INDEX SCAN {scan['table']} AS {base_alias} USING idx_{scan['index_col']} "
            f"({scan['index_col']}={scan['value']!r}) (est {scan['est_rows']} rows)"
        )
    else:
        plan.steps.append(f"SEQ SCAN {scan['table']} AS {base_alias} (est {scan['est_rows']} rows)")
    plan.scan = scan

    left_est = scan["est_rows"]
    for j in sel.joins:
        jmeta = lookup_meta(j.table)
        jalias = j.alias or j.table
        right_est = max(jmeta.row_count, 1)
        on_conjuncts = flatten_and(j.on)
        equi = find_join_equi(on_conjuncts, known, jalias, jmeta)
        if equi:
            algo = "HASH JOIN"
            build_side = "right" if right_est <= left_est else "left"
            est_out = max(left_est, right_est)
            detail = (f"keys {_expr_text(equi['acc'])}={_expr_text(equi['new'])}, "
                      f"build {build_side} ({min(left_est, right_est)} rows)")
        else:
            algo = "NESTED LOOP JOIN"
            build_side = None
            est_out = max(1, left_est * right_est // 10)
            detail = f"cost ~{left_est * right_est} comparisons"
        plan.joins.append({
            "jtype": j.jtype,
            "table": j.table,
            "alias": jalias,
            "meta": jmeta,
            "on": j.on,
            "algo": algo,
            "equi": equi,
            "build_side": build_side,
        })
        plan.steps.append(
            f"{algo} [{j.jtype}] {j.table} AS {jalias} ON {_expr_text(j.on)} ({detail}; "
            f"est in {left_est}x{right_est})"
        )
        known[jalias] = jmeta
        left_est = est_out

    n_residual_total = 0
    staged = assign_filter_stages(sel, lookup_meta)
    for stage in sorted(staged):
        for conj in staged[stage]:
            plan.filters.append((stage, conj))
            if stage == len(sel.joins):
                n_residual_total += 1
            else:
                plan.steps.append(f"PUSHDOWN FILTER {_expr_text(conj)} (applied after {stage} join(s))")
    if n_residual_total > 0:
        plan.steps.append(f"FILTER WHERE ({n_residual_total} residual predicate(s), 3-valued logic)")
    if sel.group_by or has_aggregate(sel):
        keys = ", ".join(_expr_text(g) for g in sel.group_by) if sel.group_by else "global group"
        aggs = aggregate_names(sel)
        step = f"GROUP AGGREGATE keys=({keys}) aggs=({aggs})"
        if sel.having is not None:
            step += f" HAVING {_expr_text(sel.having)}"
        plan.steps.append(step)
    if sel.order_by:
        terms = ", ".join(f"{_expr_text(e)} {'DESC' if d else 'ASC'}" for e, d in sel.order_by)
        plan.steps.append(f"SORT {terms}")
    if sel.distinct:
        plan.steps.append("DISTINCT")
    if sel.limit is not None or sel.offset:
        lim = sel.limit if sel.limit is not None else "all"
        plan.steps.append(f"LIMIT {lim} OFFSET {sel.offset or 0}")
    return plan


def find_join_equi(on_conjuncts, known, ralias, rmeta):
    def owner(col):
        if col.table is not None:
            if col.table == ralias:
                return ralias if col.name in rmeta.colnames() else None
            meta = known.get(col.table)
            if meta and col.name in meta.colnames():
                return col.table
            return None
        hits = []
        for alias, meta in list(known.items()) + [(ralias, rmeta)]:
            if col.name in meta.colnames():
                hits.append(alias)
        return hits[0] if len(hits) == 1 else None

    for c in on_conjuncts:
        if not (isinstance(c, BinOp) and c.op == "="):
            continue
        if not (isinstance(c.left, Column) and isinstance(c.right, Column)):
            continue
        lo = owner(c.left)
        ro = owner(c.right)
        if lo is None or ro is None or lo == ro:
            continue
        if ro == ralias:
            acc, new = c.left, c.right
            acc_owner = lo
        elif lo == ralias:
            acc, new = c.right, c.left
            acc_owner = ro
        else:
            continue
        return {
            "acc": Column(acc.name, table=acc_owner),
            "new": Column(new.name, table=ralias),
        }
    return None


def has_aggregate(sel):
    nodes = []
    for expr, _ in sel.items:
        nodes.extend(walk_func_nodes(expr))
    if sel.having is not None:
        nodes.extend(walk_func_nodes(sel.having))
    for e, _d in sel.order_by:
        nodes.extend(walk_func_nodes(e))
    return bool(nodes)


def aggregate_names(sel):
    names = []
    for expr, _ in sel.items:
        for f in walk_func_nodes(expr):
            names.append(f"{f.name}(*)" if f.star else f"{f.name}({getattr(f.arg, 'name', '?')})")
    seen = list(dict.fromkeys(names))
    return ", ".join(seen) if seen else "-"


def _expr_text(e):
    if isinstance(e, Column):
        return f"{e.table + '.' if e.table else ''}{e.name}"
    if isinstance(e, Literal):
        return repr(e.value)
    if isinstance(e, BinOp):
        return f"{_expr_text(e.left)} {e.op} {_expr_text(e.right)}"
    if isinstance(e, UnaryOp):
        return f"{e.op}({_expr_text(e.operand)})" if e.op != "NOT" else f"NOT {_expr_text(e.operand)}"
    if isinstance(e, IsNull):
        return f"{_expr_text(e.operand)} IS {'NOT ' if e.negated else ''}NULL"
    if isinstance(e, Func):
        return f"{e.name}(*)" if e.star else f"{e.name}({_expr_text(e.arg)})"
    return "?"
