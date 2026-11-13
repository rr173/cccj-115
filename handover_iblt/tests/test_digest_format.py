"""Digest format validation: tampering, truncation and impossible cells."""

from __future__ import annotations

import hashlib
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handover_iblt.config import DigestConfig  # noqa: E402
from handover_iblt.digest import (  # noqa: E402
    HEADER_BYTES,
    TRAILER_BYTES,
    build_digest_from_store,
    load_digest,
    parse_digest_bytes,
)
from handover_iblt.errors import DigestFormatError  # noqa: E402
from handover_iblt.store import ChecklistStore  # noqa: E402


CFG = DigestConfig(capacity=8, k=3, seed=7)


def _blob(rows) -> bytes:
    store = ChecklistStore(None)
    try:
        store.ingest_lines(iter(rows))
        return build_digest_from_store(store, CFG).to_bytes()
    finally:
        store.close()


def _rows(n=3):
    return [
        f"{i:032x},{hashlib.sha256(str(i).encode()).hexdigest()}\n"
        for i in range(n)
    ]


def test_roundtrip_header_fields():
    blob = _blob(_rows(4))
    digest = parse_digest_bytes(blob)
    assert digest.config == CFG
    assert digest.unique_elements == 4
    assert digest.source_lines == 4
    assert digest.duplicate_lines == 0
    assert len(blob) == 96 + 64 * CFG.capacity
    assert digest.to_bytes() == blob


def test_single_flipped_body_byte_is_rejected():
    blob = bytearray(_blob(_rows(3)))
    blob[HEADER_BYTES + 10] ^= 0x01
    with pytest.raises(DigestFormatError, match="integrity"):
        parse_digest_bytes(bytes(blob))


def test_header_change_is_rejected():
    blob = bytearray(_blob(_rows(3)))
    blob[24:28] = struct.pack(">I", 16)
    with pytest.raises(DigestFormatError):
        parse_digest_bytes(bytes(blob))


def test_truncated_digest_is_rejected():
    blob = _blob(_rows(3))
    with pytest.raises(DigestFormatError, match="truncated"):
        parse_digest_bytes(blob[:-1])
    with pytest.raises(DigestFormatError, match="truncated"):
        parse_digest_bytes(blob[:40])
    with pytest.raises(DigestFormatError, match="truncated"):
        parse_digest_bytes(b"")


def test_trailing_bytes_are_rejected():
    blob = _blob(_rows(3)) + b"\x00"
    with pytest.raises(DigestFormatError, match="trailing"):
        parse_digest_bytes(blob)


def test_bad_magic_is_rejected():
    blob = bytearray(_blob(_rows(3)))
    blob[0:8] = b"NOTIBLT1"
    with pytest.raises(DigestFormatError, match="magic"):
        parse_digest_bytes(bytes(blob))


def test_unknown_protocol_is_rejected():
    blob = bytearray(_blob(_rows(3)))
    blob[8:24] = b"iblt-handover/9".ljust(16, b"\x00")
    with pytest.raises(DigestFormatError, match="protocol"):
        parse_digest_bytes(bytes(blob))


@pytest.mark.parametrize("capacity,k", [(0, 1), (8, 0), (8, 9), (1 << 21, 2)])
def test_impossible_configuration_in_header_is_rejected(capacity, k):
    blob = bytearray(_blob(_rows(3)))
    blob[24:28] = struct.pack(">I", capacity)
    blob[28:32] = struct.pack(">I", k)
    with pytest.raises(DigestFormatError):
        parse_digest_bytes(bytes(blob))


def test_int64_min_count_is_rejected_even_when_resealed():
    blob = bytearray(_blob(_rows(3)))
    blob[HEADER_BYTES:HEADER_BYTES + 8] = struct.pack(">q", -(1 << 63))
    # Re-seal so only the semantic check can catch it.
    blob[-32:] = hashlib.sha256(blob[:-32]).digest()
    with pytest.raises(DigestFormatError, match="INT64_MIN"):
        parse_digest_bytes(bytes(blob))


def test_implausible_count_is_rejected_even_when_resealed():
    blob = bytearray(_blob(_rows(3)))
    blob[HEADER_BYTES:HEADER_BYTES + 8] = struct.pack(">q", 10 ** 12)
    blob[-32:] = hashlib.sha256(blob[:-32]).digest()
    with pytest.raises(DigestFormatError, match="implausible"):
        parse_digest_bytes(bytes(blob))


def test_count_without_key_material_is_rejected():
    blob = bytearray(_blob(_rows(3)))
    offset = HEADER_BYTES + 64 * 2
    blob[offset:offset + 8] = struct.pack(">q", 1)
    blob[offset + 8:offset + 64] = b"\x00" * 56
    blob[-32:] = hashlib.sha256(blob[:-32]).digest()
    with pytest.raises(DigestFormatError, match="zero key"):
        parse_digest_bytes(bytes(blob))


def test_header_cannot_claim_a_different_element_count():
    blob = bytearray(_blob(_rows(3)))
    blob[40:48] = struct.pack(">Q", 9)
    blob[-32:] = hashlib.sha256(blob[:-32]).digest()
    with pytest.raises(DigestFormatError, match="cells encode"):
        parse_digest_bytes(bytes(blob))


def test_load_rejects_missing_file(tmp_path):
    with pytest.raises(DigestFormatError, match="cannot read"):
        load_digest(str(tmp_path / "nope.dig"))


def test_build_refuses_to_overwrite_digest(tmp_path):
    from handover_iblt.builder import build_from_checklist

    checklist = tmp_path / "in.txt"
    checklist.write_text(_rows(2)[0], encoding="utf-8")
    out = tmp_path / "out.dig"
    build_from_checklist(str(checklist), CFG, digest_path=str(out))
    original = out.read_bytes()
    with pytest.raises(FileExistsError):
        build_from_checklist(str(checklist), CFG, digest_path=str(out))
    assert out.read_bytes() == original


def test_failed_build_leaves_no_digest(tmp_path):
    from handover_iblt.builder import build_from_checklist

    checklist = tmp_path / "in.txt"
    checklist.write_text(
        _rows(1)[0] + "zz%062d\n" % 0, encoding="utf-8"
    )
    out = tmp_path / "out.dig"
    with pytest.raises(Exception):
        build_from_checklist(str(checklist), CFG, digest_path=str(out))
    assert not out.exists()


def test_failed_build_leaves_no_store(tmp_path):
    from handover_iblt.builder import build_from_checklist

    checklist = tmp_path / "in.txt"
    checklist.write_text("not-a-valid-line\n", encoding="utf-8")
    store = tmp_path / "s.db"
    with pytest.raises(Exception):
        build_from_checklist(str(checklist), CFG, store_path=str(store))
    assert not store.exists()
