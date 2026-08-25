import os
import shutil
import tempfile
import unittest

os.environ["LEAFDB_FSYNC"] = "0"

from leafdb.engine import Database
from leafdb.rows import encode_row, decode_row, coerce_value


class FloatTypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_float_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_float_roundtrip(self):
        db = Database(os.path.join(self.tmp, "f.db"))
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v FLOAT)")
        db.execute("INSERT INTO t VALUES (1, 3.14), (2, -0.5), (3, NULL)")
        rows = db.execute("SELECT id, v FROM t ORDER BY id").rows
        self.assertEqual(rows[0], (1, 3.14))
        self.assertEqual(rows[1], (2, -0.5))
        self.assertIsNone(rows[2][1])
        db.close()

    def test_float_persistence(self):
        path = os.path.join(self.tmp, "p.db")
        db = Database(path)
        db.execute("CREATE TABLE m (id INT PRIMARY KEY, price FLOAT)")
        db.execute("INSERT INTO m VALUES (1, 99.99)")
        db.close()
        db2 = Database(path)
        self.assertEqual(db2.execute("SELECT price FROM m").rows, [(99.99,)])
        db2.close()

    def test_int_into_float_promotes(self):
        db = Database(os.path.join(self.tmp, "i.db"))
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v FLOAT)")
        db.execute("INSERT INTO t VALUES (1, 42)")
        self.assertEqual(db.execute("SELECT v FROM t").rows, [(42.0,)])
        db.close()

    def test_float_arithmetic_in_queries(self):
        db = Database(os.path.join(self.tmp, "a.db"))
        db.execute("CREATE TABLE p (id INT PRIMARY KEY, price FLOAT)")
        db.execute("INSERT INTO p VALUES (1, 10.5), (2, 20.25)")
        r = db.execute("SELECT SUM(price) AS total FROM p")
        self.assertAlmostEqual(r.rows[0][0], 30.75)
        db.close()

    def test_coerce_float_rejects_string(self):
        with self.assertRaises(TypeError):
            coerce_value("FLOAT", "c", "hello")


if __name__ == "__main__":
    unittest.main()
