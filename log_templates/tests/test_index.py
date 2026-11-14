"""Index correctness: coverage, pruning, relinking and visit counts."""

from __future__ import annotations

import tempfile
from fractions import Fraction

import pytest

import reference as ref

from conftest import ingest
from logtempl.index import Candidate, IndexWriter, load_items, lookup
from logtempl.library import Library, Record


def _message_items(words):
    from logtempl import masks

    enabled = masks.validate_enabled(None)
    out = []
    for word in words:
        kind = masks.classify(word, enabled)
        out.append(("literal", word) if kind is None else ("mask", kind))
    return tuple(out)


def _all_templates(lib):
    return {
        tid: load_items(lib.conn, tid) for tid in lib.template_ids()
    }


def test_index_finds_templates_on_nonliteral_branches(make_library):
    """The matching position is behind a different literal branch.

    Template 1 'aa bb' is followed by a message 'aa cc' that scores 1/2.
    The index must expand the 'cc' branch even though it is a mismatch,
    because the template behind it still reaches the threshold.
    """
    lib = make_library(threshold="0.5", depth=1)
    ingest(lib, ["aa bb"])
    from logtempl.index import lookup

    report = lookup(
        lib.conn, 1, 2, _message_items(["aa", "cc"]), Fraction(1, 2),
        lib.template_cache, lib.node_cache,
    )
    assert [c.template_id for c in report.candidates] == [1]
    assert report.candidates[0].points == 1


def test_typed_mask_branch_is_covered(make_library):
    lib = make_library(threshold="0.5", depth=1)
    ingest(lib, ["host 10.0.0.1 ok"])
    from logtempl.index import lookup

    # Positions 1 and 2 match (mask + literal), position 0 differs: 2/3 is
    # above the 1/2 threshold, so the literal branch "host" must be explored
    # even though the message's first word is "other".
    report = lookup(
        lib.conn, 1, 3,
        _message_items(["other", "10.0.0.9", "ok"]),
        Fraction(1, 2), lib.template_cache, lib.node_cache,
    )
    assert [c.template_id for c in report.candidates] == [1]
    assert report.candidates[0].points == 2
    # A mask of a different type at position 1 leaves only 1/3: not admissible.
    report2 = lookup(
        lib.conn, 1, 3,
        _message_items(["other", "7", "ok"]),
        Fraction(1, 2), lib.template_cache, lib.node_cache,
    )
    assert report2.candidates == []


def test_generalisation_relinks_and_new_message_still_matches(make_library):
    """Generalising a depth-prefix position must keep the template reachable.

    depth=1: template 'alpha beta gamma delta'.  Message
    'omega beta gamma delta' scores 3/4 and joins it, turning position 0 into
    a wildcard.  The index path changes from lit:alpha to wild.  A later
    message whose first word differs from 'alpha' at all must still find the
    template -- this only works if the leaf was re-linked.
    """
    lib = make_library(threshold="0.5", depth=1)
    ingest(lib, ["alpha beta gamma delta"])
    ingest(lib, ["omega beta gamma delta"], start=1)
    assert lib.template_head(1)["render"] == "<*> beta gamma delta"
    # New message whose first word is neither 'alpha' nor 'omega'.
    outcome = ingest(lib, ["zeta beta gamma delta"], start=2)
    assert outcome.templates_created == []
    assert lib.get_message("s", 2).template_id == 1
    assert lib.get_message("s", 2).template_rev == lib.template_head(1)["revision"]
    assert lib.reconstruct_message("s", 2) == "zeta beta gamma delta"


def test_depth_two_prefix_generalisation_relinks_index(make_library):
    """Generalising the second depth position re-links the depth-2 path."""
    lib = make_library(threshold="0.5", depth=2)
    ingest(lib, ["alpha beta gamma delta"])
    ingest(lib, ["alpha omega gamma delta", ], start=1)  # 3/4 -> joins
    assert lib.template_head(1)["render"] == "alpha <*> gamma delta"
    # Path is now lit:alpha -> wild.  'alpha zeta gamma delta' must be found.
    outcome = ingest(lib, ["alpha zeta gamma delta"], start=2)
    assert outcome.templates_created == []
    assert lib.get_message("s", 2).template_id == 1
    lib.verify_integrity()
    # The leaf sits at level 2 (the depth), reached through the relinked path.
    leaves = lib.conn.execute(
        "SELECT n.level FROM index_leaves l JOIN index_nodes n "
        "ON n.node_id = l.node_id WHERE l.template_id = 1"
    ).fetchall()
    assert leaves == [(2,)]
    # No leaf may remain attached to the old literal 'beta' path.
    stale = lib.conn.execute(
        "SELECT COUNT(*) FROM index_leaves l JOIN index_nodes n "
        "ON n.node_id = l.node_id WHERE l.template_id = 1 "
        "AND n.slot_kind = 'lit' AND n.slot_value = 'beta'"
    ).fetchone()[0]
    assert stale == 0


