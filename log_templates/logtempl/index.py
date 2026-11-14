"""Persistent candidate index and threshold-aware best-first lookup.

Structure
---------
One trie per word count.  The root is a node at level 0 with
``slot_kind='root'`` and ``slot_value=<word count>``.  A node at level ``L``
is reached by following the template's first ``L`` items; each edge is

    ("lit", exact literal text)      exact literal word
    ("ipv4"|"uuid"|"int", "")        a typed mask item
    ("wild", "")                     a learned wildcard item

A template of ``W`` words hangs as a leaf off the node at level
``min(depth, W)`` (directly off the root when ``W == 0``).  Because every
template in a word-count trie has the same length, the leaf level is uniform
within that trie.

Lookup
------
Given a message with ``W`` words, walk the W-word trie best-first:

* leaves of each reached node are scored exactly and kept when
  ``points / W >= threshold`` (``W == 0`` templates score 1 by definition);
* *every* child edge is expanded -- literal, all typed mask branches and the
  wildcard branch -- because a branch whose own position scores 0 can still
  hold a template that reaches the threshold through other positions;
* before descending, prune with the optimistic bound
  ``(points_so_far + unvisited_positions) / W``.  A wildcard edge adds no
  points; a literal edge adds one only if the message word is that same
  literal, a typed edge only for the same mask kind.  The heap is ordered by
  this bound: once the best remaining bound is below the threshold no
  unexplored branch can yield an admissible template, and the walk stops.

The lookup reports ``nodes_visited`` and ``candidates_examined`` -- the
number of index nodes/templates actually touched, independent of the total
template count -- plus expanded/pruned branch counters.

Materialisation
---------------
Nodes and leaves are derived state: ``IndexWriter.rebuild`` recreates them
from ``templates`` + ``template_items``.  Every generalization re-links the
template's leaf inside the same transaction as the revision, so a template
that becomes reachable at a new path is never missing from the index.
"""

from __future__ import annotations

import heapq
import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from .masks import INT, IPV4, UUID
from .templates import LITERAL, MASK, Item

_MASK_SLOT = {IPV4: "ipv4", UUID: "uuid", INT: "int"}
_SLOT_MASK = {v: k for k, v in _MASK_SLOT.items()}


def slot_for_item(item: Item) -> tuple[str, str]:
    if item.kind == LITERAL:
        return ("lit", item.value)
    if item.kind == MASK:
        return (_MASK_SLOT[item.value], "")
    return ("wild", "")


@dataclass(frozen=True)
class Candidate:
    template_id: int
    revision: int
    points: int
    wildcards: int
    word_count: int

    @property
    def score(self) -> Fraction:
        return Fraction(1, 1) if self.word_count == 0 else Fraction(
            self.points, self.word_count
        )


@dataclass
class LookupReport:
    candidates: list[Candidate]
    nodes_visited: int
    candidates_examined: int
    branches_expanded: int
    branches_pruned: int


