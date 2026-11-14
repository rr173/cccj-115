"""Scoring, threshold, tie-break and selection tests against the reference."""

from __future__ import annotations

from fractions import Fraction

import pytest

import reference as ref

from logtempl import masks
from logtempl.config import ConfigError, parse_threshold
from logtempl.index import score as index_score
from logtempl.templates import LITERAL, MASK, WILDCARD, Item


def items_from_reference(items):
    return tuple(
        Item({"lit": "literal", "mask": "mask", "wild": "wildcard"}[kind], value)
        for kind, value in items
    )


# ---------------------------------------------------------------------------
# threshold parsing is exact
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("1", Fraction(1, 1)),
    ("1.0", Fraction(1, 1)),
    ("0.5", Fraction(1, 2)),
    (".5", Fraction(1, 2)),
    ("0.75", Fraction(3, 4)),
    ("0.750", Fraction(3, 4)),
    ("0.0001", Fraction(1, 10000)),
    ("0.1", Fraction(1, 10)),   # not the nearest binary float
])
def test_threshold_parses_exactly(text, expected):
    assert parse_threshold(text) == expected


@pytest.mark.parametrize("text", ["0", "0.0", "-0.5", "-1", "1.0001", "2", "nan", "inf", ""])
def test_threshold_rejects_out_of_range(text):
    with pytest.raises(ConfigError):
        parse_threshold(text)


def test_threshold_is_exact_not_float():
    # 0.1 + 0.2 style errors must not leak in: 3/10 >= 0.3 exactly.
    assert Fraction(3, 10) >= parse_threshold("0.3")
    assert not Fraction(3, 10) >= parse_threshold("0.31")
    # 0.7 as a float is 0.69999...; the decimal comparison must be exact.
    assert Fraction(7, 10) >= parse_threshold("0.7")


# ---------------------------------------------------------------------------
# scoring semantics
# ---------------------------------------------------------------------------
CASES = [
    # (message words, template items, expected points / wildcards / differ)
    (["a", "b", "c"], [("lit", "a"), ("lit", "b"), ("lit", "c")], (3, 0, 0)),
    (["a", "x", "c"], [("lit", "a"), ("lit", "b"), ("lit", "c")], (2, 0, 1)),
    (["a", "b", "10.0.0.1"], [("lit", "a"), ("lit", "b"), ("mask", "IPV4")], (3, 0, 0)),
    (["a", "b", "42"], [("lit", "a"), ("lit", "b"), ("mask", "IPV4")], (2, 0, 1)),
    (["a", "b", "42"], [("lit", "a"), ("lit", "b"), ("mask", "INT")], (3, 0, 0)),
    (["a", "b", "42"], [("lit", "a"), ("lit", "b"), ("wild", "")], (2, 1, 0)),
    (["a", "b", "42"], [("wild", ""), ("wild", ""), ("wild", "")], (0, 3, 0)),
    (["<*>", "b"], [("lit", "<*>"), ("lit", "b")], (2, 0, 0)),
    (["<*>", "b"], [("wild", ""), ("lit", "b")], (1, 1, 0)),
]


@pytest.mark.parametrize("words,template,expected", CASES)
def test_score_against_reference(words, template, expected):
    production = index_score(
        tuple(("literal", w) if masks.classify(w, masks.validate_enabled(None)) is None
              else ("mask", masks.classify(w, masks.validate_enabled(None)))
              for w in words),
        items_from_reference(template),
    )
    assert production == expected
    assert ref.score(words, template) == expected


def test_same_mask_type_scores_different_type_does_not():
    enabled = masks.validate_enabled(None)
    msg = tuple(
        ("mask", masks.classify(w, enabled)) for w in ["10.0.0.1", "10.0.0.2"]
    )
    assert index_score(msg, (Item(MASK, "IPV4"), Item(MASK, "IPV4"))) == (2, 0, 0)
    assert index_score(msg, (Item(MASK, "IPV4"), Item(MASK, "UUID"))) == (1, 0, 1)
    assert index_score(msg, (Item(MASK, "IPV4"), Item(LITERAL, "10.0.0.2"))) == (1, 0, 1)


def test_zero_word_score_is_one():
    # Aligned empty pairs: 0 points but score defined as 1 by the contract.
    assert ref.score_of([], ()) == Fraction(1, 1)


