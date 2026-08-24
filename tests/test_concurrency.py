import os
import random
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

os.environ["LEAFDB_FSYNC"] = "0"

from leafdb.engine import Database


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_conc_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        path = os.path.join(self.tmp, "conc.db")
        self.db = Database(path)
        self.addCleanup(self.db.close)

    def test_parallel_writers_and_readers(self):
        self.db.execute(
            "CREATE TABLE events (id INT PRIMARY KEY, thread INT, seq INT)"
        )
        writers = 8
        per_writer = 150

        def write_work(wid):
            rng = random.Random(wid)
            for s in range(per_writer):
                rid = wid * 100000 + s
                if rng.random() < 0.05:
                    try:
                        with self.db._lock:
                            pass
                    except Exception:
                        pass
                self.db.execute(
                    f"INSERT INTO events VALUES ({rid}, {wid}, {s})"
                )

        def read_work():
            for _ in range(120):
                res = self.db.execute("SELECT COUNT(*) AS n FROM events")
                n = res.rows[0][0]
                self.assertGreaterEqual(n, 0)

        with ThreadPoolExecutor(max_workers=writers + 3) as pool:
            futures = [pool.submit(write_work, w) for w in range(writers)]
            futures += [pool.submit(read_work) for _ in range(3)]
            for f in futures:
                f.result(timeout=120)

        total = self.db.execute("SELECT COUNT(*) AS n FROM events").rows[0][0]
        self.assertEqual(total, writers * per_writer)

        distinct = self.db.execute(
            "SELECT COUNT(DISTINCT id) AS d FROM events"
        ).rows[0][0]
        self.assertEqual(distinct, writers * per_writer)

        meta = self.db.lookup_meta("events")
        self.db.btree.check(meta.root_page)

    def test_concurrent_transactions_serialize(self):
        self.db.execute("CREATE TABLE acct (id INT PRIMARY KEY, bal INT)")
        self.db.execute("INSERT INTO acct VALUES (1, 1000), (2, 1000)")

        def transfer_loop(src, dst, times):
            for _ in range(times):
                with self.db._lock:
                    self.db.execute_script("BEGIN")
                    b1 = self.db.execute(f"SELECT bal FROM acct WHERE id = {src}").rows[0][0]
                    b2 = self.db.execute(f"SELECT bal FROM acct WHERE id = {dst}").rows[0][0]
                    if b1 > 10:
                        self.db.execute(f"UPDATE acct SET bal = {b1 - 10} WHERE id = {src}")
                        self.db.execute(f"UPDATE acct SET bal = {b2 + 10} WHERE id = {dst}")
                    self.db.execute_script("COMMIT")

        threads = [
            threading.Thread(target=transfer_loop, args=(a, b, 40))
            for a, b in ((1, 2), (2, 1))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        rows = self.db.execute("SELECT id, bal FROM acct ORDER BY id").rows
        total = rows[0][1] + rows[1][1]
        self.assertEqual(total, 2000, "money leaked between accounts")

    def test_shared_connection_across_threads_is_safe(self):
        self.db.execute("CREATE TABLE kv (k INT PRIMARY KEY, v TEXT)")
        errors = []

        def hammer(tid):
            try:
                for i in range(80):
                    k = tid * 1000 + i
                    self.db.execute(f"INSERT INTO kv VALUES ({k}, 'v{tid}')")
                    self.db.execute(f"SELECT v FROM kv WHERE k = {k}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=hammer, args=(t,)) for t in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        n = self.db.execute("SELECT COUNT(*) AS n FROM kv").rows[0][0]
        self.assertEqual(n, 480)


if __name__ == "__main__":
    unittest.main(verbosity=2)
