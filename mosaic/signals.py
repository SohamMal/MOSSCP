"""Corroborating signals: consistent renaming, compression distance, profile.

None of these is a detector on its own. Their job is to make the verdict
*explainable* and to catch the cases the first two layers miss.
"""

from __future__ import annotations

import difflib
import math
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from .structure import Node, walk
from .tokens import NAME, Token
from .winnow import Block


# --- consistent renaming -------------------------------------------------


@dataclass(slots=True)
class RenameEvidence:
    """Evidence that one file is the other with identifiers substituted."""

    pairs: int = 0
    distinct: int = 0
    consistency: float = 0.0  # share of occurrences following the dominant map
    injective: float = 0.0  # share of targets used by exactly one source
    renamed: float = 0.0  # share of distinct mappings that change the name
    score: float = 0.0
    examples: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.distinct < 3:
            return "not enough identifier evidence"
        pct = round(100 * self.consistency)
        kind = "renamed" if self.renamed > 0.5 else "largely unchanged"
        return f"{self.distinct} identifiers in 1:1 correspondence, {pct}% consistent, {kind}"


def rename_evidence(
    toks_a: Sequence[Token],
    toks_b: Sequence[Token],
    norm_a: Sequence[str],
    norm_b: Sequence[str],
    blocks: Sequence[Block],
    *,
    max_blocks: int = 40,
) -> RenameEvidence:
    """Align matched passages and look for a one-to-one identifier map.

    A copier who renames every variable defeats token fingerprint *values*
    but produces something MOSS never reports: a near-bijection between
    the two identifier sets. That is far stronger evidence than a
    similarity percentage, and it is what you show a student.
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    for bl in sorted(blocks, key=lambda b: -(b.len_a + b.len_b))[:max_blocks]:
        a_slice = norm_a[bl.a0 : bl.a1]
        b_slice = norm_b[bl.b0 : bl.b1]
        sm = difflib.SequenceMatcher(None, a_slice, b_slice, autojunk=False)
        for i, j, size in sm.get_matching_blocks():
            for off in range(size):
                ta = toks_a[bl.a0 + i + off]
                tb = toks_b[bl.b0 + j + off]
                if ta.kind == NAME and tb.kind == NAME:
                    counts[ta.value][tb.value] += 1

    if not counts:
        return RenameEvidence()

    total = 0
    agree = 0
    chosen: list[tuple[str, str, int]] = []
    for src, targets in counts.items():
        n = sum(targets.values())
        tgt, c = targets.most_common(1)[0]
        total += n
        agree += c
        chosen.append((src, tgt, c))

    target_use = Counter(t for _, t, _ in chosen)
    injective = sum(1 for _, t, _ in chosen if target_use[t] == 1) / len(chosen)
    renamed = sum(1 for s, t, _ in chosen if s != t) / len(chosen)
    consistency = agree / total if total else 0.0
    evidence = 1.0 - math.exp(-len(chosen) / 6.0)  # need several identifiers
    score = consistency * injective * evidence
    chosen.sort(key=lambda x: -x[2])
    return RenameEvidence(
        pairs=total,
        distinct=len(chosen),
        consistency=consistency,
        injective=injective,
        renamed=renamed,
        score=score,
        examples=chosen[:12],
    )


# --- normalized compression distance -------------------------------------


def compressed_size(data: bytes) -> int:
    return len(zlib.compress(data, 9))


def ncd_similarity(a: bytes, b: bytes, ca: int | None = None, cb: int | None = None) -> float:
    """1 - NCD(a, b). A cheap, assumption-free catch-all."""
    ca = compressed_size(a) if ca is None else ca
    cb = compressed_size(b) if cb is None else cb
    cab = compressed_size(a + b)
    lo, hi = min(ca, cb), max(ca, cb)
    if hi == 0:
        return 0.0
    ncd = (cab - lo) / hi
    return max(0.0, min(1.0, 1.0 - ncd))


def normalized_blob(norm_tokens: Sequence[str]) -> bytes:
    return " ".join(norm_tokens).encode("utf-8", "replace")


# --- structural profile --------------------------------------------------

_PROFILE_KEYS = (
    "FUNC", "CLASS", "LOOP", "BRANCH", "ASSIGN", "CALL", "RET", "BREAK",
    "CONT", "TRY", "RAISE", "WITH", "IMPORT", "VAR", "CONST", "BIN", "CMP",
    "BOOL", "UN", "SUB", "SEQ", "MAP", "ATTR", "B", "EXPR", "BLOCK", "ASSERT",
)


def profile(root: Node | None) -> dict[str, float]:
    """Label histogram plus shape statistics, as a comparable vector."""
    if root is None:
        return {}
    counts: Counter = Counter()
    depths: list[int] = []
    fn_sizes: list[int] = []
    for n in walk(root):
        head = n.label.split("/", 1)[0]
        counts[head] += 1
        if head == "FUNC":
            fn_sizes.append(n.size)
        if not n.children:
            depths.append(n.depth)
    total = max(1, sum(counts.values()))
    vec = {k: counts.get(k, 0) / total for k in _PROFILE_KEYS}
    vec["_nesting"] = root.depth / 24.0
    vec["_fn_mean"] = (sum(fn_sizes) / len(fn_sizes) / 120.0) if fn_sizes else 0.0
    vec["_fn_count"] = min(1.0, len(fn_sizes) / 20.0)
    return vec


def profile_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Hellinger affinity over the histogram, damped by shape differences."""
    if not a or not b:
        return 0.0
    hist_keys = [k for k in a if not k.startswith("_")]
    aff = sum(math.sqrt(max(0.0, a[k]) * max(0.0, b[k])) for k in hist_keys)
    shape = 1.0
    for k in ("_nesting", "_fn_mean", "_fn_count"):
        d = abs(a.get(k, 0.0) - b.get(k, 0.0))
        shape *= 1.0 / (1.0 + 2.0 * d)
    return max(0.0, min(1.0, aff * (0.6 + 0.4 * shape)))
