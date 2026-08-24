import os
import random
import time


CITIES = [
    "Mumbai", "Delhi", "Bangalore", "Chennai", "Kolkata", "Hyderabad",
    "Pune", "Ahmedabad", "Jaipur", "Lucknow",
]


def seed_bench_db(path, n_users=200, n_orders=20000, verbose=True):
    from .engine import Database
    for suffix in ("", ".wal"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    db = Database(path)
    db.execute_script(
        "CREATE TABLE users(id INT PRIMARY KEY, name TEXT, city TEXT);\n"
        "CREATE TABLE orders(id INT PRIMARY KEY, user_id INT, amount INT);"
    )
    rng = random.Random(42)
    t0 = time.perf_counter()
    db.execute_script("BEGIN;")
    stmts = []
    for i in range(1, n_users + 1):
        city = CITIES[i % len(CITIES)]
        stmts.append(f"INSERT INTO users VALUES ({i}, 'user_{i:04d}', '{city}')")
    db.execute_script(";\n".join(stmts) + ";")
    db.execute_script("COMMIT;")
    db.execute_script("BEGIN;")
    stmts = []
    for i in range(1, n_orders + 1):
        uid = rng.randint(1, n_users)
        amt = rng.randint(5, 500)
        stmts.append(f"INSERT INTO orders VALUES ({i}, {uid}, {amt})")
    for j in range(0, len(stmts), 2000):
        db.execute_script(";\n".join(stmts[j:j + 2000]) + ";")
    db.execute_script("COMMIT;")
    seed_ms = (time.perf_counter() - t0) * 1000.0
    if verbose:
        print(f"  seeded {n_users} users + {n_orders} orders in {seed_ms:.0f} ms")
    return db


def bench_point_lookups(db, samples=300):
    rng = random.Random(7)
    keys = [rng.randint(1, 200) for _ in range(samples)]
    times = []
    last = None
    for k in keys:
        t0 = time.perf_counter()
        res = db.execute(f"SELECT name FROM users WHERE id = {k}")
        times.append((time.perf_counter() - t0) * 1000.0)
        if res.rows:
            last = res.rows[0][0]
    times.sort()
    avg = sum(times) / len(times)
    p95 = times[int(len(times) * 0.95)]

    db.execute("SELECT name FROM users WHERE id = 42")
    warm_times = []
    for _ in range(samples):
        t0 = time.perf_counter()
        db.execute("SELECT name FROM users WHERE id = 42")
        warm_times.append((time.perf_counter() - t0) * 1000.0)
    warm_avg = sum(warm_times) / len(warm_times)

    t0 = time.perf_counter()
    for _ in range(1000):
        blob = db.btree.search(db.lookup_meta("users").root_page, 42)
    raw_ms = (time.perf_counter() - t0)
    return {
        "samples": samples,
        "avg_ms": avg,
        "p95_ms": p95,
        "warm_ms": warm_avg,
        "raw_us_per_search": raw_ms * 1000.0,
        "sample_hit": last,
    }


def bench_range_scan(db):
    t0 = time.perf_counter()
    res = db.execute("SELECT COUNT(*) AS n FROM orders WHERE id >= 100 AND id <= 9900")
    ms = (time.perf_counter() - t0) * 1000.0
    return {"rows": res.rows[0][0], "ms": ms}


def bench_join_group_order(db):
    sql = (
        "SELECT u.city AS city, COUNT(*) AS cnt, SUM(o.amount) AS total "
        "FROM orders o JOIN users u ON o.user_id = u.id "
        "GROUP BY u.city ORDER BY total DESC LIMIT 5"
    )
    t0 = time.perf_counter()
    res = db.execute(sql)
    ms = (time.perf_counter() - t0) * 1000.0
    return {"ms": ms, "cols": res.cols, "rows": res.rows}


def run_report(path=None, n_orders=20000):
    import tempfile
    from .pager import PAGE_SIZE

    if path is None:
        fd, path = tempfile.mkstemp(prefix="leafdb_bench_", suffix=".db")
        os.close(fd)
        cleanup = True
    else:
        cleanup = False
    try:
        print(f"LeafDB benchmark ({n_orders} orders)")
        db = seed_bench_db(path, n_orders=n_orders)
        lk = bench_point_lookups(db)
        print(f"\npoint lookups (pk = const), {lk['samples']} samples:")
        print(f"  ad-hoc SQL    avg {lk['avg_ms']:.3f} ms   p95 {lk['p95_ms']:.3f} ms")
        print(f"  cached stmt   avg {lk['warm_ms']:.3f} ms")
        print(f"  raw btree     {lk['raw_us_per_search']:.1f} us/search")

        rs = bench_range_scan(db)
        print(f"\nrange scan COUNT(*) id >= 100 AND id <= 9900:")
        print(f"  {rs['rows']} rows in {rs['ms']:.1f} ms")

        jg = bench_join_group_order(db)
        print("\nhash join + GROUP BY + ORDER BY (orders x users):")
        print(f"  completed in {jg['ms']:.1f} ms; top rows:")
        from .repl import format_table
        print(format_table(jg["cols"], jg["rows"], indent="    "))
        st = db.stats()["pager"]
        print(f"\nbuffer pool: hit rate {st['hit_rate']} over {st['cache_hits'] + st['cache_misses']} accesses")
        db.close()
        size_kb = os.path.getsize(path) / 1024
        pages = size_kb * 1024 / PAGE_SIZE
        print(f"file after checkpoint: {size_kb:.0f} KB ({pages:.0f} x {PAGE_SIZE}-byte pages)")
    finally:
        if cleanup:
            for suffix in ("", ".wal"):
                p = path + suffix
                if os.path.exists(p):
                    os.remove(p)


if __name__ == "__main__":
    run_report()
