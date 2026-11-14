"""Reproducible demonstration of the pipeline's contract points.

Run ``python3 materials/demo.py``.  It prints a narrated transcript and
asserts each claim as it goes, exiting non-zero on the first disagreement.

Sections
    1. Create a library and feed a stream that gradually generalises one
       fixed position; verify every message rebuilds character-for-char
       before and after the generalisation.
    2. Generalise a position inside the first ``depth`` words and show a
       later message still finds the template after the index re-link.
    3. Show a tie, a mask-type clash, illegal IPs, a literal ``<*>``,
       whitespace and empty messages.
    4. Perfectly match again: no revision bump.
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from logtempl.library import ConflictError, GapError, Library, Record  # noqa: E402
from logtempl.index import lookup  # noqa: E402


def rule(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def ingest(lib, messages, start=0, source="demo"):
    return lib.ingest_batch(
        [Record(source, start + i, m) for i, m in enumerate(messages)]
    )


def show_templates(lib):
    for tid in lib.template_ids():
        head = lib.template_head(tid)
        print(
            f"  T{tid} word_count={head['word_count']} "
            f"rev={head['revision']} wildcards={head['wildcards']} "
            f"members={head['member_count']} :: {head['render']}"
        )


def show_revisions(lib, tid):
    for rev in lib.template_revisions(tid):
        print(f"    rev {rev.revision} [{rev.origin}] {rev.render}")


def section_gradual(tmp):
    rule("1. Gradual generalisation of a fixed position, exact reconstruction")
    path = os.path.join(tmp, "gradual.db")
    lib = Library.create(path, threshold="0.5", depth=2)
    messages = [
        "job 17 started on node a1",
        "job 18 started on node b2",
        "job 19 finished on node b2",
        "job 20 started on node c3",
    ]
    expected_renders = [
        "job <INT> started on node a1",
        "job <INT> started on node <*>",
        "job <INT> <*> on node <*>",
        "job <INT> <*> on node <*>",
    ]
    expected_revs = [1, 2, 3, 3]
    for seq, message in enumerate(messages):
        before = lib.template_head(1)["render"] if 1 in lib.template_ids() else None
        outcome = ingest(lib, [message], start=seq, source="gradual")
        row = lib.get_message("gradual", seq)
        rebuilt = lib.reconstruct_message("gradual", seq)
        assert rebuilt == message, (seq, message, rebuilt)
        if seq > 0:
            changed = row.template_rev != lib.get_message("gradual", seq - 1).template_rev
        else:
            changed = True
        print(
            f"  seq {seq}: {message!r}\n"
            f"    -> T{row.template_id} rev {row.template_rev}"
            f"   template was {before!r}\n"
            f"    -> template now {lib.template_head(1)['render']!r}"
            f"   reconstruction exact={rebuilt == message}"
        )
        assert lib.template_head(1)["render"] == expected_renders[seq]
        assert row.template_rev == expected_revs[seq]
    print("  revisions:")
    show_revisions(lib, 1)
    print("  all four messages, before and after generalisation:")
    for seq, message in enumerate(messages):
        row = lib.get_message("gradual", seq)
        rebuilt = lib.reconstruct_message("gradual", seq)
        print(f"    seq {seq} refs rev {row.template_rev}: {rebuilt!r}")
        assert rebuilt == message
    # Early message's interpretation is the revision-1 snapshot.
    rev1 = lib.template_revision(1, lib.get_message("gradual", 0).template_rev)
    print(f"  seq 0 keeps its original structure: {rev1.render}")
    assert rev1.render == "job <INT> started on node a1"
    lib.close()


def section_prefix_relink(tmp):
    rule("2. Generalising a position inside the first `depth` words re-links the index")
    path = os.path.join(tmp, "relink.db")
    lib = Library.create(path, threshold="0.5", depth=2)
    print("  depth=2, so positions 0 and 1 are index edges")
    ingest(lib, ["alpha beta gamma delta"], source="relink")
    ingest(lib, ["alpha omega gamma delta"], start=1, source="relink")
    print("  after 'alpha omega gamma delta':")
    show_templates(lib)
    show_revisions(lib, 1)
    assert lib.template_head(1)["render"] == "alpha <*> gamma delta"
    # The leaf must have moved from lit:alpha/lit:beta to lit:alpha/wild.
    leaf_node = lib.conn.execute(
        "SELECT node_id FROM index_leaves WHERE template_id = 1"
    ).fetchone()[0]
    last_edge = lib.conn.execute(
        "SELECT slot_kind, slot_value FROM index_nodes WHERE node_id = ?",
        (leaf_node,),
    ).fetchone()
    print(f"  leaf now hangs under edge {last_edge}")
    assert last_edge == ("wild", "")
    # Now a brand new message whose second word is neither beta nor omega.
    outcome = ingest(lib, ["alpha zeta gamma delta"], start=2, source="relink")
    row = lib.get_message("relink", 2)
    report = lookup(
        lib.conn, 2, 4,
        (("literal", "alpha"), ("literal", "zeta"),
         ("literal", "gamma"), ("literal", "delta")),
        lib.threshold, lib.template_cache, lib.node_cache,
    )
    print(
        f"  new message 'alpha zeta gamma delta' -> T{row.template_id} "
        f"rev {row.template_rev}"
    )
    print(
        f"  index walk: nodes={report.nodes_visited} "
        f"candidates={report.candidates_examined} "
        f"branches={report.branches_expanded} pruned={report.branches_pruned} "
        f"of {len(lib.template_ids())} templates"
    )
    assert outcome.templates_created == []
    assert row.template_id == 1
    assert report.candidates_examined < len(lib.template_ids()) + 1
    assert lib.reconstruct_message("relink", 2) == "alpha zeta gamma delta"
    lib.close()


def section_competition(tmp):
    rule("3. Tie, mask clash, illegal IP, literal <*>, whitespace, empty message")
    path = os.path.join(tmp, "edge.db")
    lib = Library.create(path, threshold="0.5", depth=2)

    print("\n  3a. equal points, equal wildcards -> lower template id")
    ingest(lib, ["aa bb cc dd", "ee ff gg hh"], source="tie")
    report = lookup(
        lib.conn, 2, 4,
        (("literal", "aa"), ("literal", "bb"),
         ("literal", "gg"), ("literal", "hh")),
        lib.threshold, lib.template_cache, lib.node_cache,
    )
    print(f"    candidates: {[(c.template_id, c.points, c.wildcards) for c in report.candidates]}")
    # T1 rev1 aa,bb = 2/4; T2 rev2 gg,hh = 2/4.
    assert [(c.template_id, c.points) for c in report.candidates] == [
        (1, 2), (2, 2)
    ]
    outcome = ingest(lib, ["aa bb gg hh"], start=2, source="tie")
    row = lib.get_message("tie", 2)
    print(f"    selected T{row.template_id} rev {row.template_rev}")
    assert row.template_id == 1
    show_templates(lib)

    print("\n  3b. different mask types meet: <IPV4> vs <INT>")
    ingest(lib, ["from 10.0.0.1 port 80"], source="mask")
    mask_tid = lib.get_message("mask", 0).template_id
    ingest(lib, ["from 7 port 443"], start=1, source="mask")
    print(
        f"    'from 7 port 443' scored 3/4 -> "
        f"{lib.template_head(mask_tid)['render']}"
    )
    assert lib.template_head(mask_tid)["render"] == "from <*> port <INT>"
    ingest(lib, ["from 10.0.0.9 port 8080"], start=2, source="mask")
    row = lib.get_message("mask", 2)
    print(f"    later IPV4 message -> T{row.template_id} (once-IPV4 slot is now a wildcard)")
    assert row.template_id == mask_tid

    print("\n  3c. illegal IPv4 octets stay literal")
    ingest(lib, ["peer 256.0.0.1 down"], source="ip")
    first_ip = lib.get_message("ip", 0)
    ip_tid = first_ip.template_id
    rev = lib.template_revision(ip_tid, first_ip.template_rev)
    print(f"    stored position 1 as {rev.items[1].kind} {rev.items[1].value!r}")
    assert rev.items[1].kind == "literal"
    ingest(lib, ["peer 999.1.1.1 down"], start=1, source="ip")
    print(f"    second illegal IP: {lib.template_head(ip_tid)['render']}")
    assert lib.template_head(ip_tid)["render"] == "peer <*> down"

    print("\n  3d. a literal word '<*>' is not a wildcard")
    lib2 = Library.create(os.path.join(tmp, "lit.db"), threshold="1")
    ingest(lib2, ["pattern <*> here"], source="lit")
    rev = lib2.template_revision(1, 1)
    print(f"    stored item: {rev.items[1].kind} {rev.items[1].value!r}")
    print(f"    render: {rev.render}")
    assert rev.items[1].kind == "literal"
    assert rev.items[1].value == "<*>"
    ingest(lib2, ["pattern x here"], start=1, source="lit")
    print(f"    'pattern x here' -> T{lib2.get_message('lit', 1).template_id} (new, not absorbed)")
    assert lib2.get_message("lit", 1).template_id == 2
    lib2.close()

    print("\n  3e. whitespace differences and empty messages")
    lib3 = Library.create(os.path.join(tmp, "ws.db"), threshold="1")
    ingest(lib3, ["", " \t\n ", "   "], source="ws")
    print(f"    zero-word messages -> {lib3.template_ids()}, "
          f"word_count={lib3.template_head(1)['word_count']}")
    assert lib3.template_ids() == [1]
    ingest(lib3, ["  aa\t\tbb  ", "aa  bb", "aa\tbb"], start=3, source="ws")
    print(f"    whitespace variants -> {lib3.template_ids()}")
    for seq in range(6):
        text = lib3.reconstruct_message("ws", seq)
        print(f"      seq {seq}: {text!r} exact")
        assert lib3.get_message("ws", seq).message == text
        assert text in ["", " \t\n ", "   ", "  aa\t\tbb  ", "aa  bb", "aa\tbb"]
    assert lib3.template_ids() == [1, 2]
    lib3.close()
    lib.close()


def section_perfect_match(tmp):
    rule("4. A perfect match reuses the revision and only bumps membership")
    path = os.path.join(tmp, "perfect.db")
    lib = Library.create(path, threshold="0.5")
    ingest(lib, ["host 10.0.0.1 alive"], source="perfect")
    ingest(lib, ["host 10.0.0.2 alive"], start=1, source="perfect")
    head = lib.template_head(1)
    print(f"  T1 rev {head['revision']} members {head['member_count']} :: {head['render']}")
    assert head["revision"] == 1
    assert head["member_count"] == 2
    assert len(lib.template_revisions(1)) == 1
    lib.close()


def section_protocol(tmp):
    rule("5. Position protocol: replay ignored, changed replay conflicts, gap refused")
    path = os.path.join(tmp, "protocol.db")
    lib = Library.create(path, threshold="0.5")
    ingest(lib, ["alpha beta 10.0.0.1"], source="p")
    outcome = lib.ingest_batch([Record("p", 0, "alpha beta 10.0.0.1")])
    print(f"  replay of seq 0 (same text): accepted={outcome.accepted} "
          f"ignored={outcome.ignored_replays}")
    assert outcome.accepted == [] and outcome.ignored_replays == [("p", 0)]
    try:
        lib.ingest_batch([Record("p", 0, "alpha beta 10.0.0.2")])
    except ConflictError as exc:
        print(f"  replay of seq 0 (changed text): conflict -> {exc}")
        assert "conflict" in str(exc)
    try:
        lib.ingest_batch([Record("p", 3, "gap data")])
    except GapError as exc:
        print(f"  jump from 1 to 3: refused -> {exc}")
    print(f"  committed positions: next_seq={lib.next_seq('p')} "
          f"messages={lib.message_count()}")
    assert lib.next_seq("p") == 1
    assert lib.message_count() == 1
    lib.close()


def main():
    with tempfile.TemporaryDirectory() as tmp:
        section_gradual(tmp)
        section_prefix_relink(tmp)
        section_competition(tmp)
        section_perfect_match(tmp)
        section_protocol(tmp)
    print()
    print("all demonstration assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
