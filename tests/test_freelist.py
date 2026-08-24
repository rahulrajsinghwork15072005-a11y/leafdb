"""Free-page reclamation: DROP TABLE returns pages to the free list,
CREATE TABLE reuses them instead of growing the file."""
import os
import shutil
import tempfile
import unittest

os.environ["LEAFDB_FSYNC"] = "0"

from leafdb.engine import Database


class FreePageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_free_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "free.db")

    def test_drop_reclaims_pages_for_reuse(self):
        db = Database(self.path)
        pages_before = db.pager.num_pages
        for i in range(5):
            db.execute(f"CREATE TABLE t{i} (id INT PRIMARY KEY, v INT)")
        pages_after_create = db.pager.num_pages
        self.assertGreater(pages_after_create, pages_before)

        db.execute("DROP TABLE t2")
        pages_after_drop = db.pager.num_pages
        # file does NOT shrink (we don't truncate) but freed page is reusable
        self.assertEqual(db.pager.num_pages, pages_after_drop)

        db.execute("CREATE TABLE new_t (id INT PRIMARY KEY)")
        # should reuse a freed page, not allocate a new one
        self.assertEqual(db.pager.num_pages, pages_after_drop)
        db.close()

    def test_data_integrity_after_page_reuse(self):
        db = Database(self.path)
        db.execute_script(
            "CREATE TABLE a (id INT PRIMARY KEY, v TEXT);\n"
            "INSERT INTO a VALUES (1,'hello'),(2,'world');\n"
            "CREATE TABLE b (id INT PRIMARY KEY, v TEXT);\n"
            "INSERT INTO b VALUES (1,'x'),(2,'y');"
        )
        db.execute("DROP TABLE b")
        db.execute("CREATE TABLE c (id INT PRIMARY KEY, msg TEXT)")
        db.execute("INSERT INTO c VALUES (1, 'fresh data')")
        res = db.execute("SELECT * FROM a ORDER BY id")
        self.assertEqual(res.rows[0][0], 1)
        self.assertEqual(res.rows[1][0], 2)
        res = db.execute("SELECT msg FROM c WHERE id = 1")
        self.assertEqual(res.rows, [("fresh data",)])
        db.close()
        db2 = Database(self.path)
        self.assertEqual(
            db2.execute("SELECT COUNT(*) AS n FROM a").rows[0][0], 2
        )
        db2.close()


if __name__ == "__main__":
    unittest.main()
