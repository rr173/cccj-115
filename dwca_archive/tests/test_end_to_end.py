"""End-to-end expectations for the headline synthetic archive.

Expected identities, attributions and reasons below are written out
independently of the production code; the tests check them one by one.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dwca import DwcaLibrary, import_archive  # noqa: E402
from dwca.models import (  # noqa: E402
    ORIGIN_DEFAULT,
    ORIGIN_EMPTY,
    ORIGIN_SOURCE,
    REASON_AMBIGUOUS_REF,
    REASON_BAD_CORE_REF,
    REASON_DUPLICATE_KEY,
    REASON_EMPTY_REF,
    REASON_MISSING_REF,
    REASON_SHORT_ROW,
)

from . import materials as M

MEAS_ID = "ext:" + M.MEAS
MEDIA_ID = "ext:" + M.MEDIA


@pytest.fixture()
def imported(tmp_path):
    archive = tmp_path / "feature.zip"
    db = tmp_path / "feature.db"
    M.build_feature_archive(str(archive))
    result = import_archive(str(archive), str(db))
    return archive, db, result


def test_per_table_counts_reconcile(imported):
    _, _, result = imported
    by_id = {t.table_id: t for t in result.tables}
    assert by_id["core"].input_count == 7
    assert by_id["core"].valid_count == 3
    assert by_id["core"].quarantined_count == 4
    assert by_id[MEAS_ID].input_count == 8
    # valid: three C1 rows (rows 1, 2 and the exact-content duplicate row 8)
    # plus the single C3 row = 4
    assert by_id[MEAS_ID].valid_count == 4
    # quarantined: bad-core, ambiguous, missing, empty-ref = 4
    assert by_id[MEAS_ID].quarantined_count == 4
    assert by_id[MEDIA_ID].input_count == 4
    assert by_id[MEDIA_ID].valid_count == 4
    assert by_id[MEDIA_ID].quarantined_count == 0
    for t in result.tables:
        assert t.input_count == t.valid_count + t.quarantined_count


def test_core_identity_and_shared_occurrence_id(imported):
    _, db, _ = imported
    with DwcaLibrary.open(str(db)) as lib:
        cores = list(lib.iter_core())
        assert [c.core_id for c in cores] == ["C1", "C2", "C3"]
        # Two distinct core rows share one occurrenceID.
        c1, c2 = cores[0], cores[1]
        assert c1.values[M.OCC_TERM] == "OCC-SHARED"
        assert c2.values[M.OCC_TERM] == "OCC-SHARED"
        assert c1.record_number == 1 and c2.record_number == 2
        # Exact decoded-string matching: whitespace/case must not fold.
        assert lib.get_core("c1") is None
        assert lib.get_core(" C1") is None
        assert lib.get_core("C1").core_id == "C1"


def test_extensions_grouped_not_cartesian(imported):
    _, db, _ = imported
    with DwcaLibrary.open(str(db)) as lib:
        c1 = lib.get_core("C1")
        # Empty groups are explicit for every extension table.
        assert set(c1.extensions) == {MEAS_ID, MEDIA_ID}
        measurements = c1.extensions[MEAS_ID]
        media = c1.extensions[MEDIA_ID]
        # Three measurement rows (including the exact-content duplicate,
        # which is never deduplicated) and three media rows stay separate:
        # no 3x3=9 Cartesian-product join.
        assert len(measurements) == 3
        assert [m.values[M.MEAS_TERM] for m in measurements] == [
            "weight", "length", "weight",
        ]
        assert [m.record_number for m in measurements] == [1, 2, 8]
        assert len(media) == 3
        assert [m.values[M.MEDIA_TERM] for m in media] == [
            "m1.jpg", "m2.jpg", "m3.jpg",
        ]
        # C3 has measurements but an explicit empty media group.
        c3 = lib.get_core("C3")
        assert len(c3.extensions[MEAS_ID]) == 1
        assert c3.extensions[MEDIA_ID] == []
        # Duplicate content rows are never deduplicated (checked on C1 + the
        # extra duplicate measurement quarantines only on reference failure).
        c2 = lib.get_core("C2")
        assert len(c2.extensions[MEDIA_ID]) == 1
        assert c2.extensions[MEAS_ID] == []


def test_duplicate_content_rows_kept_independently(imported, tmp_path):
    # The archive also carries two identical measurement rows for C1; build a
    # focused archive where both references are valid to prove no dedup.
    archive = tmp_path / "dup.zip"
    db = tmp_path / "dup.db"
    core = M.csv_bytes([["id", "x"], ["K1", "a"]])
    meas = M.csv_bytes([
        ["coreid", M.MEAS_TERM, M.MEAS_VAL],
        ["K1", "weight", "1"],
        ["K1", "weight", "1"],
    ])
    meta = M.META.format(
        core=M.core_xml("core.csv", [M.field_xml(M.OCC_TERM, 1)], 0),
        extensions=M.ext_xml(
            M.MEAS, "meas.csv",
            [M.field_xml(M.MEAS_TERM, 1), M.field_xml(M.MEAS_VAL, 2)], 0),
    )
    M.write_zip(str(archive), [("core.csv", core), ("meas.csv", meas)], meta)
    import_archive(str(archive), str(db))
    with DwcaLibrary.open(str(db)) as lib:
        rec = lib.get_core("K1")
        assert len(rec.extensions["ext:" + M.MEAS]) == 2
        nums = [
            r.record_number
            for r in rec.extensions["ext:" + M.MEAS]
        ]
        assert nums == [1, 2]


def test_quarantine_reasons_are_distinct_and_locatable(imported):
    _, db, _ = imported
    with DwcaLibrary.open(str(db)) as lib:
        core_q = {q.record_number: q for q in lib.iter_quarantine("core")}
        # record numbers are the original logical numbers: 4 (short),
        # 5 & 6 (duplicate pair), 7 (empty key).
        assert core_q[4].reasons == [REASON_SHORT_ROW]
        assert core_q[4].raw_cells == ["C4", "OCC-4"]
        assert core_q[5].reasons == [REASON_DUPLICATE_KEY]
        assert core_q[6].reasons == [REASON_DUPLICATE_KEY]
        # Duplicate rows keep their own original raw cells -- the later row
        # is not silently merged into or replaced by the earlier one.
        assert core_q[5].raw_cells == [
            "C5", "OCC-5", "cat-5", "epsilon", "eps-b", "captive",
        ]
        assert core_q[6].raw_cells == [
            "C5", "OCC-5-DUP", "cat-5b", "epsilon2", "eps2-b", "captive",
        ]
        # Term-level values and origins survive in quarantine too.
        assert core_q[5].values[M.OCC_TERM] == "OCC-5"
        assert core_q[6].values[M.OCC_TERM] == "OCC-5-DUP"
        assert core_q[5].origins[M.OCC_TERM] == ORIGIN_SOURCE
        assert core_q[7].reasons == ["empty_key"]
        assert core_q[7].key_value is None
        assert core_q[7].raw_cells == [
            "", "OCC-6", "cat-6", "zeta", "zeta-b", "wild",
        ]

        ext_q = list(lib.iter_quarantine(MEAS_ID))
        by_rec = {q.record_number: q for q in ext_q}
        # source order: 1 C1, 2 C1, 3 C3, 4 C4(bad core), 5 C5(ambiguous),
        # 6 GHOST(missing), 7 empty, 8 duplicate-of-1 (valid ref C1)
        assert by_rec[4].reasons == [REASON_BAD_CORE_REF]
        assert by_rec[4].key_value == "C4"
        assert by_rec[5].reasons == [REASON_AMBIGUOUS_REF]
        assert by_rec[5].key_value == "C5"
        assert by_rec[6].reasons == [REASON_MISSING_REF]
        assert by_rec[6].key_value == "GHOST"
        assert by_rec[7].reasons == [REASON_EMPTY_REF]
        assert by_rec[7].key_value is None
        # Every quarantined row keeps member + original values.
        for q in ext_q:
            assert q.member == "data/measurements.csv"
            assert q.raw_cells  # non-empty evidence retained
        # The orphan's exact raw text and parsed terms are both retained.
        assert by_rec[6].raw_cells == ["GHOST", "weight", "1", "orphan"]
        assert by_rec[6].values[M.MEAS_TERM] == "weight"
        assert by_rec[6].origins[M.MEAS_TERM] == ORIGIN_SOURCE
        # The default-only field is distinguishable even in quarantine.
        assert by_rec[6].values[M.DEFAULT_TERM] == "assumed"
        assert by_rec[6].origins[M.DEFAULT_TERM] == ORIGIN_DEFAULT


def test_short_core_participates_in_duplicate_detection(tmp_path):
    archive = tmp_path / "s.zip"
    db = tmp_path / "s.db"
    # First occurrence of K is itself a short row; second occurrence makes
    # the key ambiguous. Neither core row may receive an extension.
    core = M.csv_bytes([
        ["id", "occurrenceID", "catalogNumber"],
        ["K", "O1"],            # short: catalogNumber index 2 out of range
        ["K", "O2", "cat-2"],
    ])
    meas = M.csv_bytes([
        ["coreid", M.MEAS_TERM],
        ["K", "weight"],
    ])
    meta = M.META.format(
        core=M.core_xml(
            "core.csv",
            [M.field_xml(M.OCC_TERM, 1), M.field_xml(M.CAT_TERM, 2)], 0),
        extensions=M.ext_xml(
            M.MEAS, "meas.csv", [M.field_xml(M.MEAS_TERM, 1)], 0),
    )
    M.write_zip(str(archive), [("core.csv", core), ("meas.csv", meas)], meta)
    import_archive(str(archive), str(db))
    with DwcaLibrary.open(str(db)) as lib:
        assert list(lib.iter_core()) == []
        q_core = {q.record_number: q for q in lib.iter_quarantine("core")}
        assert set(q_core[1].reasons) == {
            REASON_SHORT_ROW, REASON_DUPLICATE_KEY
        }
        assert q_core[2].reasons == [REASON_DUPLICATE_KEY]
        q_ext = list(lib.iter_quarantine("ext:" + M.MEAS))
        assert q_ext[0].reasons == [REASON_AMBIGUOUS_REF]


def test_value_origins_default_vs_source(imported):
    _, db, _ = imported
    with DwcaLibrary.open(str(db)) as lib:
        c1 = lib.get_core("C1")
        # Empty source cell with declared default -> default origin.
        assert c1.values[M.DEFAULT_TERM] == "wild"
        assert c1.origins[M.DEFAULT_TERM] == ORIGIN_DEFAULT
        assert c1.origins[M.OCC_TERM] == ORIGIN_SOURCE
        c2 = lib.get_core("C2")
        assert c2.origins[M.DEFAULT_TERM] == ORIGIN_SOURCE
        # Extension: no-index default-only field is always default origin.
        m = c1.extensions[MEAS_ID][0]
        assert m.values[M.DEFAULT_TERM] == "assumed"
        assert m.origins[M.DEFAULT_TERM] == ORIGIN_DEFAULT


def test_same_local_name_uris_stay_distinct(imported):
    _, db, _ = imported
    with DwcaLibrary.open(str(db)) as lib:
        c1 = lib.get_core("C1")
        assert M.NOTE_A in c1.values and M.NOTE_B in c1.values
        assert c1.values[M.NOTE_A] == "alpha note"
        # noteB has no declared default: an empty source cell stays an empty
        # string with explicit "empty" origin rather than borrowing another
        # term's default.
        assert c1.values[M.NOTE_B] == ""
        assert c1.origins[M.NOTE_B] == ORIGIN_EMPTY


def test_unmapped_term_gets_no_column(imported):
    _, db, _ = imported
    with DwcaLibrary.open(str(db)) as lib:
        mapping = {f["term"] for f in lib.field_maps("core")}
        assert M.UNMAPPED_TERM not in mapping
        for c in lib.iter_core():
            assert M.UNMAPPED_TERM not in c.values


def test_reads_after_zip_removed(imported):
    archive, db, _ = imported
    os.remove(str(archive))
    assert not os.path.exists(str(archive))
    with DwcaLibrary.open(str(db)) as lib:
        ids = [c.core_id for c in lib.iter_core()]
        assert ids == ["C1", "C2", "C3"]
        quarantined = list(lib.iter_quarantine())
        assert len(quarantined) == 8  # 4 core + 4 extension
        # Descriptor itself is preserved.
        assert b"<archive" in lib.descriptor_xml()


def test_refuse_overwrite(imported):
    archive, db, _ = imported
    with pytest.raises(FileExistsError):
        import_archive(str(archive), str(db))
