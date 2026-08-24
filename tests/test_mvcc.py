import os
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

os.environ["LEAFDB_FSYNC"] = "0"

from leafdb.engine import Database, SQLError


class MvccTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_mvcc_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "mvcc.db")
        self._open_sessions = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for db in getattr(self, "_open_sessions", []):
            try:
                db.close()
            except Exception:
                pass

    def session(self):
        db = Database(self.path)
        self._open_sessions.append(db)
        self.addCleanup(db.close)
        return db

    def test_read_committed_across_statements(self):
        w = self.session()
        r = self.session()
        w.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        w.execute_script("BEGIN; INSERT INTO t VALUES (1, 10);")
        self.assertEqual(r.execute("SELECT COUNT(*) AS n FROM t").rows, [(0,)],
                         "reader saw uncommitted data")
        w.execute_script("COMMIT;")
        self.assertEqual(r.execute("SELECT COUNT(*) AS n FROM t").rows, [(1,)],
                         "committed row invisible to reader")

    def test_snapshot_isolation_inside_explicit_txn(self):
        w = self.session()
        r = self.session()
        w.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        r.refresh_from_wal()
        w.execute("INSERT INTO t VALUES (1)")
        r.execute_script("BEGIN")
        before = r.execute("SELECT COUNT(*) AS n FROM t").rows[0][0]
        w.execute("INSERT INTO t VALUES (2)")
        w.execute("INSERT INTO t VALUES (3)")
        during = r.execute("SELECT COUNT(*) AS n FROM t").rows[0][0]
        self.assertEqual((before, during), (1, 1),
                         "snapshot changed mid-transaction")
        r.execute_script("COMMIT")
        after = r.execute("SELECT COUNT(*) AS n FROM t").rows[0][0]
        self.assertEqual(after, 3)

    def test_readers_never_see_partial_batches(self):
        w = self.session()
        r = self.session()
        w.execute("CREATE TABLE t (id INT PRIMARY KEY, batch INT)")
        w.execute_script(
            "BEGIN;\n" +
            ";\n".join(f"INSERT INTO t VALUES ({i}, 0)" for i in range(100)) +
            ";\nCOMMIT;")
        r.refresh_from_wal()

        stop = threading.Event()
        bad = []

        def writer():
            b = 1
            while not stop.is_set():
                stmts = [f"INSERT INTO t VALUES ({1000 * b + i}, {b})" for i in range(10)]
                w.execute_script("BEGIN;\n" + ";\n".join(stmts) + ";\nCOMMIT;")
                b += 1

        def reader():
            while not stop.is_set():
                n = r.execute("SELECT COUNT(*) AS n FROM t").rows[0][0]
                if (n - 100) % 10 != 0:
                    bad.append(n)
                    return
                time.sleep(0.001)

        tw = threading.Thread(target=writer)
        tr = threading.Thread(target=reader)
        tw.start()
        tr.start()
        time.sleep(1.5)
        stop.set()
        tw.join(timeout=30)
        tr.join(timeout=30)
        self.assertEqual(bad, [], f"reader observed a torn batch: counts {bad[:5]}")

    def test_reader_does_not_block_writer(self):
        w = self.session()
        r = self.session()
        w.execute("CREATE TABLE big (id INT PRIMARY KEY, pad TEXT)")
        rows = [(i, "x" * 40) for i in range(20000)]
        w.bulk_insert("big", rows)
        r.refresh_from_wal()

        done = {"commits": 0}

        def writer_load():
            for i in range(20000, 20400):
                w.execute(f"INSERT INTO big VALUES ({i}, 'y')")
                done["commits"] += 1

        r.execute_script("BEGIN")
        first = r.execute("SELECT COUNT(*) AS n FROM big").rows[0][0]

        t0 = time.perf_counter()
        tw = threading.Thread(target=writer_load)
        tw.start()
        mid_scan = r.execute(
            "SELECT COUNT(*) AS n FROM big WHERE id >= 0"
        ).rows[0][0]
        scan_ms = (time.perf_counter() - t0) * 1000.0
        tw.join(timeout=60)
        r.execute_script("COMMIT")

        self.assertEqual(first, 20000)
        self.assertEqual(mid_scan, 20000, "scan saw writer's committed rows mid-txn")
        self.assertEqual(done["commits"], 400,
                         "writer was blocked by the reader's open transaction")
        self.assertLess(scan_ms, 5000)

        final = r.execute("SELECT COUNT(*) AS n FROM big").rows[0][0]
        self.assertEqual(final, 20400)

    def test_ddl_propagates_between_sessions(self):
        a = self.session()
        b = self.session()
        b.execute("CREATE TABLE late (id INT PRIMARY KEY)")
        b.execute("INSERT INTO late VALUES (7)")
        a.refresh_from_wal()
        a.execute("INSERT INTO late VALUES (8)")
        self.assertEqual(a.execute("SELECT id FROM late ORDER BY id").rows, [(7,), (8,)])
        self.assertEqual(b.execute("SELECT id FROM late ORDER BY id").rows, [(7,), (8,)])

    def test_checkpoint_refused_while_other_sessions_open(self):
        a = self.session()
        b = self.session()
        a.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        with self.assertRaises(SQLError):
            a.checkpoint()
        b.close()
        a.checkpoint()

    def test_close_last_session_checkpoints_and_reopen_sees_data(self):
        w = self.session()
        w.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        w.execute("INSERT INTO t VALUES (1, 'persisted')")
        w.close()
        fresh = Database(self.path)
        self.assertEqual(fresh.execute("SELECT v FROM t WHERE id = 1").rows,
                         [("persisted",)])
        fresh.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
