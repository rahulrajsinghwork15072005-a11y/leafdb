"""notes.py — a tiny CLI notes app that uses LeafDB as its storage engine.

This is LeafDB dogfooding: real usage through the public API
(multi-statement scripts, transactions, indexes, FTS-ish LIKE search).

Usage:
    python examples/notes.py add "buy milk"
    python examples/notes.py list
    python examples/notes.py search milk
    python examples/notes.py done 3
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leafdb.engine import Database

DB_PATH = os.environ.get("NOTES_DB", "notes.db")


def open_db():
    db = Database(DB_PATH)
    if "notes" not in db.catalog.tables:
        db.execute_script(
            """
            CREATE TABLE notes (
                id INT PRIMARY KEY,
                text TEXT,
                done INT
            );
            CREATE INDEX idx_done ON notes(done);
            """
        )
    return db


def cmd_add(db, text):
    res = db.execute(f"INSERT INTO notes (text, done) VALUES ('{text.replace(chr(39), chr(39) * 2)}', 0)")
    print(res.message)


def cmd_list(db):
    res = db.execute("SELECT id, text FROM notes WHERE done = 0 ORDER BY id")
    if not res.rows:
        print("(no open notes)")
    for nid, text in res.rows:
        print(f"  [{nid}] {text}")
    done = db.execute("SELECT COUNT(*) AS n FROM notes WHERE done = 1").rows[0][0]
    print(f"  ({done} done)")


def cmd_search(db, term):
    res = db.execute(
        f"SELECT id, text FROM notes WHERE text LIKE '%{term.replace(chr(39), chr(39) * 2)}%' ORDER BY id"
    )
    for nid, text in res.rows:
        mark = "x" if db.execute(f"SELECT done FROM notes WHERE id = {nid}").rows[0][0] else " "
        print(f"  [{mark}] [{nid}] {text}")


def cmd_done(db, nid):
    msg = db.execute(f"UPDATE notes SET done = 1 WHERE id = {nid}").message
    print(msg)


def main(argv):
    db = open_db()
    try:
        if not argv or argv[0] == "list":
            cmd_list(db)
        elif argv[0] == "add":
            cmd_add(db, " ".join(argv[1:]) or "(empty)")
        elif argv[0] == "search":
            cmd_search(db, argv[1] if len(argv) > 1 else "")
        elif argv[0] == "done":
            cmd_done(db, int(argv[1]))
        else:
            print(__doc__)
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1:])
