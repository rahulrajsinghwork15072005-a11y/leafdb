import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

os.environ["LEAFDB_FSYNC"] = "0"

from leafdb.engine import Database
from leafdb import web


class WebStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leafdb_web_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "web.db")
        self.db = Database(self.path)
        self.db.execute_script(
            """
            CREATE TABLE users (id INT PRIMARY KEY, name TEXT);
            INSERT INTO users VALUES (1,'a'),(2,'b'),(3,'c');
            CREATE TABLE empty_t (id INT PRIMARY KEY);
            """
        )
        web.Handler.db = self.db
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.server.server_address[1]
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.db.close)
        self.base = f"http://127.0.0.1:{self.port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read())

    def post(self, path, obj):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    def test_index_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            body = r.read().decode()
        self.assertIn("LeafDB Studio", body)

    def test_tables_endpoint(self):
        data = self.get("/api/tables")
        names = [t["name"] for t in data["tables"]]
        self.assertIn("users", names)
        users = next(t for t in data["tables"] if t["name"] == "users")
        self.assertTrue(users["columns"][0]["pk"])
        self.assertEqual(users["rows"], 3)

    def test_query_select(self):
        res = self.post("/api/query", {"sql": "SELECT id FROM users ORDER BY id"})
        self.assertTrue(res["ok"])
        one = res["results"][0]
        self.assertEqual(one["cols"], ["id"])
        self.assertEqual(one["rows"], [[1], [2], [3]])
        self.assertGreaterEqual(one["elapsed_ms"], 0)

    def test_query_error_is_reported_not_raised(self):
        res = self.post("/api/query", {"sql": "SELECT * FROM missing_table"})
        self.assertFalse(res["ok"])
        self.assertIn("no such table", res["error"])

    def test_query_empty_rejected(self):
        res = self.post("/api/query", {"sql": ""})
        self.assertFalse(res["ok"])

    def test_btree_layout_shape(self):
        data = self.get("/api/btree/users")
        self.assertEqual(data["table"], "users")
        self.assertEqual(data["levels"][0][0]["kind"], "leaf")
        flat = [n for lvl in data["levels"] for n in lvl]
        keys_in_leaves = sum(n["count"] for n in flat if n["kind"] == "leaf")
        self.assertEqual(keys_in_leaves, 3)
        for lvl in data["levels"]:
            for node in lvl:
                if node["kind"] == "internal":
                    self.assertEqual(len(node["children"]), len(node["keys"]) + 1)

    def test_stats_endpoint(self):
        st = self.get("/api/stats")
        self.assertIn("pager", st)
        self.assertIn("wal_bytes", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
