# Design notes

## Why these boundaries

This module is deliberately a *new, standalone* package: it does not import
from `dwca_archive`, `handover_iblt`, `occupancy`, or `pointcloud_las`, and
none of those import it.  Fusion with any of them would couple unrelated
contracts (archive dialect parsing, digest exchange, statistical models,
LAS headers) to log structuring, which the request explicitly forbids.

## Data model

| table | role | authority |
| --- | --- | --- |
| `meta` | config JSON, global revision/tx/template counters | authoritative |
| `sources` | per-source `next_seq` and message count | authoritative |
| `templates` | current template head | authoritative |
| `template_items` | current items, one row per position | authoritative |
| `template_revs` + `rev_items` | immutable revision snapshots | authoritative |
| `messages` | accepted records with template id + revision | authoritative |
| `message_words` | classified item and raw word per position | authoritative (parameters and rebuild) |
| `message_gaps` | preserved whitespace strings | authoritative (rebuild) |
| `template_stats` | member/total counters | derived, verified against `messages` |
| `index_nodes` + `index_leaves` | candidate index | derived, rebuildable |
| `batches` | accepted batch observability | append-only |

"Derived" tables are verified by `verify` against the authoritative rows and
can be regenerated with `rebuild-index`.  The two caches (`template items`,
`index subtrees`) are in-memory only; nothing in them is authoritative, which
is why eviction is invisible in results.

## Tokenisation invariant

`Tokenised` guarantees `gaps[i]` lies between `word[i-1]` and `word[i]`, and
`len(gaps) == len(words) + 1`.  `reassemble()` is a pure inverse of
`from_message()`.  The database stores words and gaps separately precisely so
that later template rewrites cannot lose spacing.

## Scoring, thresholds and ties

`Fraction(Decimal(text))` keeps the threshold exact; `points / W` is compared
as a rational, never as a float.  Scores use integer points so there is no
rounding at all.  Candidate ordering is the explicit key
`(-points, wildcards, template_id)`: encoded integers only, no `id()`, no
`hash()`, no set iteration.  Template ids and revision numbers come from
`meta` counters updated inside the transaction, so a crashed run cannot consume
or skip a number.

## Index shape

Words counts are separated, because templates of differering lengths can never
match.  Within a word count, the trie path is the first `min(depth, W)` items.
Templates with `W < depth` occupy the shallower leaf level, which is legal
because their word-count trie contains only such templates (leaf level is
uniform per trie).

Lookup is a best-first search rather than a single descend: a branch whose own
position mismatches may still reach the threshold from other positions.
Branches are scored with the optimistic bound `(points + unvisited)/W`, and the
heap orders by that bound, so a bound below the threshold at the top of the
heap proves no unexplored branch can admit.  `lookup` returns the visit
counters so tests can assert the walk is sub-linear in template count.

Relinking is mandatory on generalization: the leaf's path depends on the
current items.  `IndexWriter.relink` deletes and re-creates the leaf inside the
same transaction as the revision, and `verify` recomputes the expected node
from current items.

## Atomicity and the fault point

Every batch runs in one `BEGIN IMMEDIATE` … `COMMIT` with rollback journal and
`synchronous=FULL`.  The fault hook fires after template creation or
generalization (rows, revisions, relink) and before `_register_message`.
`os._exit` skips every Python finalizer, but SQLite's journal makes the
uncommitted transaction disappear on the next open.

The preflight scan validates gaps and replays *before* a write transaction
starts, so refusals do not even consume a tx id.  Inside the write transaction
the expected position is re-checked (a concurrent writer might have advanced
it); if it moved, the batch rolls back and reports a retryable refusal.

## Replay and gaps

Replays are recognized by `(source, seq)` existing in `messages`.  Identical
text is a no-op; different text is a conflict for the entire batch.  Positions
below `next_seq` that are not in `messages` cannot occur in a consistent
database, but are treated as gaps defensively.

## Determinism

`PYTHONHASHSEED` is irrelevant: no code path orders by `set` iteration, dict
insertion of hash-randomized keys, or object identity.  `test_config_and_determinism.py`
runs the whole CLI under four seeds and compares listings and histories byte
for byte.  A `random_seed` option exists only as recorded metadata and is never
consulted.

## Cache coherency

The template cache only holds item tuples.  Creation does not invalidate other
templates, so it only clears the *node* cache (whose children/leaves changed).
Generalization drops that template's item entry and clears node-derived entries
(a leaf moved).  Both caches being warmer/colder cannot alter scores because
loaders read identical rows.

## Known limits

* Per-position gaps are stored for every message, which is proportional to
  message length; that is the price of exact reconstruction.
* Deleted index leaves/nodes are never garbage-collected, so a pathological
  sequence of generalizations can leave unreachable nodes.  They are harmless
  (lookup never visits them) and `rebuild-index` reclaims them.
* `threshold` is fixed at creation; changing it would reinterpret stored
  templates, so the library refuses.
