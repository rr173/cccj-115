# Neighbourhood maintenance: expansion and stop basis

Only the neighbourhood affected by a patch is rebuilt.  This document states
exactly how the neighbourhood is derived, why it is complete, and how it is
read without loading a large segment into Python memory.

## Definitions

For one user, active events are totally ordered by `(event_ms, event_id)`.  An
edge between consecutive events is **closed** if their gap is ≤ G and **open**
otherwise.  Sessions are exactly the runs between open edges; they are maximal
by construction.

A patch line changes at most one identity.  Its *anchors* are the business
times of:

* the event's old position (when a stored active event is replaced, moved or
  retracted);
* the event's new position (for every `put`).

All anchors of one user in the patch are pooled because a patch is one common
new state; several changes can jointly alter one chain of edges.

## Lemma: which edges can change status?

An edge's status can change only if at least one of its two endpoint events is
an anchor event, or if a newly inserted/deleted event appears next to it.  In
time terms, every affected edge has an endpoint within G of some anchor: a new
event at time t can only close an edge to an event in `[t-G, t+G]`, and a
deleted event only exposes the gap between its two former neighbours.

Therefore any active event strictly outside
`[min(anchors)-G, max(anchors)+G]` is incident only to edges whose two
endpoints and relative order are unchanged; those edges keep their old status.

## Closure algorithm

`store._rebuild_neighbourhood` maintains a time window `[lo, hi]` and a set of
old session ids `affected`:

1. Seed `lo = min(anchors) - G`, `hi = max(anchors) + G`.
2. Query (via `idx_sessions_user_range`) every old session intersecting
   `[lo, hi]`.  A session is one connected component, so if any of its edges
   could change, its entire membership must be rebuilt: add its id to
   `affected` and extend the window to the session's full `[start_ms,
   end_ms]`.
3. Repeat the intersection query against the widened window until a pass adds
   no new session.
4. Delete the affected sessions (chunked; `ON DELETE CASCADE` removes their
   memberships), stream all active events in the final window through a
   cursor in `BATCH = 512` fetchmany groups, re-segment the runs, and
   materialise them through a staging table.
5. Probe the nearest active events immediately outside both window edges and
   assert both gaps are strictly greater than G
   (`store._verify_outer_edges`).  These are the counted `boundary_probes`.

## Why the stop condition is sound

When a pass finds no intersecting new session, the closest old session left of
`lo` ends before `lo`, and the closest right of `hi` starts after `hi`.  The
final outer-edge probes prove both gaps exceed G.  Because untouched sessions
are themselves maximal, every session further away is separated by an even
larger gap; no chain of ≤ G links can reach them from the changed region.
Sessions beyond the probes are provably unaffected and are never read.

This handles the required shapes without assuming only one edge moves:

* **bridge insertion / move** may connect both left and right neighbours in the
  same patch — the closure absorbs both sessions;
* **bridge deletion** may split one component into several runs — the whole
  component was in `affected`, so every fragment is produced by the same
  streamed re-segmentation;
* **joint multi-point bridging** (multiple new events jointly connecting a
  chain of formerly separate sessions) widens the window over several passes
  until the full chain is included;
* **cross-user moves** anchor both users; each user rebuilds independently,
  stale memberships are removed by identity, and globally unique
  event-derived session ids are reconciled inside the single transaction.

## Bounded memory

The rebuild cursor is consumed with `fetchmany(BATCH)`; only the current run
plus a `BATCH`-sized staging buffer exists in Python at any moment, even when
the affected component is a giant connected segment.  Sessions and memberships
are then materialised with set-based `INSERT … SELECT … GROUP BY` statements.
The benchmark in `bench/locality.py` shows a 20,000-event forced component
re-read staying around 1.6 MiB tracemalloc peak.

## Transaction and visibility

Revision decisions are validated against stored headers before any write.
All header, session and membership changes then happen in one
`BEGIN IMMEDIATE` transaction.  Any error — including the deliberate
`os._exit` crash hook — discards the transaction, so a reopened database shows
the complete pre-patch state and the same patch can be re-ingested.
