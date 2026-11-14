# log_templates

Turns an unstructured UTF-8 log stream into **templates plus per-position
parameters**, keeping a record that reconstructs every message
character-for-character.  Pure Python 3.10+, durable state in SQLite, no
network services, no changes to the other modules in this repository.

A message such as

```
job 17 started on node a1
job 19 finished on node b2
```

yields template `job <INT> <*> on node <*>` with parameter positions carrying
the masked `INT` at position 1 and the raw words at the wildcard positions.
Messages are never rewritten: each one keeps the template revision that was
current when it was accepted, plus the raw word text and the exact whitespace
gap strings.

---

## 1. Install and run

```bash
cd log_templates
python3 -m pip install -e .          # optional; `python3 -m logtempl.cli` works anyway
python3 -m pytest                    # 183 tests
```

The CLI is available as `python3 -m logtempl.cli` (or the installed
`logtempl` script).

## 2. Command operations

### Create a library

```bash
python3 -m logtempl.cli create LIB.db \
    --threshold 0.5 \
    --depth 2 \
    --template-cache 64 \
    --node-cache 256 \
    --masks IPV4,UUID,INT
```

| flag | meaning |
| --- | --- |
| `--threshold` | exact decimal in `(0, 1]`, fixed for the library |
| `--depth` | number of leading words indexed per template (0 allowed) |
| `--template-cache` / `--node-cache` | entry caps for the two in-memory caches |
| `--masks` | enabled mask rules, fixed for the library |

`depth` and the cache caps are re-open parameters; `threshold` and the rule
set are fixed at creation because changing them would re-interpret existing
templates.

### Ingest a stream

Input is one JSON object per line, UTF-8:

```json
{"source": "web-1", "seq": 0, "message": "GET /index 200 10.0.0.1"}
```

```bash
python3 -m logtempl.cli ingest LIB.db stream.jsonl
python3 -m logtempl.cli ingest LIB.db stream.jsonl --batch-size 1000 --token nightly
```

Output: `accepted=N replays_ignored=R templates_created=C
templates_revised=V index_nodes_visited=X candidates_examined=Y`.

Exit codes: `0` ok, `2` usage/library error, `3` seq gap (batch refused),
`4` replay conflict (batch refused), `5` malformed input line.

### Query

```bash
python3 -m logtempl.cli list-templates LIB.db
python3 -m logtempl.cli show-template LIB.db 1                 # current revision
python3 -m logtempl.cli show-template LIB.db 1 --revision 2    # a historical structure
python3 -m logtempl.cli show-template LIB.db 1 --history       # every revision
python3 -m logtempl.cli members LIB.db 1                       # members of the template
python3 -m logtempl.cli members LIB.db 1 --revision 1          # members of one revision
python3 -m logtempl.cli show-message LIB.db web-1 42           # original + rebuilt text
python3 -m logtempl.cli show-message LIB.db web-1 42 --json    # with parameters
python3 -m logtempl.cli parameters LIB.db web-1 42
python3 -m logtempl.cli stats LIB.db
python3 -m logtempl.cli verify LIB.db
python3 -m logtempl.cli rebuild-index LIB.db
```

* **Reconstruct original text**: `show-message` reads the stored raw words and
  whitespace gaps; `"exact": true` means the rebuild equals the accepted text.
* **Structure of a template revision**: `show-template --revision`.
* **Members and counts**: `members`, plus `template_stats.member_count /
  total_count` in `list-templates`.

## 3. Contract summary

### Tokenisation and whitespace

Words are maximal runs of non-whitespace characters; every run of whitespace
before, between and after words is stored verbatim, so
`gap[0] + word[0] + gap[1] + … + gap[n]` is the original message.  Empty and
all-whitespace messages are recorded with zero words and score 1 against a
zero-word template.

### Masks (whole word only)

Fixed rule order `IPV4, UUID, INT`, configurable as a set per library:

* `IPV4` — canonical dotted quad, each octet 0..255; `256.0.0.1`, `01.2.3.4`,
  `1.2.3` and `1.2.3.4.` stay literal;
* `UUID` — canonical 8-4-4-4-12 hyphenated hex form, case-insensitive;
* `INT` — optional `+`/`-` and at least one ASCII digit; `1_000`, `1.0`,
  `+`, non-ASCII digits stay literal.

A word only masks when the *whole* word matches.  Items are stored with their
kind, so a literal word spelled `<*>` renders as `LIT(<*>)` and is never
mistaken for a learned wildcard.

