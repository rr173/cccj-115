# Wire protocol `iblt-handover/1`

This document is the normative description of the bytes exchanged between the
two parties. Anything not written here is an implementation detail.

## 1. Element

```
element = uuid (16 bytes) || sha256_fingerprint (32 bytes)     = 48 bytes
```

* `uuid` and `sha256_fingerprint` are decoded from fixed-width hexadecimal
  text (32 and 64 hex characters). Comparison is on bytes.
* Checklist parsing rejects anything that is not exactly one UUID field and
  one fingerprint field, including wrong widths, non-hex characters, extra
  fields and blank lines.
* One checklist may not contain the same UUID with two fingerprints.

## 2. Digest file

```
 digest = header || body || trailer
 header = magic(8) || protocol(16) || capacity(u32) || k(u32)
          || seed(u64) || unique_elements(u64) || source_lines(u64)
          || duplicate_lines(u64)
 body   = capacity * 64 bytes
 trailer= SHA256(header || body)
```

All integers are unsigned big-endian; `count` inside a cell is signed
big-endian. `protocol` is `"iblt-handover/1"` padded with NUL bytes to 16.
The file size is exactly `96 + 64 * capacity`, independent of how many
identities were folded in.

### Cell

```
cell = count(i64) || key_xor(48 bytes) || check_xor(8 bytes)
```

* `count` — signed sum of the elements that hash to this cell.
* `key_xor` — XOR of every full 48-byte element that hashes here.
* `check_xor` — XOR of the 8-byte checksums of those elements.

## 3. Hashing

```
BE64(x) = unsigned big-endian interpretation of 8 bytes

h1(E,s) = BE64(SHA256("iblt-handover/hash-cell/v1" || 0x69 || BE64(s) || E))
h2(E,s) = BE64(SHA256("iblt-handover/hash-cell/v1" || 0x6a || BE64(s) || E)) | 1

index_i = (h1 + i*h2 + i*i) mod capacity      i = 0..k-1
```

If `index_i` is already in the element's chosen set, increment it modulo
capacity until it is not. Consequently an element touches exactly `k`
distinct cells and the function is deterministic for a given
(capacity, k, seed).

```
check(E) = SHA256("iblt-handover/check/v1" || E)[0:8]
```

Note that `h1`/`h2` cover the **whole** 48-byte element, so a recovered cell
key is the actual UUID plus fingerprint, not a truncated digest.

## 4. Insertion

For each unique element (and each occurrence with its sign):

```
for each index i in index_0..index_{k-1}:
    cell[i].count += sign              # +1 insert, -1 remove
    cell[i].key_xor ^= element
    cell[i].check_xor ^= check(element)
```

## 5. Difference and peeling

```
diff.cell[i].count      = left.cell[i].count      - right.cell[i].count
diff.cell[i].key_xor    = left.cell[i].key_xor    ^ right.cell[i].key_xor
diff.cell[i].check_xor  = left.cell[i].check_xor  ^ right.cell[i].check_xor
```

A cell is **pure** iff

1. `count != 0`,
2. `abs(count) == 1`,
3. `check_xor == check(key_xor)`.

Peeling takes a pure cell, recovers `element = key_xor` with
`sign = signum(count)` and then applies insertion with `sign = -sign` to all
`k` cells of the element. Before accepting the element the implementation
also verifies `cell_index(element)` contains the index of the pure cell.

The output is then grouped by UUID:

| peeled items for one UUID | reported row |
| --- | --- |
| one item, `sign = +1` | `LEFT_ONLY`, fingerprint from the element |
| one item, `sign = −1` | `RIGHT_ONLY`, fingerprint from the element |
| two items, signs `+1` and `−1` | `CONTENT_CHANGED`, one fingerprint per side |
| two items, same sign | error: input was not a set |
| identical fingerprints with opposite signs | error: must have cancelled |
| three or more items | error: not a set / untrustworthy digest |

Complete success requires every cell of the difference table to be all-zero.
Otherwise the comparison is `INCOMPLETE`; no difference list is returned and
the residual scale is reported.

## 6. Header validation at load

A digest is rejected if any of these fail:

* file shorter than 96 bytes, or wrong total length for its capacity;
* magic differs from `HNDIBLT1`;
* protocol name differs from `iblt-handover/1`;
* `capacity` outside `1..2^20`, `k` outside `1..16`, `k > capacity`;
* SHA-256 trailer does not match;
* a cell count is `INT64_MIN`, or `|count| > 10^9`;
* `count != 0` while both `key_xor` and `check_xor` are all zero;
* header `unique_elements` differs from `k * sum(count) / k` derived from the
  cells;
* `duplicate_lines > source_lines` or `unique_elements > source_lines`.

Two digests may be compared only when `(capacity, k, seed)` are identical.
Otherwise the comparison is refused before subtraction; neither digest is
modified.

## 7. Probability statement

Fingerprints use SHA-256 for content identity and the cell checksum is the
first 8 bytes of a domain-separated SHA-256. These are probabilistic
arguments. The practical bounds:

* two distinct contents colliding in the 32-byte fingerprint: ≈ 2⁻¹²⁸;
* a random 48-byte key producing a matching 8-byte cell check: ≈ 2⁻⁶⁴, and it
  must additionally hash into the pure cell being examined;
* a fully saturated table cannot peel and is reported as incomplete, not as
  an empty difference.

The tool never claims mathematical certainty; it reports “complete” only when
the arithmetic finished with an all-zero table.
