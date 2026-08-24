import os

from leafdb.bench import (
    bench_join_group_order,
    bench_point_lookups,
    bench_range_scan,
    seed_bench_db,
)
from leafdb.engine import Database


def make_db(tmp, name="bench.db", users=200, orders=20000):
    path = os.path.join(tmp, name)
    return seed_bench_db(path, n_users=users, n_orders=orders, verbose=False)


def test_point_lookups_are_fast(tmp_path):
    db = make_db(str(tmp_path))
    try:
        r = bench_point_lookups(db, samples=100)
        assert r["avg_ms"] < 5.0
        assert r["sample_hit"].startswith("user_")
    finally:
        db.close()


def test_range_scan_count(tmp_path):
    db = make_db(str(tmp_path))
    try:
        r = bench_range_scan(db)
        assert 9000 <= r["rows"] <= 9900
    finally:
        db.close()


def test_join_group_order(tmp_path):
    db = make_db(str(tmp_path), orders=4000)
    try:
        r = bench_join_group_order(db)
        assert len(r["rows"]) == 5
        totals = [row[2] for row in r["rows"]]
        assert totals == sorted(totals, reverse=True)
        assert all(isinstance(t, int) and t >= 0 for t in totals)
    finally:
        db.close()
