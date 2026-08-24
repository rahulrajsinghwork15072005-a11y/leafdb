import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leafdb.engine import Database
from leafdb.repl import format_table


def section(title):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def show(db, sql, note=None):
    if note:
        print(f"\n-- {note}")
    print(f"leafdb> {sql}")
    res = db.execute(sql)
    if res.cols and (res.rows or True):
        body = format_table(res.cols, res.rows)
        lines = body.splitlines()
        if len(lines) > 14:
            print("\n".join(lines[:7]))
            print(f"{lines[0]}  ... {len(res.rows) - 8} rows omitted ...")
            print("\n".join(lines[-4:]))
        else:
            print(body)
    elif res.message:
        print(res.message)


def main():
    tmpdir = tempfile.mkdtemp(prefix="leafdb_demo_")
    path = os.path.join(tmpdir, "demo.db")
    db = Database(path)

    section("1. Schema + inserts (typed rows in 4 KB pages)")
    db.execute_script(
        """
        CREATE TABLE users (id INT PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, amount INT);
        INSERT INTO users VALUES (1,'ada','pune'),(2,'grace','delhi'),
                                 (3,'linus','pune'),(4,'barbara','goa');
        INSERT INTO orders VALUES (100,1,250),(101,1,120),(102,2,90),
                                  (103,3,NULL),(104,9,60);
        """
    )
    show(db, "SELECT * FROM users ORDER BY id")

    section("2. NULL-aware filtering (three-valued logic)")
    show(db, "SELECT id, user_id FROM orders WHERE user_id > 1 ORDER BY id",
         "NULL user_id is UNKNOWN -> excluded")
    show(db, "SELECT id, amount FROM orders WHERE amount IS NULL OR amount < 100 ORDER BY id")

    section("3. Aggregation: GROUP BY + HAVING + ORDER BY")
    show(db,
         "SELECT u.city AS city, COUNT(*) AS cnt, SUM(o.amount) AS total "
         "FROM orders o JOIN users u ON o.user_id = u.id "
         "GROUP BY u.city HAVING total >= 100 ORDER BY total DESC")

    section("4. The planner picks a hash join + PK lookup")
    show(db, "EXPLAIN SELECT u.name FROM orders o JOIN users u ON o.user_id = u.id WHERE o.id = 100")
    show(db, "SELECT name FROM users WHERE id = 3", "PK LOOKUP plan for point queries")

    section("5. Secondary index")
    db.execute("CREATE INDEX idx_city ON users(city)")
    show(db, "EXPLAIN SELECT * FROM users WHERE city = 'pune'")
    show(db, "SELECT name FROM users WHERE city = 'pune'")

    section("6. Transactions: rollback undoes everything")
    db.execute_script("BEGIN; DELETE FROM users; ROLLBACK;")
    show(db, "SELECT COUNT(*) AS users_left FROM users")

    section("7. B+ tree internals")
    meta = db.lookup_meta("users")
    print(" ", db.btree.stats(meta.root_page))
    print("  leaf chain:", [k for k, _ in db.btree.range_scan(meta.root_page)])

    section("8. Crash safety: WAL replay on restart")
    db.execute("INSERT INTO users VALUES (77, 'survivor', 'delhi')")
    db.close(checkpoint=False)
    print("  simulated crash (no checkpoint). Reopening...")
    db = Database(path)
    show(db, "SELECT name FROM users WHERE id = 77", "row recovered from WAL")

    section("9. Buffer pool stats")
    for k, v in db.stats()["pager"].items():
        print(f"  {k}: {v}")

    db.close()
    print("\ndemo complete.")


if __name__ == "__main__":
    main()
