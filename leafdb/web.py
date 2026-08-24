"""LeafDB Studio - a local web console for inspecting a database.

    python -m leafdb.web mydata.db [--port 8000]

Serves a single-page UI (static files from web/) plus a JSON API on
127.0.0.1. Queries run through the normal engine, so MVCC sessions,
EXPLAIN plans and B+ tree inspection all work exactly as in the REPL.
"""
import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .engine import Database
from .catalog import CATALOG_PAGE

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_static")

STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
}


def btree_layout(db, table_name):
    """Node graph for the visualizer: levels of pages root -> leaves."""
    meta = db.catalog.get(table_name)
    stats = db.btree.stats(meta.root_page)

    levels = []
    current = [(meta.root_page, None)]

    while current:
        next_level = []
        level_nodes = []
        for page, _parent in current:
            node = db.btree._load(page)
            if hasattr(node, "cells"):
                cells = [{"key": k, "size": len(v)} for k, v in node.cells]
                level_nodes.append({
                    "page": page, "kind": "leaf", "cells": cells,
                    "count": len(cells), "next": node.next_leaf,
                    "first": cells[0]["key"] if cells else None,
                    "last": cells[-1]["key"] if cells else None,
                })
            else:
                level_nodes.append({
                    "page": page, "kind": "internal",
                    "keys": list(node.keys), "children": list(node.children),
                })
        levels.append(level_nodes)
        for page, _parent in current:
            node = db.btree._load(page)
            if not hasattr(node, "cells"):
                next_level.extend((c, page) for c in node.children)
        current = next_level
    return {"table": table_name, "root": meta.root_page,
            "stats": stats, "levels": levels}


class Handler(BaseHTTPRequestHandler):
    server_version = "LeafDBStudio/1.0"
    db = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name):
        path = os.path.join(WEB_DIR, name)
        if not os.path.exists(path):
            self.send_error(404)
            return
        ctype = ("text/html" if name.endswith(".html")
                 else "text/css" if name.endswith(".css")
                 else "application/javascript" if name.endswith(".js")
                 else "text/plain")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/tables":
            tables = []
            for name in sorted(self.db.catalog.tables):
                m = self.db.catalog.tables[name]
                pk = m.pk_column()
                tables.append({
                    "name": name, "rows": m.row_count,
                    "columns": [
                        {"name": c["name"], "type": c["type"],
                         "pk": c["name"] == pk,
                         "indexed": c["name"] in m.indexes}
                        for c in m.columns],
                })
            self._json({"tables": tables})
        elif route.startswith("/api/btree/"):
            table = route[len("/api/btree/"):]
            try:
                self._json(btree_layout(self.db, table))
            except Exception as e:
                self._json({"error": str(e)}, 400)
        elif route == "/api/stats":
            st = self.db.stats()
            st["wal_bytes"] = self.db.wal.pending_bytes()
            self._json(st)
        elif route in STATIC_FILES:
            self._static(STATIC_FILES[route])
        else:
            self.send_error(404)

    def do_POST(self):
        route = urlparse(self.path).path
        if route != "/api/query":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad JSON body"}, 200)
            return
        sql = (payload.get("sql") or "").strip()
        if not sql:
            self._json({"ok": False, "error": "empty query"}, 200)
            return
        try:
            results = []
            for res in self.db.execute_script(sql):
                results.append({
                    "cols": res.cols,
                    "rows": [list(r) for r in res.rows],
                    "message": res.message,
                    "plan": res.plan,
                    "elapsed_ms": round(res.elapsed_ms, 3),
                })
            self._json({"ok": True, "results": results})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 200)


def main():
    ap = argparse.ArgumentParser(description="LeafDB Studio web console")
    ap.add_argument("database", nargs="?", default="leafdb.db")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    db = Database(args.database)
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"LeafDB Studio  ->  http://127.0.0.1:{args.port}")
    print(f'database       ->  "{args.database}"   (Ctrl+C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()


