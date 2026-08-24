import sys

from .engine import Database, SQLError
from . import bench


def fmt_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, float):
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return s
    return str(v)


def format_table(cols, rows, indent=""):
    if not cols:
        return ""
    cells = [[fmt_value(v) for v in r] for r in rows]
    widths = [len(c) for c in cols]
    numeric = [True] * len(cols)
    for r in cells:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
            if v != "NULL":
                try:
                    float(v)
                except ValueError:
                    numeric[i] = False

    def line(ch="-"):
        return indent + "+" + "+".join(ch * (w + 2) for w in widths) + "+"

    def render(vals):
        out = []
        for i, v in enumerate(vals):
            if numeric[i]:
                out.append(" " + v.rjust(widths[i]) + " ")
            else:
                out.append(" " + v.ljust(widths[i]) + " ")
        return indent + "|" + "|".join(out) + "|"

    lines = [line(), render(cols), line()]
    for r in cells:
        lines.append(render(r))
    lines.append(line())
    n = len(rows)
    lines.append(indent + f"{n} row(s)")
    return "\n".join(lines)


HELP = """\
meta commands:
  .tables              list tables
  .schema TABLE        show table columns
  .btree TABLE         B+ tree stats (depth, leaves, fill)
  .check TABLE         validate B+ tree invariants (balance, separators, occupancy)
  .explain SQL         print the query plan for a SELECT
  .stats               buffer pool / engine stats
  .benchmark           run the built-in benchmark suite
  .vacuum TABLE        rebuild a table's tree compactly
  .quit                exit
everything else is SQL; end statements with ';'"""


def run_sql(db, sql, stream=None):
    out = stream or sys.stdout
    try:
        results = db.execute_script(sql)
    except SQLError as e:
        print(f"Error: {e}", file=out)
        return False
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=out)
        return False
    for res in results:
        if res.cols and res.rows:
            print(format_table(res.cols, res.rows), file=out)
        elif res.message:
            print(res.message, file=out)
        if res.elapsed_ms >= 0.05 and (res.rows or res.message):
            print(f"({res.elapsed_ms:.1f} ms)", file=out)
    return True


def handle_meta(db, cmd, stream=None):
    out = stream or sys.stdout
    parts = cmd.split()
    name = parts[0].lower()
    if name in (".quit", ".exit"):
        raise SystemExit(0)
    if name == ".help":
        print(HELP, file=out)
    elif name == ".tables":
        tables = sorted(db.catalog.tables)
        if not tables:
            print("(no tables)", file=out)
        else:
            for t in tables:
                m = db.catalog.tables[t]
                print(f"  {t}  ({m.row_count} rows)", file=out)
    elif name == ".schema":
        if len(parts) < 2:
            print("usage: .schema TABLE", file=out)
            return
        m = db.catalog.get(parts[1])
        pk = m.pk_column()
        for c in m.columns:
            mark = " PRIMARY KEY" if c["name"] == pk else ""
            idx = f"  [indexed: {m.indexes[c['name']]}]" if c["name"] in m.indexes else ""
            print(f"  {c['name']:<12} {c['type']}{mark}{idx}", file=out)
    elif name == ".btree":
        if len(parts) < 2:
            print("usage: .btree TABLE", file=out)
            return
        m = db.catalog.get(parts[1])
        st = db.btree.stats(m.root_page)
        print(
            f"  depth={st['depth']} leaves={st['leaves']} internal={st['internal_nodes']} "
            f"keys={st['keys']} avg_leaf_fill={st['avg_fill']}",
            file=out,
        )
    elif name == ".check":
        if len(parts) < 2:
            print("usage: .check TABLE   (validate B+ tree invariants)", file=out)
            return
        m = db.catalog.get(parts[1])
        try:
            res = db.btree.check(m.root_page)
        except AssertionError as e:
            print(f"  INVARIANT VIOLATION: {e}", file=out)
            return
        print(f"  OK: {res['keys']} keys, balanced={res['balanced']}", file=out)
    elif name == ".stats":
        st = db.stats()
        print(f"  tables: {', '.join(st['tables']) or '-'}", file=out)
        p = st["pager"]
        print(
            f"  cache: {p['cached_pages']} pages cached, {p['dirty_pages']} dirty, "
            f"hit rate {p['hit_rate']} ({p['cache_hits']}/{p['cache_hits'] + p['cache_misses']})",
            file=out,
        )
        print(f"  file: {p['file_pages']} pages x 4096 bytes", file=out)
        print(f"  wal pending: {db.wal.pending_bytes()} bytes", file=out)
    elif name == ".explain":
        sql = cmd[len(".explain"):].strip()
        if not sql:
            print("usage: .explain SELECT ...", file=out)
            return
        run_sql(db, "EXPLAIN " + sql, stream=out)
    elif name == ".benchmark":
        bench.run_report()
    elif name == ".vacuum":
        if len(parts) < 2:
            print("usage: .vacuum TABLE", file=out)
            return
        msg = db.vacuum(parts[1])
        print(msg, file=out)
    else:
        print(f"unknown command {name!r} (.help for help)", file=out)


def main(db_path="leafdb.db", stream_in=None, stream_out=None):
    out = stream_out or sys.stdout
    if stream_in is not None:
        inp = stream_in
    else:
        def inp(_prompt):
            return sys.stdin.readline()
    print("LeafDB — a tiny relational database (python, stdlib-only)", file=out)
    print(f'database: "{db_path}"   type ".help" for meta commands', file=out)
    db = Database(db_path)
    buffer = ""
    try:
        while True:
            prompt = "leafdb> " if not buffer else "  ....> "
            try:
                line = inp(prompt)
            except EOFError:
                break
            stripped = line.strip()
            if not buffer and stripped.startswith("."):
                handle_meta(db, stripped, stream=out)
                continue
            if not buffer and not stripped:
                continue
            buffer += line + "\n"
            if stripped.endswith(";"):
                run_sql(db, buffer, stream=out)
                buffer = ""
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        db.close()
        print("bye", file=out)


def console_main():
    path = sys.argv[1] if len(sys.argv) > 1 else "leafdb.db"
    main(path)


if __name__ == "__main__":
    console_main()
