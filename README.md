# LeafDB

A relational database engine written from scratch in Python — page-based
storage, a B+ tree, crash-safe write-ahead logging, MVCC sessions, and a SQL
parser/planner/executor. Standard library only, zero dependencies.

It started as interview prep: I could describe what indexes and WAL logs do,
but couldn't explain how one actually gets built. So I built one. Along the
way it got differential-tested against SQLite (same queries must produce the
same results), gained a C++17 storage core that reads the same files, and
collected some genuinely painful bugs — those are written up honestly in
[DEVLOG.md](DEVLOG.md).

Docs: [DESIGN.md](docs/DESIGN.md) covers architecture and rejected designs.
[INTERVIEW_NOTES.md](docs/INTERVIEW_NOTES.md) maps interview questions to code.
[DEVLOG.md](DEVLOG.md) is the build journal.

## MVCC: readers don't block writers

```python
writer = Database("app.db")
reader = Database("app.db")          # independent session, own buffer pool

reader.execute_script("BEGIN")       # snapshot frozen
for row in reader.execute("SELECT * FROM events"):   # long scan...
    ...
# ...while another session keeps committing - no blocking, no torn reads
```

Sessions adopt other sessions' commits from the WAL tail between statements
(READ COMMITTED); explicit transactions hold their snapshot (REPEATABLE READ).
Commits are atomic batches, so a reader can never observe half a transaction.
Checkpointing is refused while other sessions are alive.

## Numbers

Measured on Windows, CPython 3.11. Yours will differ; both benchmark scripts
ship with the project.

| Workload | LeafDB | sqlite3 (same machine, same data) |
|---|---|---|
| Point lookup, prepared/cached | ~0.02 ms | ~0.05 ms |
| Point lookup, ad-hoc SQL string | ~0.08 ms | ~0.05 ms |
| Raw B+ tree search (C++ core) | ~11 us | — |
| Range scan COUNT over 9,800 rows | ~60 ms | ~0.6 ms |
| Hash join + GROUP BY + ORDER BY (20k x 200) | ~55 ms | ~7 ms |

LeafDB wins where its path is shortest (point lookups through Python
bindings). The C engine wins full scans, as it should. Reproduce with:

```bash
python -m leafdb.bench       # internal benchmarks + buffer-pool stats
python -m leafdb.compare     # head-to-head vs sqlite3 on identical data
```

## Quickstart

```bash
cd projects/leafdb

pip install -e .          # optional; provides the `leafdb` command
python -m leafdb          # interactive REPL (creates leafdb.db)
python demo.py            # scripted feature tour incl. crash recovery
python -m unittest discover -s tests -v   # full suite incl. differential tests
```

REPL meta commands: `.tables` `.schema T` `.btree T` `.check T`
`.explain SELECT...` `.stats` `.vacuum T` `.benchmark` `.quit`

```sql
leafdb> CREATE TABLE users (id INT PRIMARY KEY, name TEXT, city TEXT);
leafdb> CREATE INDEX idx_city ON users(city);
leafdb> EXPLAIN SELECT * FROM users WHERE city = 'pune';
INDEX SCAN users AS users USING idx_city (city='pune') (est 1 rows)
```

## What's implemented

| Layer | Details |
|---|---|
| Storage | Fixed 4 KB pages, typed row codec (INT/TEXT + null bitmap), LRU buffer pool, eviction paused during transactions |
| Indexing | B+ tree primary index: splits on insert, borrow/merge rebalancing on delete, linked leaves for range scans; hash secondary indexes maintained online |
| Durability | WAL with per-frame CRC32 and commit records, fsync, torn-tail discard, redo recovery, checkpointing; shadow-paged rollback |
| SQL | CREATE/DROP TABLE, CREATE/DROP INDEX, INSERT, UPDATE, DELETE, SELECT |
| Queries | INNER/LEFT/RIGHT/FULL joins (hash or nested loop), GROUP BY/HAVING with alias resolution, ORDER BY/LIMIT/OFFSET, DISTINCT, COUNT(DISTINCT ...) |
| Predicates | Comparisons, AND/OR/NOT with three-valued logic, IS [NOT] NULL, [NOT] BETWEEN, [NOT] IN, [NOT] LIKE (% and _, ASCII case-insensitive like SQLite) |
| Planner | PK lookup vs index scan vs seq scan by cost; equi-join detection picks hash join (smaller side builds); predicate pushdown below joins; EXPLAIN |
| Concurrency | Multi-session MVCC snapshot reads; thread-safe shared connections; stress-tested |