def test_zero_word_template_index(make_library):
    lib = make_library(threshold="0.5", depth=2)
    ingest(lib, ["", "   ", "\t"])
    from logtempl.index import lookup

    report = lookup(
        lib.conn, 2, 0, (), Fraction(1, 2),
        lib.template_cache, lib.node_cache,
    )
    assert [c.template_id for c in report.candidates] == [1]
    assert report.candidates[0].score == 1


def test_index_lookup_does_not_scan_every_template(make_library):
    """Many templates, one message: examined candidates stay bounded."""
    lib = make_library(threshold="1", depth=2)
    # 60 distinct templates of the same word count, no two ever matching.
    messages = [f"alpha{n:03d} beta gamma item{n:03d}" for n in range(60)]
    ingest(lib, messages)
    assert len(lib.template_ids()) == 60
    for probe in ("alpha000 beta gamma item000", "alpha059 beta gamma item059"):
        words, _gaps = ref.split(probe)
        report = lookup(
            lib.conn, 2, 4, _message_items(words), Fraction(1, 1),
            lib.template_cache, lib.node_cache,
        )
        # The shared depth-2 prefix ('alphaNNN','beta') is a distinct branch
        # per template, so the walk touches only this template's chain.
        assert [c.template_id for c in report.candidates] == [
            int(probe[5:8]) + 1
        ]
        assert report.candidates_examined == 1
        assert report.nodes_visited <= 4


def test_lookup_visit_counts_are_reported(make_library):
    lib = make_library(threshold="0.5", depth=2)
    ingest(lib, ["aa bb cc", "aa bb dd", "zz bb cc"])
    report = lookup(
        lib.conn, 2, 3, _message_items(["aa", "bb", "dd"]),
        Fraction(1, 2), lib.template_cache, lib.node_cache,
    )
    assert report.nodes_visited >= 1
    assert report.candidates_examined >= 1
    assert report.branches_expanded >= 0
    # the message belongs to template 1
    assert [c.template_id for c in report.candidates] == [1]


def test_index_rebuild_reproduces_lookup(make_library):
    lib = make_library(threshold="0.5", depth=2)
    ingest(lib, ["a b c d", "a b c e", "a x c y", "p b c q"])
    message = _message_items(["a", "b", "c", "e"])
    before = lookup(lib.conn, 2, 4, message, Fraction(1, 2),
                    lib.template_cache, lib.node_cache)
    lib.rebuild_index()
    after = lookup(lib.conn, 2, 4, message, Fraction(1, 2),
                   lib.template_cache, lib.node_cache)
    assert [(c.template_id, c.points, c.wildcards) for c in before.candidates] == \
           [(c.template_id, c.points, c.wildcards) for c in after.candidates]
    lib.verify_integrity()


def test_index_matches_naive_full_scan(make_library):
    """For every dataset message, index candidates == naive admissible set."""
    lib = make_library(threshold="0.67", depth=2)
    messages = [
        "job 17 started on node a1",
        "job 18 started on node a1",
        "job 19 started on node b2",
        "job 20 finished on node b2",
        "job 21 started on node c3",
    ]
    ingest(lib, messages)
    templates = _all_templates(lib)
    for message in messages:
        words, _ = ref.split(message)
        w = len(words)
        naive = []
        for tid, items in templates.items():
            if len(items) != w:
                continue
            points, wild, _ = ref.score(words, items)
            frac = Fraction(points, w)
            if frac >= Fraction("0.67"):
                naive.append((tid, points, wild))
        naive.sort(key=lambda t: (-t[1], t[2], t[0]))
        report = lookup(
            lib.conn, 2, w, _message_items(words), Fraction("0.67"),
            lib.template_cache, lib.node_cache,
        )
        got = [(c.template_id, c.points, c.wildcards)
               for c in report.candidates]
        assert got == naive[: len(naive)]
