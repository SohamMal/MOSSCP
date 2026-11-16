"""A MOSS engine, independent of mosaic's own scoring.

This follows the algorithm Stanford's MOSS is built on (Schleimer, Wilcox &
Aiken, "Winnowing: Local Algorithms for Document Fingerprinting", SIGMOD
2003) and MOSS's documented behaviour:

* Whitespace and comments are ignored and every identifier is the same
  token, so reformatting and renaming do not hide a copy. Keywords,
  operators and punctuation are kept. Nothing else is canonicalised (no
  for/while equivalence, no structure matching: that is mosaic, not MOSS).
* Every k-gram of tokens is hashed, and winnowing keeps the minimum hash of
  each window of w, which guarantees that any shared passage of at least
  k + w - 1 tokens produces a shared fingerprint.
* With submissions grouped by directory (MOSS's -d), each submission is one
  program and files of the same submission are never compared.
* A passage that appears in more than ``max_matches`` programs (MOSS's -m,
  default 10) is treated as legitimately shared and ignored, as is anything
  in a base file (-b). Nothing else about the rest of the class affects a
  pair: there is no cohort statistics, no z-score, no automatic boilerplate.
* For each pair, matching fingerprints are chained into passages. Each
  program's percentage is the share of its tokens inside those passages,
  and "lines matched" counts the lines they span. Pairs are ranked by lines
  matched, and the top ``show`` pairs (MOSS's -n, default 250) are listed.

Stanford has never published MOSS's source, so its exact tokenizers and
parameter values are unknown; k and w here are configurable.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from . import tokens as tk
from .winnow import covered, kgram_hashes, winnow

MAX_POSITIONS = 16  # occurrences of one fingerprint kept per program


@dataclass
class MossConfig:
    k: int = 12  # noise threshold: k-gram length in tokens
    w: int = 8  # winnowing window; guaranteed detection at k + w - 1 tokens
    max_matches: int = 10  # MOSS -m
    show: int = 250  # MOSS -n
    by_dir: bool = True  # MOSS -d
    lang: str | None = None  # MOSS -l (None: by file extension)


@dataclass
class MossFile:
    rel: str
    source: str
    lang: str
    lines: list[int]  # line of each token
    start: int  # offset of this file's first token in the program
    n_lines: int

    @property
    def n_tokens(self) -> int:
        return len(self.lines)


@dataclass
class Program:
    name: str  # submission (or file, without -d)
    files: list[MossFile]
    n_tokens: int
    index: dict[int, list[int]]  # fingerprint -> token positions in the program

    def locate(self, pos: int) -> tuple[MossFile, int]:
        """The file and line of a program-wide token position."""
        starts = [f.start for f in self.files]
        f = self.files[bisect.bisect_right(starts, pos) - 1]
        return f, f.lines[pos - f.start]


@dataclass
class Passage:
    a0: int
    a1: int  # token range in program A, half-open
    b0: int
    b1: int


@dataclass
class MossPair:
    a: Program
    b: Program
    pct_a: float
    pct_b: float
    lines: int
    passages: list[Passage]

    @property
    def score(self) -> float:
        return max(self.pct_a, self.pct_b)


@dataclass
class MossReport:
    cfg: MossConfig
    programs: list[Program]
    pairs: list[MossPair]  # every matching pair, best first
    ignored: int  # fingerprints dropped by -m or the base file
    n_compared: int
    skipped: list[str] = field(default_factory=list)

    @property
    def shown(self) -> list[MossPair]:
        return self.pairs[: self.cfg.show]


def normalize(toks: list[tk.Token]) -> list[str]:
    out = []
    for t in toks:
        if t.kind == tk.NAME:
            out.append("V")
        elif t.kind == tk.NUMBER:
            out.append("N")
        elif t.kind == tk.STRING:
            out.append("S")
        else:
            out.append(t.value)
    return out


def _fingerprint_file(source: str, lang: str, cfg: MossConfig) -> tuple[list[int], dict[int, list[int]]]:
    toks = tk.tokenize(source, lang)
    index: dict[int, list[int]] = {}
    for h, pos in winnow(kgram_hashes(normalize(toks), cfg.k), cfg.w):
        index.setdefault(h, []).append(pos)
    return [t.line for t in toks], index


def build_program(name: str, files: Iterable[tuple[str, str]], cfg: MossConfig) -> Program | None:
    """files: (display path, source text). Fingerprints never span two files."""
    out_files: list[MossFile] = []
    index: dict[int, list[int]] = defaultdict(list)
    offset = 0
    for rel, source in sorted(files):
        lang = cfg.lang or tk.lang_for_path(rel)
        lines, idx = _fingerprint_file(source, lang, cfg)
        if not lines:
            continue
        out_files.append(MossFile(rel, source, lang, lines, offset, source.count("\n") + 1))
        for h, ps in idx.items():
            bucket = index[h]
            for p in ps:
                if len(bucket) < MAX_POSITIONS:
                    bucket.append(p + offset)
        offset += len(lines)
    if not out_files:
        return None
    return Program(name, out_files, offset, dict(index))


def _passages(a: Program, b: Program, hits: list[tuple[int, int]], cfg: MossConfig) -> list[Passage]:
    """Chain hits that advance together on both sides into passages."""
    runs: list[list[int]] = []  # [a_first, a_last, b_first, b_last]
    for pa, pb in hits:
        best = None
        for r in runs:
            da, db = pa - r[1], pb - r[3]
            if 0 < da <= cfg.w and 0 < db <= cfg.w and da == db:
                best = r
                break
        if best is None:
            for r in runs:
                da, db = pa - r[1], pb - r[3]
                if 0 < da <= cfg.w and 0 < db <= cfg.w:
                    best = r
                    break
        if best is not None and a.locate(pa)[0] is a.locate(best[1])[0] and b.locate(pb)[0] is b.locate(best[3])[0]:
            best[1], best[3] = pa, pb
        else:
            runs.append([pa, pa, pb, pb])
    found = [Passage(r[0], r[1] + cfg.k, r[2], r[3] + cfg.k) for r in runs]
    found.sort(key=lambda p: -(p.a1 - p.a0))
    # Repeated lines produce echoes of a passage at shifted offsets. Keep a
    # passage only if it adds matched text on at least one side.
    out: list[Passage] = []
    for p in found:
        sa = [(q.a0, q.a1) for q in out]
        sb = [(q.b0, q.b1) for q in out]
        new_a = covered(sa + [(p.a0, p.a1)]) - covered(sa)
        new_b = covered(sb + [(p.b0, p.b1)]) - covered(sb)
        if not out or new_a > (p.a1 - p.a0) // 10 or new_b > (p.b1 - p.b0) // 10:
            out.append(p)
    return out


def _lines(prog: Program, spans: list[tuple[int, int]]) -> int:
    seen: set[tuple[int, int]] = set()
    for lo, hi in spans:
        for pos in range(lo, min(hi, prog.n_tokens)):
            f, line = prog.locate(pos)
            seen.add((f.start, line))
    return len(seen)


def compare(a: Program, b: Program, ignore: set[int], cfg: MossConfig) -> MossPair | None:
    small, large, flip = (a, b, False) if len(a.index) <= len(b.index) else (b, a, True)
    hits = []
    for h, ps in small.index.items():
        if h in ignore:
            continue
        qs = large.index.get(h)
        if not qs:
            continue
        for p in ps:
            for q in qs:
                hits.append((q, p) if flip else (p, q))
    if not hits:
        return None
    hits.sort()
    passages = _passages(a, b, hits, cfg)
    sa = [(p.a0, p.a1) for p in passages]
    sb = [(p.b0, p.b1) for p in passages]
    pct_a = min(1.0, covered(sa) / max(1, a.n_tokens))
    pct_b = min(1.0, covered(sb) / max(1, b.n_tokens))
    lines = max(_lines(a, sa), _lines(b, sb))
    return MossPair(a, b, pct_a, pct_b, lines, passages)


def analyse(programs: list[Program], cfg: MossConfig, base: Iterable[Program] = (), progress=None) -> MossReport:
    df: dict[int, int] = defaultdict(int)
    for prog in programs:
        for h in prog.index:
            df[h] += 1
    ignore = {h for h, c in df.items() if c > cfg.max_matches}
    for bp in base:
        ignore |= set(bp.index)

    inv: dict[int, list[int]] = defaultdict(list)
    for i, prog in enumerate(programs):
        for h in prog.index:
            if h not in ignore:
                inv[h].append(i)
    cands: set[tuple[int, int]] = set()
    for ids in inv.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                cands.add((ids[x], ids[y]))

    pairs = []
    for n, (i, j) in enumerate(sorted(cands)):
        if progress and n % 50 == 0:
            progress("moss", n + 1, len(cands), "")
        p = compare(programs[i], programs[j], ignore, cfg)
        if p is not None:
            pairs.append(p)
    pairs.sort(key=lambda p: (-p.lines, -p.score, p.a.name, p.b.name))
    return MossReport(cfg, programs, pairs, len(ignore), len(cands))


def run_sources(
    files: Iterable[tuple[str, str, bytes]],
    cfg: MossConfig | None = None,
    base_files: Iterable[tuple[str, bytes]] = (),
    *,
    progress=None,
) -> MossReport:
    """files: (submission, relative path, raw bytes), held in memory."""
    from .engine import decode_source, is_source_path

    cfg = cfg or MossConfig()
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    skipped = []
    for sub, rel, raw in files:
        if not is_source_path(rel) or (cfg.lang and tk.lang_for_path(rel) != cfg.lang):
            continue
        text = decode_source(raw)
        display = f"{sub}/{rel}"
        if text is None:
            skipped.append(display)
            continue
        grouped[sub if cfg.by_dir else display].append((display, text))
    programs = [p for name, fs in sorted(grouped.items()) if (p := build_program(name, fs, cfg))]
    base = []
    for rel, raw in base_files:
        text = decode_source(raw) if is_source_path(rel) else None
        if text is not None:
            bp = build_program("__base__", [(rel, text)], cfg)
            if bp:
                base.append(bp)
    rep = analyse(programs, cfg, base, progress)
    rep.skipped = skipped
    return rep


def run_paths(roots: list[str], cfg: MossConfig | None = None, base_paths: Iterable[str] = ()) -> MossReport:
    """Like run_sources, for folders on disk (first path segment = submission)."""
    import os

    from .engine import iter_source_files

    cfg = cfg or MossConfig()
    base_dir = roots[0] if len(roots) == 1 and os.path.isdir(roots[0]) else os.path.commonpath(
        [os.path.abspath(r) for r in roots])
    items = []
    for path in iter_source_files(roots, cfg.lang):
        rel = os.path.relpath(path, base_dir).replace(os.sep, "/")
        sub, _, inner = rel.partition("/")
        with open(path, "rb") as fh:
            items.append((sub, inner or sub, fh.read()) if inner else (rel, rel, fh.read()))
    base = []
    for bp in base_paths:
        for path in iter_source_files([bp], cfg.lang):
            with open(path, "rb") as fh:
                base.append((os.path.basename(path), fh.read()))
    return run_sources(items, cfg, base)


def pair_summary(p: MossPair) -> dict:
    return {
        "a": p.a.name,
        "b": p.b.name,
        "pct_a": round(p.pct_a, 4),
        "pct_b": round(p.pct_b, 4),
        "lines": p.lines,
    }


def passage_lines(p: MossPair, ps: Passage) -> tuple[tuple[str, int, int], tuple[str, int, int]]:
    fa, la0 = p.a.locate(ps.a0)
    _, la1 = p.a.locate(min(ps.a1, p.a.n_tokens) - 1)
    fb, lb0 = p.b.locate(ps.b0)
    _, lb1 = p.b.locate(min(ps.b1, p.b.n_tokens) - 1)
    return (fa.rel, la0, la1), (fb.rel, lb0, lb1)
