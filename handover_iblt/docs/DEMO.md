# Handover demo — transcript and story

Everything shown below is produced by `handover-iblt demo`, which invokes the
real command line as subprocesses. Checklist materials are written by
`handover_iblt/materials.py` from literal UUIDs and SHA-256 fingerprints; no
specimen payload is involved.

Start the demo in an empty directory:

```bash
handover-iblt demo --workdir /tmp/handover-demo \
    --capacity 32 --k 4 --seed 20261113
```

## 1. Sender has A, B, C; receiver has B, C, D

```
$ handover-iblt build sender.txt sender.digest --capacity 32 --k 4 --seed 20261113
built digest from sender.txt
  checklist lines read: 3
  unique elements:      3
  duplicate lines:      0
  identity store:       (in-memory SQLite)
  digest:               sender.digest
  digest size:          2144 bytes (32 cells x 64 bytes + 96)
$ handover-iblt build receiver.txt receiver.digest --capacity 32 --k 4 --seed 20261113
...
$ handover-iblt compare sender.digest receiver.digest \
      --left-name sender --right-name receiver
left  (sender): 3 unique elements
right (receiver): 3 unique elements
configuration: {"capacity": 32, "k": 4, "protocol": "iblt-handover/1", "seed": 20261113}
STATUS: COMPLETE — table fully peeled to zero
differences: 2 (LEFT_ONLY=1, RIGHT_ONLY=1, CONTENT_CHANGED=0)
  LEFT_ONLY        uuid=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa left_fingerprint=86f1…6c6c (present on left only)
  RIGHT_ONLY       uuid=dddddddddddddddddddddddddddddddd right_fingerprint=07b8…0952 (present on right only)
```

The UUIDs and fingerprints above are abbreviated here; the demo prints them
in full. `A` is sender-only and `D` is receiver-only.

## 2. One specimen changed content

Receiver keeps `B`, keeps `C` but with a revised fingerprint, and has `D`:

```
$ handover-iblt compare chg_sender.digest chg_receiver.digest \
      --left-name sender --right-name receiver
STATUS: COMPLETE — table fully peeled to zero
differences: 3 (LEFT_ONLY=1, RIGHT_ONLY=1, CONTENT_CHANGED=1)
  LEFT_ONLY        uuid=aaaa…aaaa left_fingerprint=86f1…6c6c (present on left only)
  CONTENT_CHANGED  uuid=cccc…cccc left_fingerprint=62bd…eb4 right_fingerprint=37bc…546 (same identifier, different content)
  RIGHT_ONLY       uuid=dddd…dddd right_fingerprint=07b8…0952 (present on right only)
```

`C` is **one** row carrying both fingerprints, not an unrelated loss plus an
unrelated extra.

## 3. Swapping the arguments reverses direction

For the same two files:

| invocation | A | D |
| --- | --- | --- |
| `compare sender.digest receiver.digest` | `LEFT_ONLY` | `RIGHT_ONLY` |
| `compare receiver.digest sender.digest` | `RIGHT_ONLY` | `LEFT_ONLY` |

The direction labels are always relative to the argument order; nothing is
guessed from file names.

## 4. Identical content, reordered, duplicated

`sender` lists A, B, C, A; `receiver` lists C, A, B, B. Both digests are
identical, and the comparison prints:

```
STATUS: COMPLETE — table fully peeled to zero
differences: 0 (LEFT_ONLY=0, RIGHT_ONLY=0, CONTENT_CHANGED=0)
```

## 5. Duplicates and same-identifier conflicts

```
$ handover-iblt build duplicates.txt dups.digest --capacity 32 --k 4 --seed 20261113
  checklist lines read: 5
  unique elements:      2
  duplicate lines:      3
```

A one-UUID-two-fingerprints checklist is refused:

```
$ handover-iblt build conflict.txt conflict.digest ...
conflict: checklist conflict: identifier aaaaaaaa…aaaaaaaa has fingerprint
86f1…6c6c at line 1 and fingerprint 37bc…7546 at line 4; refusing to build a digest
```

No `conflict.digest` exists afterwards.

## 6. Malformed hexadecimal

```
line 2: fingerprint is not valid hexadecimal
line 1: UUID must be 32 hex characters, got 30
line 1: fingerprint must be 64 hex characters, got 62
line 1: expected UUID and fingerprint, got 3 fields
```

