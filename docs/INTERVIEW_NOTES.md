# LeafDB — Interview Notes (question → code map)

Every claim you make in an interview should point at code you can defend. This maps
the classic questions to exact locations.

## Storage engine

| Question | Where to point |
|---|---|
| "Why fixed-size pages?" | `pager.py` — `PAGE_SIZE = 4096`; every read/write is page-granular; buffer pool caches them |
| "How does the cache work?" | `pager.BufferPool` — LRU via `OrderedDict`, dirty set, eviction writes through; **eviction pauses during transactions** (`Pager._evict_if_full`) so dirty pages can't reach disk pre-commit |
| "How do you serialize rows?" | `rows.encode_row/decode_row` — null bitmap header + packed INTs + length-prefixed UTF-8 |

## B+ tree

| Question | Where |
|---|---|
| "Insert and keep it balanced?" | `BTree.insert/_insert` — recursive descent, `bisect` for slot search, `_split_leaf/_split_internal` push separators up; new root when the root splits |
| "Range scans?" | `range_scan` — descend to the first candidate leaf, then walk `next_leaf` chain; stops the moment a key exceeds `hi` |
| "Deletes? That's the hard part." | `delete/_delete/_rebalance` — underflow detection by byte utilization (`_min_bytes()`), borrow-from-left/right rotations (`_borrow_from_*`), merges that pull the parent separator down (`_merge(..., sep=...)`) |
| "How do you know it's correct?" | `check()` — validates children==keys+1, separator ≤ min(right subtree), leaf-chain order equals tree order, uniform depth, occupancy; fuzz-tested in `tests/test_leafdb.py::test_oracle_random_operations` with mid-run checks |
| "Why can separators go stale?" | Deletes don't rescale ancestors; invariant enforced is `sep ≤ min(right)` which keeps bisect_right routing correct — same spirit as SQLite |

## Durability

| Question | Where |
|---|---|
| "Write-ahead logging — walk me through a commit" | `engine.commit` → `pager.collect_commit()` gathers touched page images → `wal.append_batch` frames each page (magic+page+length+CRC32), ends batch with commit record carrying the new file length, then fsync |
| "Crash mid-commit?" | Recovery reads sequentially; any bad magic / short read / CRC mismatch discards the rest of the log (`wal.recover` loop breaks) → torn tail gone |
| "Crash after commit, before checkpoint?" | Committed batches replayed into the data file on next open (`Database.__init__` → `recover`), then WAL resets |
| "Rollback?" | Shadow paging: `Pager.write` captures pre-images into `self.shadow` on first touch per transaction; `rollback_txn` reinstates them. Uncommitted bytes never touch disk because dirty-page eviction is paused while a transaction is open |
| "Read-only transactions?" | Lazy snapshots + empty-batch skip: `engine.begin/_ensure_snapshot/commit` — SELECTs pay ~zero txn overhead |

## SQL

| Question | Where |
|---|---|
| "Parse a query" | `sqlparse.tokenize` (single regex pass) → `Parser` recursive descent with precedence OR < AND < NOT < predicate < additive < multiplicative |
| "Three-valued logic?" | `executor.eval_expr` returns True/False/None; comparisons with NULL yield None; `_kleene` implements SQL AND/OR; WHERE keeps only exactly-True rows |
| "JOIN algorithms?" | `planner.find_join_equi` detects cross-side equalities → hash join (build side = smaller estimated input); otherwise nested loop. Both live in `Executor._join`; LEFT/FULL unmatched handling at the end |
| "GROUP BY/HAVING semantics?" | `Executor._aggregate` — group keys evaluated per row, aggregates accumulated over non-null values, HAVING re-evaluates expressions against per-group aggregate env; alias references (HAVING cnt > 2) resolved via `Scope.agg_aliases` |
| "NULL ordering?" | `sort_wrap` — NULLs sort before everything ASC, after everything DESC (SQLite-compatible); multi-key stable sorts applied right-to-left |
| "How does EXPLAIN work?" | `planner.build_select_plan` emits step strings as it chooses scans/joins; `EXPLAIN` returns them without executing |

## Performance

| Question | Where |
|---|---|
| "How did you get to ~70 µs lookups?" | Statement cache (`Database._parse_cached`), PK fast path bypassing planner/executor (`_pk_fast_path`), lazy snapshots, raw btree.search ≈ 22 µs floor |
| "What's left on the table?" | Bytecode VM for expressions, columnar scan batches, mmap I/O, prepared-statement parameters instead of string cache |

## Testing philosophy

- Unit tests per layer (~96): `tests/test_leafdb.py`
- **Differential vs SQLite**: identical schema/data/queries run on both engines, result sets must match exactly, incl. 210 seeded-random predicates/groupings: `tests/test_differential.py`
- Crash simulations: commit→no-checkpoint→reopen replays; BEGIN without COMMIT loses data
