"""Normalized structure trees, subtree hashing, and tree matching.

This is the layer MOSS does not have. Token fingerprints die the moment a
copier rewrites `for` as `while`, splits one statement into two, or moves
functions around. A hashed tree of *normalized* structure survives all
three, because the normalization throws away exactly the things that are
cheap to change and keeps the shape of the computation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterator, Sequence

#: Labels whose children are compared as a set, so reordering independent
#: statements or functions does not hide a copy.
UNORDERED_LABELS = frozenset({"MODULE", "BLOCK", "CLASS", "ARGS", "SET"})


@dataclass(slots=True)
class Node:
    label: str
    children: list["Node"] = field(default_factory=list)
    line: int = 0
    end_line: int = 0
    size: int = 1  # nodes in subtree, filled by finalize()
    hash: int = 0  # structural hash, filled by finalize()
    depth: int = 0

    def add(self, child: "Node | None") -> "Node":
        if child is not None:
            self.children.append(child)
        return self


def _digest(label: str, child_hashes: Sequence[int], unordered: bool) -> int:
    hs = sorted(child_hashes) if unordered else list(child_hashes)
    h = hashlib.blake2b(digest_size=8)
    h.update(label.encode("utf-8", "replace"))
    h.update(b"\x00")
    for c in hs:
        h.update(c.to_bytes(8, "little"))
    return int.from_bytes(h.digest(), "little")


def finalize(root: Node) -> Node:
    """Fill size, hash, depth and line spans bottom-up."""
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, done = stack.pop()
        if not done:
            stack.append((node, True))
            for c in node.children:
                stack.append((c, False))
        else:
            node.size = 1 + sum(c.size for c in node.children)
            node.depth = 1 + max((c.depth for c in node.children), default=0)
            node.hash = _digest(
                node.label,
                [c.hash for c in node.children],
                node.label in UNORDERED_LABELS,
            )
            if node.children:
                node.line = min([node.line or 10**9] + [c.line for c in node.children if c.line])
                if node.line == 10**9:
                    node.line = 0
                node.end_line = max([node.end_line] + [c.end_line for c in node.children])
            if node.end_line < node.line:
                node.end_line = node.line
    return root



def flatten_blocks(root: Node) -> Node:
    """Splice a BLOCK that sits directly inside a BLOCK/MODULE.

    A bare compound statement, or the init-hoisting done when rewriting a
    `for` loop, otherwise adds a nesting level that the equivalent code
    does not have.
    """
    for node in _postorder(root):
        if node.label not in ("BLOCK", "MODULE"):
            continue
        out: list[Node] = []
        for c in node.children:
            if c.label == "BLOCK":
                out.extend(c.children)
            else:
                out.append(c)
        node.children = out
    return root


def _postorder(root: Node) -> list[Node]:
    order: list[Node] = []
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        n, done = stack.pop()
        if done:
            order.append(n)
        else:
            stack.append((n, True))
            for c in n.children:
                stack.append((c, False))
    return order


def prepare(root: Node | None) -> Node | None:
    """Flatten then finalize -- the standard pipeline for a built tree."""
    if root is None:
        return None
    return finalize(flatten_blocks(root))

def walk(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


@dataclass(slots=True)
class TreeIndex:
    """Subtree-hash index of one document's structure tree."""

    root: Node
    total: int
    bag: dict[int, list[Node]]
    min_size: int

    @property
    def hashes(self) -> set[int]:
        return set(self.bag)


def index_tree(root: Node, min_size: int = 6) -> TreeIndex:
    bag: dict[int, list[Node]] = {}
    for n in walk(root):
        if n.size >= min_size:
            bag.setdefault(n.hash, []).append(n)
    return TreeIndex(root, root.size, bag, min_size)


@dataclass(slots=True)
class TreeMatch:
    a: Node
    b: Node
    size: int


def match_trees(a: TreeIndex, b: TreeIndex, ignore: set[int] | None = None) -> list[TreeMatch]:
    """Greedy maximal common-subtree matching.

    Largest shared subtrees first; once a subtree is matched its
    descendants are not reported again, so the result reads as a list of
    distinct shared constructs rather than a cloud of fragments.
    """
    skip = ignore or set()
    shared = [h for h in a.bag if h in b.bag and h not in skip]
    cands: list[tuple[int, int]] = sorted(
        ((a.bag[h][0].size, h) for h in shared), reverse=True
    )
    used_a: set[int] = set()
    used_b: set[int] = set()
    out: list[TreeMatch] = []
    for size, h in cands:
        avail_a = [n for n in a.bag[h] if id(n) not in used_a]
        avail_b = [n for n in b.bag[h] if id(n) not in used_b]
        for na, nb in zip(avail_a, avail_b):
            if _covered(na, used_a) or _covered(nb, used_b):
                continue
            out.append(TreeMatch(na, nb, size))
            for d in walk(na):
                used_a.add(id(d))
            for d in walk(nb):
                used_b.add(id(d))
    out.sort(key=lambda m: -m.size)
    return out


def _covered(node: Node, used: set[int]) -> bool:
    return id(node) in used


def coverage(matches: Sequence[TreeMatch], total_a: int, total_b: int) -> tuple[float, float]:
    ca = sum(m.size for m in matches)
    cb = ca  # matched subtrees are isomorphic, so identical node counts
    return (ca / total_a if total_a else 0.0, cb / total_b if total_b else 0.0)


def line_spans(matches: Sequence[TreeMatch]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    sa = [(m.a.line, m.a.end_line) for m in matches if m.a.line]
    sb = [(m.b.line, m.b.end_line) for m in matches if m.b.line]
    return sa, sb