Every case leaves no digest file.

## 7. Different configurations are incomparable

```
$ handover-iblt compare sender.digest sender_wide.digest
incomparable: digests use different configurations and cannot be compared:
left={'protocol': 'iblt-handover/1', 'capacity': 32, 'k': 4, 'seed': 20261113}
right={... 'capacity': 64 ...}; rebuild both from their original checklists
```

Exit status `5`; both digest files are unchanged byte for byte.

## 8. A digest-size and streaming note

With `--capacity 32`, 3, 500 and 5,000 checklist rows all produce a **2,144-byte**
digest:

```
    3 checklist rows -> 2144 byte digest
  500 checklist rows -> 2144 byte digest
 5000 checklist rows -> 2144 byte digest
```

`test_streaming_memory_does_not_grow_with_checklist` additionally builds a
132,000-line checklist (12,000 exact duplicates) and asserts memory stays
within a fixed band while the store reports 120,000 unique elements. The
SQLite store holds bytes on disk; only one batch of identities is ever in
Python.

## 9. A residual cycle that cannot be peeled, then a larger retry

Material: eight common entries, sender adds `P = 3f…3f`, receiver adds
`Q = b5…b5`. With `capacity=16, k=2, seed=5`, P and Q hash to **exactly the
same cell pair, [12, 13]** (verify with the documented hash formula).

```
$ handover-iblt build cycle_left.txt  cycle_left.digest  --capacity 16 --k 2 --seed 5
$ handover-iblt build cycle_right.txt cycle_right.digest --capacity 16 --k 2 --seed 5
$ handover-iblt compare cycle_left.digest cycle_right.digest
STATUS: INCOMPLETE — no difference list is claimed
  peeling did not reach an empty table: 2/16 cells remain (sum|count|=0, max|count|=0);
  no complete difference list is available. Increase capacity and rebuild both digests ...
  residual: 2/16 nonzero cells, sum|count|=0, max|count|=0
  residual cell indexes (first 10): 12,13
```

Exit status `3`. The two residual cells hold `{+P, −Q}`, so `abs(count)` is 2,
never 1: there is no pure cell to start from. The tool does **not** print a
difference list and does not pretend the handover succeeded.

Rebuild both digests from the original checklists with a larger table:

```
$ handover-iblt build cycle_left.txt  cycle_left_retry.digest  --capacity 64 --k 2 --seed 5
$ handover-iblt build cycle_right.txt cycle_right_retry.digest --capacity 64 --k 2 --seed 5
$ handover-iblt compare cycle_left_retry.digest cycle_right_retry.digest
STATUS: COMPLETE — table fully peeled to zero
differences: 2 (LEFT_ONLY=1, RIGHT_ONLY=1, CONTENT_CHANGED=0)
  LEFT_ONLY        uuid=0000000000000000000000000000003f ...
  RIGHT_ONLY       uuid=000000000000000000000000000000b5 ...
```

At capacity 64 the two elements map to different cells, so ordinary peeling
recovers them.

## Independent expected cells for a tiny table

`tests/expected.py` recomputes a complete 8-cell table from the documented
formula, independently of the decoder. For `capacity=8, k=3, seed=7` and
`X=…01, Y=…02, Z=…03` (zero fingerprints, inserted as X +1, Y +1, Z −1):

```
X -> cells [3, 4, 5]   check f3b0cf9d24a15c5f
Y -> cells [7, 3, 1]   check 75f6d851ab0a3844
Z -> cells [7, 0, 1]   check 37dceb479da82f51

cell 0: count -1  key=Z  check=37dceb479da82f51
cell 1: count  0  key=X^Y  check=422a331636a21715
cell 2: count  0  key=0  check=0000000000000000
cell 3: count  2  key=X^Z  check=864617cc8fab641b
cell 4: count  1  key=X    check=f3b0cf9d24a15c5f
cell 5: count  1  key=X    check=f3b0cf9d24a15c5f
cell 6: count  0  key=0    check=0000000000000000
cell 7: count  0  key=Y^Z  check=422a331636a21715
```

The test asserts each cell of the real table against these hand-derived
values, then peels and compares the recovered signed elements to the original
set's two-sided difference.
