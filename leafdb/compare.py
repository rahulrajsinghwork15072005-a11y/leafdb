import os
import random
import tempfile
import time

import sqlite3

from .engine import Database

CITIES = ["Mumbai", "Delhi", "Bangalore", "Chennai", "Kolkata", "Hyderabad",
          "Pune", "Ahmedabad", "Jaipur", "Lucknow"]


def _timed(fn):
    t0 = time.perf_counter()
    result = fn()
    return (time.perf_counter() - t0) * 1000.0, result


def compare(n_users=200, n_orders=20000):
    tmp = tempfile.mkdtemp(prefix="leafdb_compare_")
    rng = random.Random(42)
    users = [(i, f"user_{i:04d}", CITIES[i % len(CITIES)]) for i in range(1, n_users + 1)]
    orders = [(i, rng.randint(1, n_users), rng.randint(5, 500)) for i in range(1, n_orders + 1)]

    ldb_path = os.path.join(tmp, "leaf.db")
    s3_path = os.path.join(tmp, "sq.db")

    def seed_leafdb():
        db = Database(ldb_path)
        db.execute_script(
            "CREATE TABLE users(id INT PRIMARY KEY, name TEXT, city TEXT);\n"
            "CREATE TABLE orders(id INT PRIMARY KEY, user_id INT, amount INT);")
        user_stmts = [f"INSERT INTO users VALUES ({r[0]}, '{r[1]}', '{r[2]}')" for r in users]
        order_stmts = [f"INSERT INTO orders VALUES ({r[0]}, {r[1]}, {r[2]})" for r in orders]
        for stmts in (user_stmts, order_stmts):
            db.execute_script("BEGIN;")
            for i in range(0, len(stmts), 2000):
                db.execute_script(";\n".join(stmts[i:i + 2000]) + ";")
            db.execute_script("COMMIT;")
        return db

    def seed_sqlite():
        conn = sqlite3.connect(s3_path)
        cur = conn.cursor()
        cur.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, city TEXT)")
        cur.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INT, amount INT)")
        cur.executemany("INSERT INTO users VALUES (?,?,?)", users)
        cur.executemany("INSERT INTO orders VALUES (?,?,?)", orders)
        conn.commit()
        return conn, cur

    ms_seed_leaf, ldb = _timed(seed_leafdb)
    ms_seed_s3, (s3, s3cur) = _timed(seed_sqlite)

    JOIN_Q = ("SELECT u.city, COUNT(*) AS cnt, SUM(o.amount) AS total "
              "FROM orders o JOIN users u ON o.user_id = u.id "
              "GROUP BY u.city ORDER BY total DESC LIMIT 5")
    RANGE_Q = "SELECT COUNT(*) FROM orders WHERE id >= 100 AND id <= 9900"

    results = {}

    ms_l, _ = _timed(lambda: [ldb.execute(f"SELECT name FROM users WHERE id = {k}") for k in range(1, 201)])
    ms_s, _ = _timed(lambda: [s3cur.execute("SELECT name FROM users WHERE id = ?", (k,)).fetchall()
                              for k in range(1, 201)])
    results["point lookups x200 ad-hoc"] = (ms_l, ms_s)

    ldb.execute("SELECT name FROM users WHERE id = 42")
    ms_l, _ = _timed(lambda: [ldb.execute("SELECT name FROM users WHERE id = 42") for _ in range(200)])
    ms_s, _ = _timed(lambda: [s3cur.execute(
        "SELECT name FROM users WHERE id = ?", (42,)).fetchall() for _ in range(200)])
    results["point lookups x200 prepared"] = (ms_l, ms_s)

    ms_l, r1 = _timed(lambda: ldb.execute(RANGE_Q).rows[0][0])
    ms_s, r2 = _timed(lambda: s3cur.execute(RANGE_Q).fetchone()[0])
    assert r1 == r2, (r1, r2)
    results["range scan COUNT (9.8k rows)"] = (ms_l, ms_s)

    ms_l, rows_l = _timed(lambda: ldb.execute(JOIN_Q).rows)
    ms_s, rows_s = _timed(lambda: s3cur.execute(JOIN_Q).fetchall())
    assert sorted(r[2] for r in rows_l) == sorted(r[2] for r in rows_s), "join results differ"
    results["hash join + GROUP BY + ORDER"] = (ms_l, ms_s)

    print(f"\nLeafDB vs sqlite3  ({n_orders:,} orders, {n_users} users)")
    print(f"  {'workload':<36}{'LeafDB':>11}{'sqlite3':>11}{'ratio':>9}")
    print("  " + "-" * 67)
    for name, (l, s) in results.items():
        print(f"  {name:<36}{l:>9.1f}ms{s:>9.1f}ms{l / s:>8.1f}x")
    print("  " + "-" * 67)
    print(f"  bulk insert+commit of both tables: LeafDB {ms_seed_leaf:.0f}ms, "
          f"sqlite3 {ms_seed_s3:.1f}ms")

    ldb.close()
    s3.close()
    for suffix in ("", ".wal"):
        try:
            os.remove(ldb_path + suffix)
        except OSError:
            pass
    os.remove(s3_path)


if __name__ == "__main__":
    compare()
