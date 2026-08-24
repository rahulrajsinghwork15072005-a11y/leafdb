"""Cross-language compatibility: LeafDB-Python and leafdb-core (C++) must be
able to read each other's page files byte-for-byte."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

os.environ["LEAFDB_FSYNC"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leafdb.engine import Database
from leafdb.pager import Pager
from leafdb.btree import BTree, Leaf

EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "native", "build", "leafdb-core.exe" if os.name == "nt"
                   else "leafdb-core")


def exe_available():
    return os.path.exists(EXE)


@unittest.skipUnless(exe_available(), "native binary not built")
class CrossCompat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_xlang_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_native(self, *args):
        r = subprocess.run([EXE, *args], capture_output=True, text=True,
                           timeout=120, cwd=self.tmp)
        return r.returncode, r.stdout.strip()

    def test_python_writes_cpp_verifies(self):
        path = os.path.join(self.tmp, "py.db")
        db = Database(path)
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        rows = [(i, f"value-{i}") for i in range(1, 501)]
        db.execute_script("BEGIN;\n" + ";\n".join(
            f"INSERT INTO t VALUES ({r[0]}, '{r[1]}')" for r in rows) + ";\nCOMMIT;")
        root = db.lookup_meta("t").root_page
        db.close(checkpoint=False)
        db = Database(path)
        root = db.lookup_meta("t").root_page
        db.close()

        code, out = self.run_native("verify", "--root", str(root), path)
        self.assertEqual(code, 0, out)
        self.assertIn("keys=500", out)

    def test_cpp_writes_python_reads(self):
        path = os.path.join(self.tmp, "cpp.db")
        code, out = self.run_native("bench", path, "2000")
        self.assertEqual(code, 0, out)
        with open(path + ".root") as f:
            root = int(f.read().strip())

        pager = Pager(path, 4096)
        tree = BTree(pager)
        got = tree.search(root, 1234)
        self.assertIsNotNone(got)
        expected = (1234 * 2654435761) & 0xFFFFFFFFFFFFFFFF
        self.assertEqual(int.from_bytes(got[:8], "little"), expected)

        all_rows = list(tree.range_scan(root))
        self.assertEqual(len(all_rows), 2000)
        self.assertEqual([k for k, _ in all_rows], list(range(1, 2001)))

    def test_roundtrip_both_directions(self):
        py_path = os.path.join(self.tmp, "rt_py.db")
        pager = Pager(py_path, 4096)
        tree = BTree(pager)
        root = pager.allocate()
        pager.write(root, Leaf().to_bytes())
        for k in range(1, 301):
            root = tree.insert(root, k, bytes([k % 256]) * 8)
        pager.flush()

        code, out = self.run_native("verify", "--root", str(root), py_path)
        self.assertEqual(code == 0 or "keys=" in out, True, out)
        self.assertIn("keys=300", out)

        code, out = self.run_native(
            "dump", "--root", str(root), py_path, "50", "59")
        keys = [int(x) for x in out.splitlines() if x.strip().isdigit()]
        self.assertEqual(keys, list(range(50, 60)))


if __name__ == "__main__":
    unittest.main(verbosity=2)

