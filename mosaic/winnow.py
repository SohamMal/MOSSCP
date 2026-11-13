"""Winnowing fingerprints (Schleimer, Wilcox & Aiken, SIGMOD 2003).

This is the layer MOSS is built on, reproduced here as the baseline signal:
k-gram hashing over a normalized token stream, with one hash selected per
window of w so that any shared passage of at least k + w - 1 tokens is
guaranteed to produce a shared fingerprint.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Iterable, Sequence

_MOD = (1 << 61) - 1
_BASE = 1_000_003


def _token_ids(tokens: Sequence[str]) -> list[int]:
    cache: dict[str, int] = {}
    out = []
    for t in tokens:
        i = cache.get(t)
        if i is None:
            i = zlib.crc32(t.encode("utf-8", "replace")) or 1
            cache[t] = i
        out.append(i)
    return out


def kgram_hashes(tokens: Sequence[str], k: int) -> list[int]:
    """Rolling hash of every k-gram. Result index i covers tokens[i:i+k]."""
    ids = _token_ids(tokens)
    n = len(ids)
    if n < k:
        return []
    high = pow(_BASE, k - 1, _MOD)
    h = 0
    for i in range(k):
        h = (h * _BASE + ids[i]) % _MOD
    out = [h]
    for i in range(k, n):
        h = ((h - ids[i - k] * high) * _BASE + ids[i]) % _MOD
        out.append(h)
    return out


def winnow(hashes: Sequence[int], w: int) -> list[tuple[int, int]]:
    """Select one (hash, position) per window of *w*, rightmost minimum."""
    n = len(hashes)
    if n == 0:
        return []
    if n <= w:
        best = min(range(n), key=lambda i: (hashes[i], -i))
        return [(hashes[best], best)]
    out: list[tuple[int, int]] = []
    # monotonic deque of indices with increasing hash values
    dq: list[int] = []
    last_picked = -1
    for i in range(n):
        while dq and hashes[dq[-1]] >= hashes[i]:
            dq.pop()
        dq.append(i)
        if i >= w - 1:
            while dq[0] <= i - w:
                dq.pop(0)
            pick = dq[0]
            # rightmost among equal minima inside the window
            mn = hashes[pick]
            for j in dq:
                if hashes[j] == mn:
                    pick = j
            if pick != last_picked:
                out.append((hashes[pick], pick))
                last_picked = pick
    return out


@dataclass(slots=True)
class Fingerprints:
    """Winnowed fingerprints of one document."""

    n_tokens: int
    k: int
    index: dict[int, list[int]]  # hash -> token positions

    @property
    def hashes(self) -> set[int]:
        return set(self.index)

    def __len__(self) -> int:
        return sum(len(v) for v in self.index.values())


def fingerprint(norm_tokens: Sequence[str], k: int, w: int, *, max_occurrences: int = 12) -> Fingerprints:
    sel = winnow(kgram_hashes(norm_tokens, k), w)
    index: dict[int, list[int]] = {}
    for h, pos in sel:
        bucket = index.setdefault(h, [])
        if len(bucket) < max_occurrences:  # guard against highly repetitive files
            bucket.append(pos)
    return Fingerprints(len(norm_tokens), k, index)


@dataclass(slots=True)
class Block:
    """A matched passage, as half-open token ranges on both sides."""

    a0: int
    a1: int
    b0: int
    b1: int
    n_fp: int  # fingerprints supporting this block

    @property
    def len_a(self) -> int:
        return self.a1 - self.a0

    @property
    def len_b(self) -> int:
        return self.b1 - self.b0


def shared_pairs(a: Fingerprints, b: Fingerprints, ignore: Iterable[int] = ()) -> list[tuple[int, int]]:
    skip = set(ignore)
    small, large = (a.index, b.index) if len(a.index) <= len(b.index) else (b.index, a.index)
    flip = small is b.index
    pairs: list[tuple[int, int]] = []
    for h, pa in small.items():
        if h in skip:
            continue
        pb = large.get(h)
        if not pb:
            continue
        for i in pa:
            for j in pb:
                pairs.append((j, i) if flip else (i, j))
    pairs.sort()
    return pairs


def chain_blocks(pairs: Sequence[tuple[int, int]], k: int, max_gap: int) -> list[Block]:
    """Chain fingerprint hits into contiguous matched passages.

    A hit joins an existing run when it advances on both sides by no more
    than *max_gap* tokens, which tolerates inserted or reordered lines
    inside an otherwise copied block.
    """
    runs: list[list[int]] = []  # [a_first, a_last, b_first, b_last, count]
    for a, b in pairs:
        best = None
        best_cost = None
        for r in runs:
            da, db = a - r[1], b - r[3]
            if 0 <= da <= max_gap and 0 <= db <= max_gap and (da or db):
                cost = da + db + abs(da - db)
                if best_cost is None or cost < best_cost:
                    best, best_cost = r, cost
        if best is None:
            runs.append([a, a, b, b, 1])
        else:
            best[1] = a
            best[3] = b
            best[4] += 1
    blocks = [Block(r[0], r[1] + k, r[2], r[3] + k, r[4]) for r in runs]
    blocks.sort(key=lambda bl: -(bl.len_a + bl.len_b))
    return blocks


def covered(intervals: Iterable[tuple[int, int]]) -> int:
    """Total length of the union of half-open intervals."""
    iv = sorted(intervals)
    total = 0
    cur_s = cur_e = None
    for s, e in iv:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        elif e > cur_e:
            cur_e = e
    if cur_e is not None:
        total += cur_e - cur_s
    return total