### Matching

Score for a message of `W` words against a template of `W` items is
`points / W`, where each position scores 1 only if both sides are fixed and
equal (same literal, or same mask kind).  Wildcards and differing positions
score 0.  A candidate is admissible when its score is at least the threshold.
Ties are broken by fewer template wildcards, then lower template id.  Arrows:

```
score   = points / W                    (W > 0)
score   = 1                             (W == 0)
accept  <=> score >= threshold
tie     : (-points, wildcards, template_id)
```

On acceptance, differing fixed positions become wildcards; equal mask kinds
stay masked.  If nothing differs, the message references the current revision
and no new revision is created.

### Index

One trie per word count.  Node edges may be `lit:<text>`, `ipv4`, `uuid`, `int`
or `wild`, over the first `depth` words.  Lookup expands *all* branch types
(not just the literal node), prunes with the optimistic bound
`(points_so_far + unvisited_words) / W`, and reports `nodes_visited` and
`candidates_examined`.  Every generalization re-links the template leaf in the
same transaction as the revision bump.

Template items and index nodes are also cached in capped, insertion-ordered
caches; every cached value is recoverable from SQLite, so eviction cannot
change any result.

### Revisions

A library-wide monotone revision counter produces a new number on every
template creation or generalization.  A message stores the revision it was
accepted under, its classified items and raw word text, and its gap strings.
`show-template --revision` and `members --revision` read those immutable
snapshots.

### Position protocol

Per source, `seq` starts at 0.  Replay of a committed position with identical
text is ignored (no writes, no revision).  Replay with different text is a
conflict and the whole batch is refused.  A skipped position is a gap and the
whole batch is refused.  All of a batch's template changes, revision numbers,
index links, message rows and per-source next positions commit in one SQLite
transaction, so a refused or crashed batch leaves the previous state intact.

### Fault point and recovery

Set `LOGTEMPL_FAULT_BEFORE_MESSAGE=<source>:<seq>` to make the named record's
processing call `os._exit(23)` after the template and index work is staged but
before the message row is written.  The uncommitted transaction rolls back on
the next open.  Re-running the original batch produces the same template ids,
revision numbers and membership as a fault-free run.

## 4. Independent expected materials

Everything in `materials/` derives its expectations from either hand arithmetic
or `tests/reference.py` (a naive full-scan re-implementation of the contract),
never from the production matcher.

```bash
# regenerate the expectations and the JSONL inputs (refuses to rewrite a
# changed transcript without explicit deletion)
python3 materials/expected_materials.py --write

# execute every scenario through the CLI and compare the observed database
# facts with the transcript
python3 materials/expected_materials.py --check

# narrated walkthroughs (assert as they go)
python3 materials/demo.py
python3 materials/fault_and_replay_demo.py
```

* `materials/expected_transcript.json` — for each scenario: expected chosen
  template id and revision per message, expected template render per revision,
  pairwise point scores and threshold verdicts, and per-message parameter
  values.
* `materials/streams/*.jsonl` — the input streams.
* `tests/test_materials.py` — re-derives the transcript from the reference,
  hand-checks selected scores, and runs the CLI end-to-end.

## 5. Scenarios covered by tests

| scenario | where |
| --- | --- |
| gradual generalisation of one fixed position, exact rebuild before/after | `materials/demo.py`, `test_scenarios.py`, transcript `gradual` |
| generalisation inside the first `depth` words, index re-link, new message still matches | `materials/demo.py` §2, `test_index.py`, transcript `prefix_relink` |
| equal-score tie, id and wildcard tie-break | `test_scenarios.py`, transcript `tie`/`wildcard_tie` |
| different mask kinds meeting | `test_scenarios.py`, transcript `masks` |
| illegal IP keeps the literal word | `test_scenarios.py`, transcript `masks` |
| literal `<*>`, not a wildcard | `test_scenarios.py`, transcript `literal_wildcard` |
| whitespace runs, empty messages, exact rebuild | `test_text_and_masks.py`, transcript `whitespace` |
| many templates with tiny caches, restart equals one big-cache run | `test_cache.py` |
| fault point, crash rollback, post-recovery equivalence | `test_fault_and_recovery.py`, `materials/fault_and_replay_demo.py` |
| replay idempotence, conflict, gap refusal | `test_ingestion_protocol.py` |
| determinism across `PYTHONHASHSEED` | `test_config_and_determinism.py` |
| threshold exactness (no float) | `test_scoring_and_selection.py` |
