"""Ingestion protocol tests: replay, conflict, gaps, empty messages, atomicity."""

from __future__ import annotations

import pytest

from conftest import ingest
from logtempl.library import ConflictError, GapError, Library, Record


def test_empty_message_is_recorded(make_library):
    lib = make_library()
    ingest(lib, ["", "   ", "\t\n"])
    assert lib.message_count() == 3
    for seq, text in enumerate(["", "   ", "\t\n"]):
        row = lib.get_message("s", seq)
        assert row is not None
        assert row.word_count == 0
        assert lib.reconstruct_message("s", seq) == text
    # All three share the single zero-word template.
    assert lib.template_ids() == [1]
    assert lib.template_head(1)["member_count"] == 3


def test_whitespace_is_preserved_char_for_char(make_library):
    lib = make_library()
    messages = [
        "  alpha \t beta  ",
        "\talpha\t\tbeta\t",
        "alpha  beta",
        "alpha\tbeta",
        " alpha beta",
    ]
    ingest(lib, messages)
    for seq, text in enumerate(messages):
        assert lib.reconstruct_message("s", seq) == text


def test_replay_of_identical_message_is_ignored(make_library):
    lib = make_library()
    first = lib.ingest_batch([Record("s", 0, "alpha beta 10.0.0.1")])
    assert first.accepted == [("s", 0)]
    second = lib.ingest_batch([Record("s", 0, "alpha beta 10.0.0.1")])
    assert second.accepted == []
    assert second.ignored_replays == [("s", 0)]
    assert second.tx_id == 0
    assert lib.message_count() == 1
    assert lib.next_seq("s") == 1
    # No new revision or template was created by the replay.
    assert lib.template_head(1)["revision"] == 1
    assert lib.template_head(1)["member_count"] == 1
    assert lib.config_report()["revision_counter"] == 1


def test_replay_with_different_text_is_a_conflict(make_library):
    lib = make_library()
    lib.ingest_batch([Record("s", 0, "alpha beta")])
    with pytest.raises(ConflictError):
        lib.ingest_batch([Record("s", 0, "alpha gamma")])
    assert lib.message_count() == 1
    assert lib.next_seq("s") == 1


def test_replay_conflict_refuses_whole_batch(make_library):
    lib = make_library()
    lib.ingest_batch([Record("s", 0, "a b")])
    with pytest.raises(ConflictError):
        lib.ingest_batch([
            Record("s", 1, "c d"),        # would be new
            Record("s", 0, "changed"),    # conflict
        ])
    # The new record in the refused batch was not accepted either.
    assert lib.next_seq("s") == 1
    assert lib.message_count() == 1
    assert lib.get_message("s", 1) is None


def test_seq_gap_refuses_the_batch(make_library):
    lib = make_library()
    lib.ingest_batch([Record("s", 0, "a b")])
    with pytest.raises(GapError):
        lib.ingest_batch([Record("s", 2, "c d")])
    assert lib.next_seq("s") == 1
    assert lib.message_count() == 1
    assert lib.config_report()["revision_counter"] == 1
    assert lib.verify_integrity() is None


def test_gap_mid_batch_refuses_earlier_records_too(make_library):
    lib = make_library()
    with pytest.raises(GapError):
        lib.ingest_batch([
            Record("s", 0, "a b"),
            Record("s", 2, "c d"),   # gap at seq 1
        ])
    assert lib.next_seq("s") == 0
    assert lib.message_count() == 0
    assert lib.template_ids() == []


def test_multi_source_positions_are_independent(make_library):
    lib = make_library()
    lib.ingest_batch([
        Record("a", 0, "x y"),
        Record("b", 0, "x y"),
        Record("a", 1, "x z"),
        Record("b", 1, "x w"),
    ])
    assert lib.next_seq("a") == 2
    assert lib.next_seq("b") == 2
    for source in ("a", "b"):
        for seq in range(2):
            assert lib.reconstruct_message(source, seq) == lib.get_message(
                source, seq
            ).message


def test_replay_inside_one_batch_is_ignored(make_library):
    lib = make_library()
    outcome = lib.ingest_batch([
        Record("s", 0, "a b"),
        Record("s", 0, "a b"),
    ])
    assert outcome.accepted == [("s", 0)]
    assert outcome.ignored_replays == [("s", 0)]
    assert lib.message_count() == 1


def test_replay_inside_one_batch_with_conflict(make_library):
    lib = make_library()
    with pytest.raises(ConflictError):
        lib.ingest_batch([
            Record("s", 0, "a b"),
            Record("s", 0, "a c"),
        ])
    assert lib.message_count() == 0


def test_failed_batch_leaves_no_partial_state(make_library):
    lib = make_library()
    lib.ingest_batch([Record("s", 0, "job 1 started")])
    before = lib.config_report()
    with pytest.raises(GapError):
        lib.ingest_batch([Record("s", 5, "job 2 started")])
    assert lib.config_report() == before
    assert lib.template_head(1)["member_count"] == 1


def test_conflict_after_gap_commit_flow(make_library):
    lib = make_library()
    lib.ingest_batch([Record("s", 0, "a b"), Record("s", 1, "c d")])
    # Replaying the whole source is idempotent.
    outcome = lib.ingest_batch([
        Record("s", 0, "a b"),
        Record("s", 1, "c d"),
    ])
    assert outcome.accepted == []
    assert outcome.ignored_replays == [("s", 0), ("s", 1)]
    assert lib.message_count() == 2