# ---------------------------------------------------------------------------
# tie-break rules
# ---------------------------------------------------------------------------
def test_tie_prefers_fewer_wildcards_then_lower_id():
    # Equal points; the candidate with fewer wildcards is preferred.
    a = (Item(LITERAL, "a"), Item(WILDCARD, ""), Item(WILDCARD, ""))
    b = (Item(LITERAL, "a"), Item(LITERAL, "b"), Item(WILDCARD, ""))
    msg = (("literal", "a"), ("literal", "b"), ("literal", "z"))
    pa = index_score(msg, a)   # 1 point, 2 wildcards
    pb = index_score(msg, b)   # 2 points  -- not a tie
    assert pa == (1, 2, 0)
    assert pb == (2, 1, 0)
    # Same points, different wildcard counts:
    c = (Item(LITERAL, "a"), Item(WILDCARD, ""))
    d = (Item(LITERAL, "a"), Item(MASK, "INT"))
    msg2 = (("literal", "a"), ("mask", "INT"))
    pc = index_score(msg2, c)  # 1 point, 1 wildcard
    pd = index_score(msg2, d)  # 2 points
    assert pc == (1, 1, 0) and pd == (2, 0, 0)
    # Genuine tie: same points, one has more wildcards.
    e = (Item(LITERAL, "a"), Item(WILDCARD, ""))
    f = (Item(MASK, "INT"), Item(WILDCARD, ""))
    g = (Item(MASK, "INT"), Item(LITERAL, "b"))
    msg3 = (("literal", "a"), ("literal", "b"))
    pe = index_score(msg3, e)
    pf = index_score(msg3, f)
    pg = index_score(msg3, g)
    assert pe[0] == pg[0] == 1
    assert pe[1] > pg[1]  # e has one wildcard, g has none
    # tie-break key: points, then fewer wildcards, then lower id
    assert (pg[0], -pg[1], -2) > (pe[0], -pe[1], -1)


def test_equal_wildcards_prefer_lower_id():
    a = (Item(LITERAL, "a"), Item(WILDCARD, ""))
    b = (Item(LITERAL, "a"), Item(WILDCARD, ""))
    msg = (("literal", "a"), ("literal", "z"))
    pa = index_score(msg, a)
    pb = index_score(msg, b)
    assert pa == pb
    # equal points and wildcards: lower id wins by the documented key
    assert (pa[0], -pa[1], -1) > (pb[0], -pb[1], -2)


def test_literal_wildcard_spelling_is_not_a_wildcard():
    lit = Item(LITERAL, "<*>")
    wild = Item(WILDCARD, "")
    assert lit.render() == "LIT(<*>)"
    assert wild.render() == "<*>"
    msg = (("literal", "x"),)
    assert index_score(msg, (lit,)) == (0, 0, 1)     # literal mismatch
    assert index_score(msg, (wild,)) == (0, 1, 0)    # wildcard absorbs
    msg2 = (("literal", "<*>"),)
    assert index_score(msg2, (lit,)) == (1, 0, 0)    # exact literal match
    assert ref.score(["<*>"], [("lit", "<*>")]) == (1, 0, 0)
    assert ref.render([("lit", "<*>")]) == "LIT(<*>)"


# ---------------------------------------------------------------------------
# full naive-learning differential
# ---------------------------------------------------------------------------
DATASETS = {
    "gradual-generalisation": [
        "job 17 started on node a1",
        "job 18 started on node a1",
        "job 19 started on node b2",
        "job 20 finished on node b2",
        "job 21 started on node c3",
    ],
    "tie-competition": [
        "alpha beta gamma delta",
        "alpha x gamma y",
        "zulu beta gamma omega",
        "kilo beta gamma zulu",
        "alpha beta gamma delta",
    ],
    "mask-clash": [
        "connect 10.0.0.1 port 80",
        "connect 10.0.0.2 port 443",
        "connect 550e8400-e29b-41d4-a716-446655440000 port 8080",
        "connect 7 port 9",
        "connect 10.0.0.3 port 999",
    ],
    "literal-wildcard-token": [
        "pattern <*> here",
        "pattern <*> there",
        "pattern x there",
    ],
    "whitespace-and-empty": [
        "",
        "   ",
        "\t\n",
        "  a\tb  ",
        "  a\tb  ",
        "a  b",
        "a b",
    ],
    "bad-ip-and-uuid": [
        "peer 256.0.0.1 id 550e8400-e29b-41d4-a716-446655440000",
        "peer 999.1.1.1 id 550e8400-e29b-41d4-a716-446655440001",
        "peer 256.0.0.1 id 550e8400-e29b-41d4-a716-446655440002",
    ],
}


@pytest.mark.parametrize("name", sorted(DATASETS))
@pytest.mark.parametrize("threshold", ["0.5", "0.67", "1"])
def test_learning_matches_naive_reference(name, threshold):
    from conftest import ingest
    from logtempl.library import Library

    messages = DATASETS[name]
    tpls, revs, members, final_rev = ref.learn(
        messages, Fraction(threshold)
    )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/lib.db"
        with Library.create(path, threshold=threshold) as lib:
            ingest(lib, messages)
            # template ids and final structures
            assert lib.template_ids() == sorted(tpls)
            for tid in sorted(tpls):
                head = lib.template_head(tid)
                assert head["render"] == ref.render(tpls[tid])
                assert head["word_count"] == len(tpls[tid])
                assert head["revision"] == max(
                    rev for (t, rev) in revs if t == tid
                )
                got_revs = {
                    r.revision: r.render for r in lib.template_revisions(tid)
                }
                want_revs = {
                    rev: ref.render(items)
                    for (t, rev), items in revs.items() if t == tid
                }
                assert got_revs == want_revs
            for pos, message in enumerate(messages):
                row = lib.get_message("s", pos)
                assert (row.template_id, row.template_rev) == members[pos]
                assert lib.reconstruct_message("s", pos) == message
            lib.verify_integrity()
