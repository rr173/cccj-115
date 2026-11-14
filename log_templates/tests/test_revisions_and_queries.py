"""Revisions, message references, reconstruction and query surface tests."""

from __future__ import annotations

from fractions import Fraction

import pytest

import reference as ref

from conftest import ingest
from logtempl.library import Library, Record, UnknownTemplate


def test_each_generalisation_creates_a_new_revision(make_library):
    lib = make_library(threshold="0.5")
    ingest(lib, [
        "job 17 started",
        "job 18 started",
        "job 19 finished",
        "task 20 started",
    ])
    revisions = lib.template_revisions(1)
    assert [r.revision for r in revisions] == [1, 2]
    assert [r.render for r in revisions] == [
        "job <INT> started",
        "job <INT> <*>",
    ]
    # 'task 20 started' scores 1/3 < 0.5, so it is a new template.
    assert lib.get_message("s", 3).template_id == 2
    # Every message references the exact revision current when it arrived.
    assert lib.get_message("s", 0).template_rev == 1
    assert lib.get_message("s", 1).template_rev == 1
    assert lib.get_message("s", 2).template_rev == 2


def test_head_revision_after_second_generalisation(make_library):
    lib = make_library(threshold="0.5")
    ingest(lib, ["aa bb cc dd", "aa bb cc ee", "aa bb ee dd"])
    revisions = lib.template_revisions(1)
    assert [r.revision for r in revisions] == [1, 2, 3]
    assert [r.render for r in revisions] == [
        "aa bb cc dd",
        "aa bb cc <*>",
        "aa bb <*> <*>",
    ]


def test_early_messages_keep_their_interpretation_after_generalisation(make_library):
    lib = make_library(threshold="0.5")
    ingest(lib, ["aa bb cc", "aa bb dd", "aa zz ee"])
    # By now template 1 is '<*> <*> <*>'; the first message referenced rev 1
    # with fully literal items.
    first = lib.get_message("s", 0)
    rev1 = lib.template_revision(1, first.template_rev)
    assert [it.kind for it in rev1.items] == ["literal"] * 3
    assert [it.value for it in rev1.items] == ["aa", "bb", "cc"]
    # The revision is immutable: re-reading gives the same snapshot.
    again = lib.template_revision(1, first.template_rev)
    assert again == rev1


def test_reconstruction_is_exact_before_and_after_generalisation(make_library):
    lib = make_library(threshold="0.5")
    before = ["  aa\tbb  10.0.0.1 ", "aa bb 10.0.0.2", "\taa bb 10.0.0.3\t"]
    ingest(lib, before)
    for seq, text in enumerate(before):
        assert lib.reconstruct_message("s", seq) == text
    after = ["zz bb 10.0.0.9", "aa qq 10.0.0.10", "aa bb 42"]
    ingest(lib, after, start=3)
    for seq, text in enumerate(before + after):
        assert lib.reconstruct_message("s", seq) == text, seq
    for seq, text in enumerate(before + after):
        assert lib.get_message("s", seq).message == text


def test_parameters_are_extracted_by_position(make_library):
    lib = make_library(threshold="0.5")
    ingest(lib, ["from 10.0.0.1 port 42"])
    params = lib.message_parameters("s", 0)
    assert params == [
        (0, "literal", "from", "from"),
        (1, "mask", "IPV4", "10.0.0.1"),
        (2, "literal", "port", "port"),
        (3, "mask", "INT", "42"),
    ]
    assert lib.reconstruct_message("s", 0) == "from 10.0.0.1 port 42"


def test_members_listing_and_counts(make_library):
    lib = make_library(threshold="0.5")
    ingest(lib, ["aa bb cc", "aa bb dd", "zz bb cc"])
    members = lib.template_members(1)
    assert [(m.seq, m.message) for m in members] == [
        (0, "aa bb cc"),
        (1, "aa bb dd"),
    ]
    head = lib.template_head(1)
    assert head["member_count"] == 2
    assert head["total_count"] == 2
    # Revision-scoped member view never mixes later interpretations.
    rev_members = lib.template_members(1, revision=members[0].template_rev)
    assert {m.seq for m in rev_members} == {0}


def test_unknown_template_is_reported(make_library):
    lib = make_library()
    ingest(lib, ["a b"])
    with pytest.raises(UnknownTemplate):
        lib.template_head(99)
    with pytest.raises(UnknownTemplate):
        lib.template_revision(1, 99)
    with pytest.raises(UnknownTemplate):
        lib.template_revisions(99)


def test_revision_numbers_are_global_and_monotone(make_library):
    lib = make_library(threshold="1")
    ingest(lib, ["a b c", "d e f", "a b c"])
    revs = sorted(
        r.revision
        for tid in lib.template_ids()
        for r in lib.template_revisions(tid)
    )
    assert revs == [1, 2]
    assert lib.config_report()["revision_counter"] == 2


def test_verify_integrity_after_mixed_workload(make_library):
    lib = make_library(threshold="0.67")
    messages = [
        "job 17 started on node a1",
        "job 18 started on node a1",
        "",
        "   ",
        "job 19 finished on node b2",
        "pattern <*> here",
        "pattern x there",
        "peer 256.0.0.1 id 550e8400-e29b-41d4-a716-446655440000",
        "peer 10.0.0.1 id 550e8400-e29b-41d4-a716-446655440001",
    ]
    ingest(lib, messages)
    lib.verify_integrity()
    for seq, message in enumerate(messages):
        assert lib.reconstruct_message("s", seq) == message