class IndexWriter:
    """Mutates the materialised index inside the caller's transaction."""

    def __init__(self, conn: sqlite3.Connection, depth: int):
        self.conn = conn
        self.depth = depth

    def root_id(self, word_count: int, *, create: bool) -> int | None:
        row = self.conn.execute(
            "SELECT node_id FROM index_nodes "
            "WHERE parent_id IS NULL AND slot_kind = 'root' AND slot_value = ?",
            (str(word_count),),
        ).fetchone()
        if row is not None:
            return row[0]
        if not create:
            return None
        cur = self.conn.execute(
            "INSERT INTO index_nodes (level, parent_id, slot_kind, slot_value) "
            "VALUES (0, NULL, 'root', ?)",
            (str(word_count),),
        )
        return cur.lastrowid

    def child_id(self, parent_id: int, kind: str, value: str, level: int) -> int:
        row = self.conn.execute(
            "SELECT node_id FROM index_nodes "
            "WHERE parent_id = ? AND slot_kind = ? AND slot_value = ?",
            (parent_id, kind, value),
        ).fetchone()
        if row is not None:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO index_nodes (level, parent_id, slot_kind, slot_value) "
            "VALUES (?, ?, ?, ?)",
            (level, parent_id, kind, value),
        )
        return cur.lastrowid

    def leaf_node(self, word_count: int, items: tuple[Item, ...]) -> int:
        node = self.root_id(word_count, create=True)
        assert node is not None
        indexed = min(self.depth, word_count)
        for level in range(indexed):
            kind, value = slot_for_item(items[level])
            node = self.child_id(node, kind, value, level + 1)
        return node

    def existing_leaf_node(self, word_count: int,
                           items: tuple[Item, ...]) -> int | None:
        """Read-only lookup of the node a template leaf should attach to.

        Returns ``None`` if any node on the path is absent; never creates
        rows, so it is safe to call from integrity verification.
        """
        node = self.root_id(word_count, create=False)
        if node is None:
            return None
        indexed = min(self.depth, word_count)
        for level in range(indexed):
            kind, value = slot_for_item(items[level])
            row = self.conn.execute(
                "SELECT node_id FROM index_nodes "
                "WHERE parent_id = ? AND slot_kind = ? AND slot_value = ?",
                (node, kind, value),
            ).fetchone()
            if row is None:
                return None
            node = row[0]
        return node

    def attach(self, template_id: int, word_count: int,
               items: tuple[Item, ...], revision: int) -> None:
        node = self.leaf_node(word_count, items)
        self.conn.execute(
            "INSERT INTO index_leaves (node_id, template_id, revision) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(node_id, template_id) "
            "DO UPDATE SET revision = excluded.revision",
            (node, template_id, revision),
        )

    def detach(self, template_id: int) -> None:
        self.conn.execute(
            "DELETE FROM index_leaves WHERE template_id = ?", (template_id,)
        )

    def relink(self, template_id: int, word_count: int,
               items: tuple[Item, ...], revision: int) -> None:
        self.detach(template_id)
        self.attach(template_id, word_count, items, revision)

    def rebuild(self) -> None:
        conn = self.conn
        conn.execute("DELETE FROM index_leaves")
        conn.execute("DELETE FROM index_nodes")
        rows = conn.execute(
            "SELECT template_id, word_count, revision "
            "FROM templates ORDER BY template_id"
        ).fetchall()
        for template_id, word_count, revision in rows:
            items = load_items(conn, template_id, word_count)
            self.attach(template_id, word_count, items, revision)


def load_items(conn: sqlite3.Connection, template_id: int,
               word_count: int | None = None) -> tuple[Item, ...]:
    rows = conn.execute(
        "SELECT item_kind, item_value FROM template_items "
        "WHERE template_id = ? ORDER BY position",
        (template_id,),
    ).fetchall()
    if word_count is not None and len(rows) != word_count:
        raise ValueError(
            f"template {template_id}: item arity {len(rows)} != word_count "
            f"{word_count}"
        )
    return tuple(Item.from_row(k, v) for k, v in rows)


def _children(conn: sqlite3.Connection, node_id: int) -> list[tuple[int, str, str, int]]:
    return conn.execute(
        "SELECT node_id, slot_kind, slot_value, level FROM index_nodes "
        "WHERE parent_id = ? ORDER BY slot_kind, slot_value, node_id",
        (node_id,),
    ).fetchall()


def _leaves(conn: sqlite3.Connection, node_id: int) -> list[tuple[int, int, int]]:
    return conn.execute(
        "SELECT l.template_id, l.revision, t.word_count "
        "FROM index_leaves l JOIN templates t ON t.template_id = l.template_id "
        "WHERE l.node_id = ? ORDER BY t.template_id",
        (node_id,),
    ).fetchall()


