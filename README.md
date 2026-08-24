# LeafDB

A **SQLite-inspired relational database engine written from scratch in pure Python** — stdlib only, zero dependencies. Multi-session **MVCC snapshot reads**, differential-tested against SQLite itself.

Read the deep dive: [docs/DESIGN.md](docs/DESIGN.md) · Interview prep: [docs/INTERVIEW_NOTES.md](docs/INTERVIEW_NOTES.md)

## MVCC: readers don't block writers

```python
writer = Database("app.db")
reader = Database("app.db")          # independent session, own buffer pool

reader.execute_script("BEGIN")       # snapshot frozen
for row in reader.execute("SELECT * FROM events"):   # long scan...
    ...
# ...while another session keeps committing — no blocking, no torn reads
```

- Sessions adopt other sessions' commits from the WAL tail between statements
  (READ COMMITTED); explicit transactions hold their snapshot (REPEATABLE READ).
- Commits are atomic batches; a reader can never observe a partial transaction.
- Checkpointing is refused while other sessions are alive.
- Thread-safe: one connection may be shared across threads.

```
SQL text ─► lexer ─► parser ─► AST ─► statement cache ─► planner (cost-aware) ─► executor
                                                            │
      B+ tree index ◄── buffer pool (LRU) ◄── 4 KB pages on disk
                                       │
                  Write-Ahead Log (CRC frames) → crash recovery → checkpoint
```

## Highlights

- **Real B+ tree** — leaf/internal splits on insert, **borrow-and-merge rebalancing on delete**, linked-leaf range scans, bulk-load constructor, and an **invariant validator** (`check()`) that proves sortedness, balance, separator safety and occupancy after every mutation in fuzz tests.
- **Crash-safe by construction** — every commit is CRC32-framed page images in a WAL ending in a commit record + `fsync`. Recovery replays committed batches and discards torn tails. Rollback uses **shadow paging** (pre-images captured at BEGIN), so uncommitted work never touches disk.
- **SQL that matches SQLite** — a differential suite runs hundreds of identical queries against both engines and requires identical result sets, including 210 randomized fuzz predicates.
- **Cost-aware planner** — PK lookup vs index scan vs seq scan; hash join (smaller side builds) vs nested loop; visible through `EXPLAIN`.
- **Fast paths** — parse-once statement cache and a rowid-lookup shortcut (the same trick SQLite uses) put warm point lookups at ~70 µs.

## Numbers (Windows, CPython 3.11 — your machine will vary)

| Workload | LeafDB | sqlite3 (same machine, same data) |
|---|---|---|
| Point lookup, prepared/cached | **~0.02 ms** ✅ 2.3x faster | ~0.05 ms |
| Raw B+ tree search | ~3 µs | — |
| Range scan COUNT over 9,800 rows | ~60 ms | ~0.6 ms |
| Hash join + GROUP BY + ORDER BY (20k ⋈ 200) | ~55 ms | ~7 ms |

LeafDB wins where its architecture is shortest (point lookups through Python
bindings); the C engine wins full scans, as it should. Run both yourself:

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
python -m unittest discover -s tests -v   # 107+ tests incl. differential suite
leafdb mydata.db          # if pip-installed
```

REPL meta commands: `.tables` `.schema T` `.btree T` `.check T` `.explain SELECT…` `.stats` `.vacuum T` `.benchmark` `.quit`

```sql
leafdb> CREATE TABLE users (id INT PRIMARY KEY, name TEXT, city TEXT);
leafdb> CREATE INDEX idx_city ON users(city);
leafdb> EXPLAIN SELECT * FROM users WHERE city = 'pune';
INDEX SCAN users AS users USING idx_city (city='pune') (est 1 rows)
```

## What's implemented

| Layer | Details |
|---|---|
| Storage | 4 KB pages, typed row codec (INT/TEXT + null bitmap), LRU buffer pool with dirty tracking & eviction pause during transactions |
| Indexing | B+ tree primary index (splits/borrows/merges), hash secondary indexes rebuilt at open and maintained online |
| Durability | WAL with per-frame CRC32, commit records, fsync, torn-tail discard, redo recovery, checkpointing; shadow-paged rollback |
| SQL | CREATE/DROP TABLE, CREATE/DROP INDEX, INSERT, UPDATE, DELETE, SELECT with JOINs, GROUP BY/HAVING, ORDER BY/LIMIT/OFFSET, DISTINCT |
| Predicates | `= <> < <= > >=`, AND/OR/NOT with three-valued logic, IS [NOT] NULL, [NOT] BETWEEN, [NOT] IN, [NOT] LIKE (`%`/`_`, ASCII case-insensitive like SQLite) |
| Planner | Equality analysis for index selection, equi-join detection, size-based build-side choice, **predicate pushdown below joins**, EXPLAIN plans |
| Executor | **AST compiled to closures** (columns resolve once into slot indices), hash + nested-loop joins, GROUP BY/HAVING with alias resolution, `COUNT(DISTINCT …)`, stable multi-key sort |
| Concurrency | Thread-safe engine (re-entrant lock, SQLite-style single-writer); stress-tested with parallel writers/readers and atomic-transfer conservation |
| Transactions | Full ACID-ish: BEGIN/COMMIT/ROLLBACK across statements, autocommit per statement, lazy snapshots so reads stay cheap |

## Design decisions worth defending in interviews

1. **B+ tree over hash for the primary index** — sorted linked leaves give O(log n) lookups *and* streaming range scans; hashes handle equality-only secondaries.
2. **Physical WAL (page images), not logical logs** — redo is a blind overwrite: idempotent, trivially replayable, no logical undo needed because rollback never touches disk.
3. **Shadow paging for rollback** — pre-images are captured in memory at first touch inside a transaction; rollback reinstates them. Combined with "no eviction of dirty pages mid-transaction", uncommitted bytes provably never reach the data file before their commit record.
4. **Separators may lag after deletes** — deletes don't rescale ancestor separators; the invariant enforced is `separator ≤ min(right subtree)`, which keeps `bisect_right` routing correct while avoiding cascading writes (same spirit as SQLite).
5. **Byte-utilization underflow thresholds (⅓ page)** rather than entry counts, so behavior is stable whether rows are 12 bytes or 2 KB.
6. **Lazy catalog snapshots** — read-only transactions skip snapshotting and WAL appends entirely; writes pay only for what they change.
7. **Differential testing as the spec** — where SQL semantics get subtle (3VL, NULL ordering, LIKE coercion, IN-with-NULL), SQLite is the oracle and CI enforces agreement.

## Known limitations (honesty section)

- INT/TEXT stored types only; floats exist in expressions but not in columns.
- Single-process, single-writer; no MVCC/locking.
- Catalog lives on one 4 KB page (schema size bounded).
- Dropped tables leave orphaned pages until `VACUUM`-style compaction of the whole file (per-table vacuum exists).

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
│   ├── executor.py     # joins/aggregates/3VL evaluation pipeline
│   ├── engine.py       # Database facade, txns, indexes, fast paths
│   ├── repl.py         # interactive shell
│   └── bench.py        # benchmark suite
├── tests/
│   ├── test_leafdb.py       # unit tests per layer (~96)
│   ├── test_differential.py # differential tests vs sqlite3 (11 scenarios, 210+ fuzz queries)
│   └── test_bench.py        # benchmark smoke tests
├── demo.py                  # guided tour
└── .github/workflows/ci.yml # Ubuntu+Windows × Python 3.9–3.12
```
