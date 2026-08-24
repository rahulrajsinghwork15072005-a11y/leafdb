# Devlog

Rough notes kept while building. Mostly the bugs — the features were the easy
part.

## The file that opened itself wrong

First working version could insert and search inside one process, but
everything vanished on reopen. Spent an evening convinced it was my B+ tree
serialization. It was `open(path, "a+b")`. Append mode. On a database file.
Every seek was ignored and every write went to end-of-file, so pages landed
wherever the last write left off. A page written for key 1 would physically
live at offset 8192 if page 2 happened to be written first. Fixed by opening
`r+b` after creating empty files with `wb`. I now check open modes before I
check algorithms.

## Borrows that never happened

Rewrote deletes to do proper borrow/merge rebalancing. Tests passed for happy
paths; the randomized oracle test started failing with "key not found" on
keys that range_scan could still see. Search said no, scan said yes. That
difference means routing and chain disagree — classic stale separator... except
the separators were fine.

The actual bug: `_borrow_from_right` mutated the sibling and child objects in
memory and then wrote *only the parent page*. Both mutated nodes sat in local
variables; the pager cache still held the old images. Every subsequent read
got pre-borrow contents while separators post-dated them. Two lines of
`pager.write(...)` fixed a week's worth of confusion in real time (it was
maybe three hours, but it felt like a week).

## Merges need the separator

Same debugging session, found by adding a `check()` method that walks the tree
asserting: children == keys + 1, separator ≤ min(right subtree), leaf chain
order equals tree order, all leaves at equal depth. First run after enabling
it flagged an internal node with 13 children and 11 separators — exactly two
too few keys. Counted merges: two cascaded internal merges had combined
`left.keys + right.keys` without pulling the parent separator down between
them. The fix is one parameter (`_merge(low, high, sep)`). Without `check()`
running mid-fuzz I would never have found this; without the fuzz loop it
wouldn't have been reached. Lesson written into the test file as a comment to
my future self.

## WAL handles and positions

MVCC sessions mean several Database objects append to one WAL file. Each
opened its own handle; each handle remembered its own position. Session B
appended at its stale offset and overwrote session A's committed batches.
Symptom: sessions couldn't see each other's data, and one session's log
randomly shrank from another's point of view. Fix: every append seeks to
end-of-file first, and commits publish the new log end through an in-process
registry so other sessions know there is something to read.

## Rollback restored zeros

After implementing rollback via shadow paging, one test hung forever inside
range_scan. Dump showed a leaf whose next pointer pointed at page 0, which
parsed as an empty leaf pointing at page 0. Page 1 had been rolled back to
*disk* contents — but the real pre-image only existed in memory and the WAL,
because checkpointing hadn't happened yet. Restoring "from disk" was simply
the wrong source. Rollback now restores shadow pre-images captured when the
transaction first touches each page. Also added the zero-guard to scans so a
corrupt pointer can loop silently ever again.

## flush() means flush

The C++ core kept reading truncated files written by the Python engine.
Python's `flush()` iterated dirty pages calling `file.write(...)` — into the
fstream user-space buffer — and never called `flush()` on the stream unless
fsync was requested. Any other process (or the C++ tool) saw a short file.
One added line. This is why cross-language tests are worth writing even when
single-language tests are green.

## Small pages find big bugs

Most bugs above reproduced only with PAGE_SIZE forced down to ~160 bytes so
trees stayed shallow and splits/merges fired constantly. At 4096 bytes you
need hundreds of thousands of rows to hit the same code paths. If something
is parameterized, make tests exploit that.
