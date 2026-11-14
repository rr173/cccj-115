# session_ledger

Business-time **event → session** maintenance for product usage events that
arrive late, get corrected, and get retracted.  Analysts need sessions defined
by *business time* rather than by ingestion order; this package persists those
facts in SQLite so downstream consumers read maintained tables instead of
re-segmenting on every query.

It is a self-contained, dependency-free Python ≥ 3.10 module.  Nothing here
touches the other modules in the repository, and it never connects to an
online service or builds web pages — every example is generated offline.

## Business rules

* The store is initialised once with a fixed idle gap **G**, a positive integer
  number of milliseconds (booleans are not accepted as integers).
* The full valid payload of an event is exactly `event_id`, `revision`,
  `user_id`, `event_ms`:
  * both identifiers are non-empty strings, compared character by character
    with no case folding;
  * `revision` is a positive integer;
  * `event_ms` is a signed 64-bit integer.
* A user's active events are sorted by `(event_ms, event_id)`.  Consecutive
  events are linked while their time difference is **≤ G**; a session is a
  maximal linked run.  A run's total span can therefore be much larger than G,
  same-timestamp events are all retained (never deduplicated), and a lone event
  is its own session.
* `sessions` rows hold `user_id, session_id, start_ms, end_ms, event_count,
  span_ms`, where `session_id` is the lexicographically smallest member
  `event_id` and `span_ms = end_ms - start_ms`.
* Every active event's membership is persisted in `members`, ordered through an
  index by `(user_id, event_ms, event_id)`.
* Interval queries use **closed intervals** and return every session
  intersecting the window, including sessions that start before it.

## Patches (corrections)

Patches are UTF-8 JSONL.  Each line is one object:

```json
{"op": "put", "event_id": "e1", "revision": 3, "user_id": "u1", "event_ms": 1200}
{"op": "retract", "event_id": "e1", "revision": 4}
```

* `put` carries the complete replacement payload; `retract` carries no
  `user_id`/`event_ms` and marks that revision as withdrawn.
* An identity first appears via `put`.  Larger revisions may skip numbers and
  overwrite the current one.  Equal revision + identical content is a no-op;
  equal revision + different content is a conflict; smaller revisions are
  stale.  Retracting an unknown identity is refused.
* A newer `put` after a retraction restores the identity.  Only the current
  revision and retraction flag are stored, not full history.
* Wrong field count/types, duplicate JSON object keys, the same `event_id`
  twice in one patch, or an unknown-identity retraction names the physical
  line and rejects the **whole** patch.  An empty file is a valid no-op patch.
* A patch is one common new state: the final segmentation never depends on the
  order of its lines (per-line transient states are never the answer).

## Locality and crash semantics

Maintenance works on a small neighbourhood, never by scanning the whole user or
rebuilding every session:

1. seed window `[min(anchors)-G, max(anchors)+G]`;
2. take every old session intersecting it and extend the window to each
   component's full extent, iterating until no new session intersects (this
   covers joint multi-point bridges and bridge-deletion splits);
3. stream the neighbourhood's active events through a cursor in fixed-size
   batches — a giant segment is never materialised as a Python list;
4. verify both outer neighbours have gaps strictly greater than G.

Every ingestion prints auditable counters: effective changes, historical event
rows actually streamed for re-segmentation, boundary probes, indexed lookups,
and session counts before/after.  All writes run in one `BEGIN IMMEDIATE`
transaction, so readers see only the complete pre-patch or post-patch state.
A deliberate `os._exit` hook simulates a crash after headers change but before
session maintenance; a reopened database shows the old state and the same
patch re-ingests cleanly.

## Install / run

No third-party dependencies; `pip` is optional.

```bash
cd session_ledger
python3 -m pip install -e .            # optional; everything also runs in place
session-ledger init ledger.sqlite --gap-ms 10
session-ledger ingest ledger.sqlite patch.jsonl
session-ledger show ledger.sqlite --user alice --from 0 --to 1000
session-ledger events ledger.sqlite --session e1
session-ledger header ledger.sqlite --event e1
session-ledger stats ledger.sqlite
```

Running in place without installation:

```bash
python3 session_ledger/cli.py init ledger.sqlite --gap-ms 10
```

## Python read API

```python
from session_ledger import (
    initialise, parse_patch, ingest,
    sessions_in_interval, session_events, events_in_interval,
    event_header, all_sessions, counts,
)

initialise("ledger.sqlite", idle_gap_ms=10)
stats = ingest("ledger.sqlite", parse_patch("patch.jsonl"))
print(stats.as_dict())

sessions_in_interval("ledger.sqlite", "alice", 0, 1000)   # closed-interval overlap
session_events("ledger.sqlite", "e1")                     # business-time ordered
events_in_interval("ledger.sqlite", "alice", 100, 200)
```

## Tables

| table     | purpose |
|-----------|---------|
| `meta`    | fixed gap G and schema version |
| `events`  | current header per identity, including retracted rows |
| `sessions`| maximal connected segments with all required fields |
| `members` | active event → session membership, time ordered |

## Tests, walkthrough and benchmark (all offline)

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v   # stdlib only
python3 -I demo/walkthrough.py                            # hand-computed timeline
python3 -I bench/locality.py                              # 28k segments + giant chain
```

* `tests/test_differential.py` runs 180 random correction rounds against a
  test-only naive full-rescan segmenter (`session_ledger.naive`, which never
  calls the production neighbourhood code) and compares every session field
  and per-event membership, not just counts.
* `demo/walkthrough.py` builds the full hand-computed timeline in
  `docs/WALKTHROUGH.md`, including the real-subprocess `os._exit` rollback.
* `bench/locality.py` records real read stats, wall time, tracemalloc peak and
  `EXPLAIN QUERY PLAN` evidence for a 28,000-session sparse world, a second
  user with 8,000 sessions, and a 20,000-event giant connected segment.