## The C++17 storage core

`native/` implements the storage hot path in C++17 with the same on-disk
formats — pages, node encoding, CRC32 WAL frames are byte-identical, so files
are interchangeable between the two engines in either direction.
`tests/native_compat.py` proves it.

```bash
make -C native test          # randomized oracle tests incl. small pages
./native/build/leafdb-core bench mydata.db 20000
# inserted 20000 rows ... point lookups ~11 us/op
```

## Design decisions worth defending

1. **B+ tree over hash for the primary index** - sorted linked leaves give
   O(log n) lookups plus streaming range scans; hashes handle equality-only
   secondaries.
2. **Physical WAL (page images), not logical logs** - redo is a blind
   overwrite: idempotent, trivially replayable, and rollback never needs
   logical undo because uncommitted work never reaches disk.
3. **Shadow paging for rollback** - pre-images captured at first touch inside
   a transaction; combined with pausing cache eviction mid-transaction,
   uncommitted bytes provably never hit the data file before their commit
   record.
4. **Separators may lag after deletes** - deletes do not rescale ancestors;
   the enforced invariant is `separator <= min(right subtree)`, which keeps
   bisect-right routing correct while avoiding cascading writes.
5. **Byte-utilization underflow thresholds (1/3 page)** instead of entry
   counts, so behavior holds whether rows are 12 bytes or 2 KB.
6. **Lazy transaction snapshots** - read-only transactions skip snapshotting
   and skip WAL appends entirely.
7. **Differential testing as the spec** - where SQL semantics get subtle
   (three-valued logic, NULL ordering, LIKE coercion, IN with NULL), SQLite is
   the oracle and CI enforces agreement.

## Known limitations

- INT/TEXT stored types only; floats exist in expressions but not columns.
- Single-process, single-writer; no locking across processes yet.
- Catalog lives on one 4 KB page, bounding schema size.
- Dropped tables leave orphaned pages until a future whole-file compaction
  (per-table vacuum exists).

## Repository layout

```
leafdb/
├── leafdb/
│   ├── pager.py        # pages + LRU buffer pool + shadow paging
│   ├── rows.py         # typed row serialization
│   ├── btree.py        # B+ tree (splits, borrows/merges, check(), bulk_load)
│   ├── wal.py          # CRC'd write-ahead log + recovery
│   ├── catalog.py      # page-0 schema catalog
│   ├── sqlparse.py     # lexer + recursive-descent parser + AST
│   ├── planner.py      # cost-based plan builder (+ EXPLAIN text)
│   ├── executor.py     # closure-compiling executor: joins/aggregates/3VL
│   ├── engine.py       # Database facade, MVCC sessions, txns, indexes
│   ├── repl.py         # interactive shell
│   ├── bench.py        # benchmark suite
│   └── compare.py      # head-to-head vs sqlite3
├── native/             # C++17 storage core (binary-compatible)
├── tests/
│   ├── test_leafdb.py       # unit tests per layer
│   ├── test_differential.py # differential tests vs sqlite3
│   ├── test_concurrency.py  # multi-threaded stress tests
│   ├── test_mvcc.py         # multi-session snapshot isolation
│   └── native_compat.py     # Python <-> C++ file compatibility
├── examples/notes.py   # small CLI app using LeafDB for storage
├── demo.py             # guided tour
└── .github/workflows/ci.yml
```
