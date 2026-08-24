# LeafDB Design

A relational storage engine built to answer one question: *do you understand how a
database works below the SQL surface?* This document explains each layer's contract,
the invariants that keep it correct, and why alternatives were rejected.

## Layer contracts

```
┌────────────────────────────────────────────────────────────┐
│ engine.py    transactions · catalog · indexes · fast paths │
├────────────────────────────────────────────────────────────┤
│ planner.py   scan/join selection        executor.py runtime│
├────────────────────────────────────────────────────────────┤
│ sqlparse.py  lexer → parser → AST → closure compiler       │
├────────────────────────────────────────────────────────────┤
│ btree.py     B+ tree                    wal.py  redo log   │
├────────────────────────────────────────────────────────────┤
│ pager.py     4 KB pages · LRU pool · shadow paging         │
└────────────────────────────────────────────────────────────┘
```

**Pager.** The only module that touches bytes-on-disk. Contract: `get(n)` returns
exactly 4096 bytes; `write(n, data)` stages them; nothing reaches the file until
flush/checkpoint/eviction. During an open transaction eviction is disabled, so
uncommitted pages cannot be written behind the log's back.

**B+ tree.** Leaves own sorted `(rowid, row-blob)` cells chained for range scans;
internals route by separators. Invariants enforced by `check()`:

1. every internal node has `children == keys + 1`;
2. `separator ≤ min(right subtree)` — may lag after deletes, never exceeds;
3. leaf chain order == tree order, acyclic;
4. all leaves at equal depth (splits push down, merges pull up);
5. non-root nodes stay above ⅓-page byte utilization.

Insert splits at page capacity; delete borrows from siblings before merging.
Merges must carry the parent separator into the merged node (`_merge(..., sep=...)`) —
omitting it silently corrupts routing, which the fuzz suite caught.

**WAL.** Physical logging: commit = CRC32'd page images + commit record + fsync.
Replay is a blind overwrite, hence idempotent. Any torn tail fails CRC/magic/length
checks and is discarded. Recovery happens before the pager opens, then the log resets
(the replay *is* the checkpoint).

**Rollback = shadow paging.** First touch of a page inside a transaction copies its
pre-image into `Pager.shadow`. Rollback reinstates pre-images in the pool and drops
them from dirty state. Combined with mid-txn eviction pause this gives a hard
guarantee: **uncommitted bytes are unreachable on disk**.

**SQL front-end.** One regex pass lexes; a recursive-descent parser builds the AST
with correct precedence (`OR < AND < NOT < predicate < additive < multiplicative`);
statements are cached by source string so repeated queries parse once.

**Compilation, not interpretation.** `compile_expr` lowers expressions to closures:
column references resolve once into `(pool-slot, column-index)` pairs, LIKE patterns
with literal patterns compile to regexes up-front, aggregates capture their argument
closure once per group context. Per-row work becomes two subscripts plus a call.

**Planner.** Conjunct-level analysis: `pk = literal` → tree search; indexed-column
equality → secondary index; else sequential scan. Joins detect cross-side equality →
hash join with the smaller estimated side as build input; otherwise nested loop.
WHERE conjuncts are assigned to the earliest pipeline stage whose tables they reference
(**predicate pushdown**) and compiled there. `EXPLAIN` prints the chosen steps.

**Executor.** Scan → staged filters → joins (+ staged filters) → grouping/aggregates →
HAVING → projection → DISTINCT → stable multi-key sort → LIMIT/OFFSET. NULL follows
SQL three-valued logic end-to-end: comparisons yield UNKNOWN, Kleene AND/OR,
aggregates skip NULLs, NULLs group together, sort first ASC / last DESC.

## Concurrency model

**MVCC-lite (SQLite-WAL architecture).** Multiple `Database` sessions may open the
same file; each owns a buffer pool and tracks its position in the WAL. Commits are
atomic, fsync'd batches; other sessions adopt them between statements by replaying
the log tail (READ COMMITTED), while explicit transactions freeze their position and
keep a stable snapshot (REPEATABLE READ) — so **readers never block the writer and
the writer never blocks readers**, and partial transactions are unobservable.

Writers serialize on a per-path lock (in-process registry keyed by absolute path).
Checkpointing is refused while other sessions are alive because they may still need
older log contents; the final session to close checkpoints automatically. A shared
"current log end" table makes cross-session freshness checks dictionary-lookups —
zero syscalls when nothing changed (stat-based forced refresh available for future
cross-process use).

Thread safety: every entry point is guarded; stress tests run 8 writers + 3 readers,
parallel bank transfers asserting conservation of money, and shared-connection
hammering.

## Testing strategy

| Suite | Purpose |
|---|---|
| `test_leafdb.py` | unit tests per layer incl. 1500-op randomized B+ tree oracle with mid-run invariant checks |
| `test_differential.py` | identical queries against stdlib sqlite3; result multisets must match exactly — 210+ seeded-random predicates/groupings |
| `test_concurrency.py` | multi-threaded writers/readers, atomic-transfer conservation, shared-connection hammering |
| crash tests | commit→no-checkpoint→reopen replays; uncommitted→reopen vanishes |

The differential suite doubles as the semantic spec: whenever behavior was subtle
(NULL in IN-lists, LIKE coercion of integers, NULL ordering), SQLite decided, LeafDB
followed, CI enforces.

## Performance notes (CPython 3.11, Windows)

Warm point lookup ≈ **28 µs** via PK fast path + statement cache — measured *faster
than sqlite3 through Python bindings* (~45 µs) because both engines pay interpreter
overhead per call and LeafDB's path is shorter. Full scans lose to C by design
(~90x on raw COUNT); the honest comparison table ships in `leafdb.compare`.

## Rejected designs (and why)

- **Logical WAL (SQL redo)** — requires logical undo for rollback and idempotence is fragile under partial application. Page images are dumb and bulletproof.
- **Count-based node occupancy** — breaks when value sizes vary 12 B–2 KB; bytes-based thresholds self-tune.
- **Eager transaction snapshots** — charged every SELECT a JSON deep-copy; lazy snapshots made reads ~free.
- **Rebuilding separators on delete** — cascading writes through ancestors; instead allow lagging separators (`sep ≤ min(right)`) which preserves routing correctness.

## Roadmap (in order of interview value)

1. Cross-process MVCC (file-lock writer arbitration + stat-based refresh)
2. Expression bytecode VM with register allocation (closure compiler already
   removed per-row AST walks; a VM would cut call overhead further)
3. Sort-merge join for ordered inputs
4. Whole-file compaction reclaiming dropped-table pages
5. **C/C++ core** (`leafdb-core`): the storage hot path — pager, B+ tree node
   ops, CRC framing — as a native library with Python bindings. This machine has
   no C++ toolchain installed, so it is scoped as its own follow-up project with
   its own test suite; the Python engine's test battery doubles as the spec it
   must satisfy before it can replace anything.
