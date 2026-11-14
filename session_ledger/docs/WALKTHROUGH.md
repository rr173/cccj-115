# Hand-computed walkthrough (G = 10 ms)

This is the exact timeline exercised by `demo/walkthrough.py`.  Every session
below is computed by hand from the rule "sort by `(event_ms, event_id)`; link
consecutive events while the gap is ≤ G"; the script asserts the database
matches each row and every per-event membership.

Notation: `id@ms`.  A session is written as `session_id [start,end] count=N
span=… -> [members in business-time order]`.

## R0 — equal-to-G link, just-over-G split, same instant, long chain

Inputs (all revision 1):

| id | user | ms |
|----|------|----|
| a | alice | 0 |
| b | alice | 10 |
| c | alice | 21 |
| d | alice | 35 |
| e | alice | 35 |
| f | alice | 45 |
| g | alice | 60 |
| x | bob | 0 |

Adjacent gaps: a→b = **10 = G → linked**; b→c = **11 > G → split**; c→d = 14
split; d,e share the **same instant 35, both kept, ordered d,e**; e→f = 10
linked; f→g = **15 > G → split**.

* `a [0,10] n=2 span=10 -> [a,b]`
* `c [21,21] n=1 span=0 -> [c]`
* `d [35,45] n=3 span=10 -> [d,e,f]`
* `g [60,60] n=1 -> [g]`
* `x [0,0] n=1 (bob) -> [x]`

Stats: 8 effective changes, 8 historical rows streamed (the whole initial
timeline), 5 sessions created.

## R1 — bridge points merge sessions

Add `h@15` and `i@28` (revision 1).  Gaps become b→h = 5, h→c = 6, c→i = 7,
i→d = 7, all ≤ G.  The three alice sessions a-b, c, d-e-f merge into one chain
whose span (45) is much larger than G.  g@60 stays 15 ms from f@45, so the
right-hand boundary probe stops and g's singleton is neither modified nor read
(the run streams 8 rows, not 9).

* `a [0,45] n=8 span=45 -> [a,b,h,c,i,d,e,f]`
* `g [60,60] n=1 -> [g]`
* `x [0,0] n=1 (bob)`

## R2 — retract the bridges, chain splits again

Retract h and i (revision 2).  The chain breaks at exactly the R0 boundaries:

* `a [0,10] -> [a,b]`; `c [21,21] -> [c]`; `d [35,45] -> [d,e,f]`;
  `g [60,60] -> [g]`; `x [0,0] (bob)`.
* Headers of h and i remain in `events` with `is_retracted = 1`; no membership
  rows point at them.

## R3 — delete a segment's minimum identity, session id migrates

Retract d (revision 2), the lexicographically smallest id of the `d` segment.
The old session row is removed; surviving members repoint to a segment named
after the new minimum:

* `e [35,45] n=2 span=10 -> [e,f]`

## R4 — cross-user move and time change in one common patch

* e revision 3 moves to **bob @7**.
* f revision 2 stays with alice but moves to **@9**.

The patch represents one joint state, not per-line transients:

* alice: a@0, f@9, b@10 all within G → `a [0,10] n=3 -> [a,f,b]`
  (business-time order, even though f was inserted later); c and g unchanged.
* bob: x@0 and the moved e@7 are 7 apart → `e [0,7] n=2 span=7 -> [x,e]`
  (session id is the minimum member id, e, not x).

## R5 — same-batch multi-point joint bridging plus a simultaneous retract

Add p@30, q@40, r@50 and retract a (revision 2) in one patch.

* With a gone, f@9,b@10 end a segment whose next event c@21 is 11 ms away
  (> G): `b [9,10] n=2 span=1 -> [f,b]`.  The old segment-minimum id a
  disappears and the surviving segment is renamed after b.
* c@21 → p@30 (9) → q@40 (10) → r@50 (10) → g@60 (10) are jointly connected:
  no single new point bridges the whole stretch, the batch does.
  `c [21,60] n=5 span=39 -> [c,p,q,r,g]`.
* bob's `e [0,7] -> [x,e]` is untouched.

## R6 — retract an entire session away, then restore it

Retract x (revision 2): bob's session had members x,e, so it shrinks to the
singleton `e [7,7]`; no empty session row remains anywhere.  Then restore x
with revision 3 at bob @5: x@5 and e@7 rejoin into `e [5,7] n=2 span=2`.

## R7 — repeat the identical patch

Re-send the R6b patch.  Same revision, same content → **all no-op**:
`effective_changes = 0`, `noops = 1`, zero historical rows streamed, session
count unchanged.

## R8 — out-of-order delivery, stale revision, revision jump

First store z revision 3 (bob @100).  A later-arriving patch contains a valid
new w plus z **revision 2**: the stale line aborts the whole patch, so w is not
inserted and z keeps its revision-3 value.  A subsequent z **revision 5**
(jumping over 4) is accepted and overwrites revision 3.

## R9 — equal-revision conflict, earlier valid line must not apply

A patch first puts a brand-new k, then puts f at the same revision f already
has but with a different time.  The conflict rejects the entire patch: a
before/after snapshot of all sessions and members is byte-identical, k never
exists.

## Structural refusals (whole patch rejected, line number reported)

extra field; `retract` carrying `user_id`; boolean `revision`; int64 overflow
on `event_ms`; empty `user_id`; duplicated JSON object key; the same
`event_id` twice in one file; retracting an unknown identity; blank/non-JSON
lines.  An empty file is accepted as a legal no-op patch.

## Crash hook — real subprocess, real rollback

`session-ledger ingest … --exit-after-event-writes` calls `os._exit(42)`
after the event headers have been changed inside the open transaction but
before any session maintenance.  The walkthrough runs this in a child process
with `python3 -I`, then from a new process reads back:

* the header of the targeted event still shows its old (retracted) state;
* sessions and members equal the pre-patch snapshot exactly;
* re-ingesting the identical patch then succeeds, and repeating it again is a
  full no-op.

## What the counters cover

* `effective_changes`: lines that changed stored state (inserts, higher
  revision replacements/retractions/restores).
* `resegment_rows_read`: active event rows actually streamed through cursors
  during neighbourhood rebuilds — including rows that end up in unchanged
  sessions, so it counts database reads, not only rows written.
* `boundary_probes`: the two outer-neighbour index probes per affected user
  that justify stopping neighbourhood expansion.
* `index_lookups`: every indexed point/range lookup (header decisions, session
  range probes, event window scan, neighbour probes).
* `sessions_before` / `sessions_after`: total session rows across the store.