def lookup(
    conn: sqlite3.Connection,
    depth: int,
    word_count: int,
    message_items: tuple[tuple[str, str], ...],
    threshold: Fraction,
    template_cache=None,
    node_cache=None,
) -> LookupReport:
    """Enumerate every admissible template with its exact score.

    ``message_items`` holds ``("literal", word)`` or ``("mask", kind)``.
    Ordering of returned candidates is deterministic:
    ``(-points, wildcards, template_id)``.
    """
    if len(message_items) != word_count:
        raise ValueError("message item count does not match word_count")
    if word_count == 0:
        return _lookup_zero_word(conn, depth, threshold, template_cache,
                                 node_cache)

    root_row = conn.execute(
        "SELECT node_id FROM index_nodes "
        "WHERE parent_id IS NULL AND slot_kind = 'root' AND slot_value = ?",
        (str(word_count),),
    ).fetchone()
    if root_row is None:
        return LookupReport([], 0, 0, 0, 0)
    leaf_level = min(depth, word_count)

    candidates: list[Candidate] = []
    nodes_visited = 0
    candidates_examined = 0
    branches_expanded = 0
    branches_pruned = 0

    heap: list[tuple] = [(-Fraction(word_count, word_count), 0, 0, root_row[0])]

    while heap:
        neg_bound, neg_points, level, node_id = heapq.heappop(heap)
        bound = -neg_bound
        if bound < threshold:
            branches_pruned += 1
            break  # heap is ordered: every remaining entry is no better

        nodes_visited += 1
        points_so_far = -neg_points

        if level == leaf_level:
            leaves = _leaves_cached(conn, node_cache, node_id)
            for template_id, revision, t_word in leaves:
                if t_word != word_count:
                    continue
                candidates_examined += 1
                template_items = _items_cached(
                    conn, template_cache, template_id, t_word
                )
                points, wildcards, _differing = score(
                    message_items, template_items
                )
                cand = Candidate(
                    template_id, revision, points, wildcards, word_count
                )
                if cand.score >= threshold:
                    candidates.append(cand)
            continue

        children = _children_cached(conn, node_cache, node_id)
        m_kind, m_value = message_items[level]
        for child_id, slot_kind, slot_value, child_level in children:
            branches_expanded += 1
            gained = 0
            if slot_kind == "lit":
                if m_kind == LITERAL and m_value == slot_value:
                    gained = 1
            elif slot_kind == "wild":
                gained = 0
            elif slot_kind in _SLOT_MASK:
                if m_kind == MASK and m_value == _SLOT_MASK[slot_kind]:
                    gained = 1
            else:  # pragma: no cover - corrupt index row
                raise ValueError(f"unknown index slot kind {slot_kind!r}")

            new_points = points_so_far + gained
            remaining = word_count - child_level
            if remaining < 0:
                remaining = 0
            new_bound = Fraction(new_points + remaining, word_count)
            if new_bound < threshold:
                branches_pruned += 1
                continue
            heapq.heappush(
                heap, (-new_bound, -new_points, child_level, child_id)
            )

    candidates.sort(key=lambda c: (-c.points, c.wildcards, c.template_id))
    return LookupReport(
        candidates, nodes_visited, candidates_examined,
        branches_expanded, branches_pruned,
    )


def _lookup_zero_word(conn, depth, threshold, template_cache,
                      node_cache) -> LookupReport:
    row = conn.execute(
        "SELECT node_id FROM index_nodes "
        "WHERE parent_id IS NULL AND slot_kind = 'root' AND slot_value = '0'"
    ).fetchone()
    if row is None:
        return LookupReport([], 0, 0, 0, 0)
    candidates: list[Candidate] = []
    examined = 0
    leaves = _leaves_cached(conn, node_cache, row[0])
    for template_id, revision, t_word in leaves:
        if t_word != 0:
            continue
        examined += 1
        candidates.append(Candidate(template_id, revision, 0, 0, 0))
    candidates.sort(key=lambda c: (-c.points, c.wildcards, c.template_id))
    return LookupReport(candidates, 1, examined, 0, 0)


def _items_cached(conn, cache, template_id: int, word_count: int) -> tuple[Item, ...]:
    if cache is None:
        return load_items(conn, template_id, word_count)
    return cache.get(
        ("items", template_id),
        lambda _k: load_items(conn, template_id, word_count),
    )


def _children_cached(conn, cache, node_id: int) -> list:
    if cache is None:
        return _children(conn, node_id)
    return cache.get(
        ("children", node_id), lambda _k: _children(conn, node_id)
    )


def _leaves_cached(conn, cache, node_id: int) -> list:
    if cache is None:
        return _leaves(conn, node_id)
    return cache.get(("leaves", node_id), lambda _k: _leaves(conn, node_id))


def score(
    message_items: tuple[tuple[str, str], ...],
    template_items: tuple[Item, ...],
) -> tuple[int, int, int]:
    """Aligned exact score: (points, wildcards, differing fixed positions)."""
    if len(message_items) != len(template_items):
        raise ValueError("word counts differ")
    points = 0
    wildcards = 0
    differing = 0
    for (m_kind, m_value), t_item in zip(message_items, template_items):
        if t_item.kind == "wildcard":
            wildcards += 1
            continue
        if m_kind == t_item.kind and m_value == t_item.value:
            points += 1
        else:
            differing += 1
    return points, wildcards, differing
