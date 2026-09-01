# Changelog

## 3.0.0 — MVCC sessions

- Multi-session databases: open the same file from several `Database` handles;
 each session gets snapshot reads off the WAL tail (`refresh_from_wal`).
- **Readers don't block the writer**: explicit transactions freeze their snapshot
 while other sessions keep committing (verified by stress test).
- READ COMMITTED between statements, REPEATABLE READ inside a transaction.
- Checkpointing is refused while other sessions are alive (they may still need
 the log); the last session to `close` checkpoints automatically.
- `Database.bulk_insert(table, rows)` — parse-free batch loading.
- Cross-session WAL appends now seek-to-EOF (multi-writer file-position bug).

## 2.0.0 — performance & proof

- Expression compiler: SELECT/HAVING/joins evaluate compiled closures instead
 of walking the AST per row.
- Predicate pushdown below joins; EXPLAIN shows applied stages.
- Statement cache (parse-once) + PK-lookup fast path.
- Differential test suite vs stdlib sqlite3 (210+ fuzz queries must match).
- Real B+ tree deletes (borrow/merge), invariant checker `check`.
- BETWEEN / IN / LIKE / DROP TABLE / DROP INDEX / COUNT(DISTINCT).
- Thread-safe engine; packaging via pyproject.toml; GitHub Actions CI.

## 1.0.0 — initial engine

- 4 KB pages + LRU buffer pool, typed rows, B+ tree, CRC'd WAL with crash
 recovery, recursive-descent SQL parser, hash/nested-loop joins,
 GROUP BY/HAVING, three-valued NULL logic, transactions, REPL.
