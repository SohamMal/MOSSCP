"""The detection engine: documents, cohort statistics, pairs, clusters."""

from __future__ import annotations

import math
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import cast, pyast, signals, structure, tokens as tk
from .structure import TreeIndex, TreeMatch
from .winnow import Block, Fingerprints, chain_blocks, covered, fingerprint, shared_pairs

DEFAULT_K = 14
DEFAULT_W = 12
DEFAULT_MIN_SUBTREE = 6


@dataclass
class Config:
    k: int = DEFAULT_K
    w: int = DEFAULT_W
    min_subtree: int = DEFAULT_MIN_SUBTREE
    max_df: float = 0.30  # drop fingerprints seen in more than this share of docs
    min_tokens: int = 40  # ignore files smaller than this
    # Scores are a share of each file's own code (provided/cohort-wide
    # material left out). Below this many tokens of own code, divide by this
    # instead, so a few coincidental lines in a file that is almost all
    # skeleton do not read as a high percentage.
    min_own: int = 120
    max_gap: int = 48  # token gap tolerated inside one matched passage
    threshold: float = 0.25  # report pairs whose mosaic or MOSS score reaches this
    cluster_threshold: float = 0.45
    weights: dict[str, float] = field(
        default_factory=lambda: {"fp": 0.38, "ast": 0.34, "rename": 0.12, "ncd": 0.09, "prof": 0.07}
    )
    by_dir: bool | None = None  # group files into submissions by top-level dir
    lang: str | None = None  # force a language


@dataclass
class Document:
    path: str
    rel: str
    submission: str
    lang: str
    source: str
    toks: list[tk.Token]
    norm: list[str]
    fp: Fingerprints
    tree: structure.Node | None
    tidx: TreeIndex | None
    prof: dict[str, float]
    blob: bytes
    cblob: int
    n_lines: int
    parsed: bool

    @property
    def n_tokens(self) -> int:
        return len(self.toks)


@dataclass
class Pair:
    a: Document
    b: Document
    fp_a: float = 0.0
    fp_b: float = 0.0
    ast_a: float = 0.0
    ast_b: float = 0.0
    ncd: float = 0.0
    prof: float = 0.0
    rename: signals.RenameEvidence = field(default_factory=signals.RenameEvidence)
    blocks: list[Block] = field(default_factory=list)
    tree_matches: list[TreeMatch] = field(default_factory=list)
    score: float = 0.0
    z: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def fp(self) -> float:
        return max(self.fp_a, self.fp_b)

    @property
    def moss(self) -> float:
        """MOSS-style score: the share of a file covered by passages of shared
        winnowed fingerprints (the larger of the two sides, as MOSS ranks).
        Material shared across the cohort, or given with --base, is excluded."""
        return self.fp

    @property
    def ast(self) -> float:
        return max(self.ast_a, self.ast_b)

    @property
    def key(self) -> tuple[str, str]:
        return (self.a.rel, self.b.rel)


SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "build", "dist", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
}


def iter_source_files(roots: Sequence[str], lang: str | None = None) -> list[str]:
    out: list[str] = []
    for root in roots:
        if os.path.isfile(root):
            out.append(os.path.abspath(root))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                detected = tk.lang_for_path(fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext not in tk.EXT_MAP:
                    continue
                if lang and detected != lang:
                    continue
                out.append(os.path.join(dirpath, fn))
    return out


def decode_source(raw: bytes) -> str | None:
    """Bytes -> text, or None for binary content."""
    if b"\x00" in raw[:4096]:
        return None
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return decode_source(raw)


def build_tree(source: str, toks: list[tk.Token], lang: str) -> tuple[structure.Node | None, bool]:
    family = tk.LANGS.get(lang, tk.LANGS["generic"]).family
    if family == "python":
        root = pyast.build(source)
        if root is not None:
            return structure.prepare(root), True
        # fall through to the tolerant parser when the file does not parse
    if family in ("c", "python", "generic"):
        root = cast.build(toks)
        return structure.prepare(root), family == "c"
    return None, False


def make_document(
    rel: str, source: str, cfg: Config, *, submission: str | None = None, path: str = ""
) -> Document | None:
    """Build a document from text already in memory.

    *rel* is the display path ("alice/main.c"); its first segment is the
    submission unless *submission* is given.
    """
    lang = cfg.lang or tk.lang_for_path(rel)
    toks = tk.tokenize(source, lang)
    if len(toks) < cfg.min_tokens:
        return None
    norm = tk.normalize(toks, lang)
    fp = fingerprint(norm, cfg.k, cfg.w)
    tree, parsed = build_tree(source, toks, lang)
    tidx = structure.index_tree(tree, cfg.min_subtree) if tree is not None else None
    blob = signals.normalized_blob(norm)
    sub = submission or (rel.split("/", 1)[0] if "/" in rel else rel)
    return Document(
        path=path or rel,
        rel=rel,
        submission=sub,
        lang=lang,
        source=source,
        toks=toks,
        norm=norm,
        fp=fp,
        tree=tree,
        tidx=tidx,
        prof=signals.profile(tree),
        blob=blob,
        cblob=signals.compressed_size(blob),
        n_lines=source.count("\n") + 1,
        parsed=parsed,
    )


def load_document(path: str, base_dir: str, cfg: Config) -> Document | None:
    source = _read(path)
    if source is None:
        return None
    rel = os.path.relpath(path, base_dir).replace(os.sep, "/")
    return make_document(rel, source, cfg, path=path)


# --- cohort-level boilerplate suppression --------------------------------


@dataclass
class Cohort:
    docs: list[Document]
    ignore_fp: set[int] = field(default_factory=set)
    ignore_tree: set[int] = field(default_factory=set)
    fp_df: Counter = field(default_factory=Counter)
    tree_df: Counter = field(default_factory=Counter)
    suppressed_fp: int = 0
    suppressed_tree: int = 0
    # Per document (keyed by id): the token ranges and structure nodes that
    # are provided or cohort-wide material. Scores are measured against the
    # rest -- the code the student actually wrote -- so a pair's score does
    # not drop just because more submissions arrive and the skeleton starts
    # being recognised.
    supp_spans: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    supp_nodes: dict[int, set[int]] = field(default_factory=dict)

    def own_tokens(self, d: "Document") -> tuple[list[tuple[int, int]], int]:
        spans = self.supp_spans.get(id(d), [])
        return spans, d.n_tokens - covered(spans)

    def own_nodes(self, d: "Document") -> tuple[set[int], int]:
        nodes = self.supp_nodes.get(id(d), set())
        return nodes, (d.tidx.total if d.tidx else 0) - len(nodes)


def build_cohort(docs: list[Document], cfg: Config, base_docs: Iterable[Document] = ()) -> Cohort:
    """Find and suppress what everybody shares.

    MOSS needs you to upload a base file. Here, anything that recurs
    across a large share of submissions is treated as provided material
    automatically -- skeleton code, the assignment's own template, the
    idioms the course taught. An explicit base file still works and is
    simply unioned in.
    """
    co = Cohort(docs)
    n = len(docs)
    for d in docs:
        co.fp_df.update(d.fp.index.keys())
        if d.tidx:
            co.tree_df.update(d.tidx.bag.keys())
    if n >= 10:
        limit = max(4, math.ceil(cfg.max_df * n))
        co.ignore_fp = {h for h, c in co.fp_df.items() if c >= limit}
        co.ignore_tree = {h for h, c in co.tree_df.items() if c >= limit}
    for b in base_docs:
        co.ignore_fp |= b.fp.hashes
        if b.tidx:
            co.ignore_tree |= b.tidx.hashes
    co.suppressed_fp = len(co.ignore_fp)
    co.suppressed_tree = len(co.ignore_tree)
    for d in docs:
        if co.ignore_fp:
            # w <= k, so consecutive winnowed k-grams of a suppressed passage
            # overlap and their union covers the passage.
            spans = [(p, p + d.fp.k) for h, ps in d.fp.index.items() if h in co.ignore_fp for p in ps]
            if spans:
                co.supp_spans[id(d)] = spans + _short_gaps(spans, d.n_tokens, d.fp.k + cfg.w - 1)
        if co.ignore_tree and d.tidx:
            nodes: set[int] = set()
            for h, ns in d.tidx.bag.items():
                if h in co.ignore_tree:
                    for n in ns:
                        if id(n) not in nodes:
                            nodes.update(id(x) for x in structure.walk(n))
            if nodes:
                co.supp_nodes[id(d)] = nodes
    return co


def _short_gaps(spans: list[tuple[int, int]], n: int, min_len: int) -> list[tuple[int, int]]:
    """Stretches between provided passages (or the file's ends) too short
    for winnowing to ever fingerprint. Nothing in them can be matched, so
    they are left out of the file's own size too -- e.g. a two-line header
    above the skeleton."""
    merged: list[list[int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, prev = [], 0
    for s, e in merged + [[n, n]]:
        if 0 < s - prev < min_len:
            out.append((prev, s))
        prev = max(prev, e)
    return out


def _own_share(matched: int, own: int, total: int, floor: int) -> float:
    """Share of a document's own (non-provided) material that is matched.
    The floor keeps a file that is almost all skeleton from scoring 100% on
    a few shared lines."""
    if total <= 0:
        return 0.0
    return min(1.0, matched / max(1, own, min(floor, total)))


def candidate_pairs(docs: list[Document], co: Cohort) -> set[tuple[int, int]]:
    """Pairs sharing at least one distinctive fingerprint or subtree.

    An inverted index keeps this near-linear in practice, so a cohort of
    a few thousand files is fine without comparing every pair.
    """
    cands: set[tuple[int, int]] = set()
    inv: dict[int, list[int]] = defaultdict(list)
    for i, d in enumerate(docs):
        for h in d.fp.index:
            if h not in co.ignore_fp:
                inv[h].append(i)
    inv_t: dict[int, list[int]] = defaultdict(list)
    for i, d in enumerate(docs):
        if not d.tidx:
            continue
        for h in d.tidx.bag:
            if h not in co.ignore_tree:
                inv_t[h].append(i)
    for table in (inv, inv_t):
        for ids in table.values():
            if len(ids) < 2 or len(ids) > 64:  # very common -> not distinctive
                continue
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    i, j = ids[x], ids[y]
                    if docs[i].lang != docs[j].lang:
                        continue
                    cands.add((i, j) if i < j else (j, i))
    return cands


# --- pair scoring --------------------------------------------------------


def score_pair(a: Document, b: Document, co: Cohort, cfg: Config) -> Pair:
    p = Pair(a, b)

    hits = shared_pairs(a.fp, b.fp, co.ignore_fp)
    if hits:
        p.blocks = [bl for bl in chain_blocks(hits, cfg.k, cfg.max_gap) if bl.n_fp >= 2 or bl.len_a >= cfg.k * 2]
        if p.blocks:
            for side, d, spans_of in (("a", a, lambda bl: (bl.a0, bl.a1)), ("b", b, lambda bl: (bl.b0, bl.b1))):
                supp, own = co.own_tokens(d)
                matched = [spans_of(bl) for bl in p.blocks]
                # count only matched tokens outside provided material
                hit = covered(matched + supp) - covered(supp) if supp else covered(matched)
                setattr(p, f"fp_{side}", _own_share(hit, own, d.n_tokens, cfg.min_own))

    if a.tidx and b.tidx:
        p.tree_matches = structure.match_trees(a.tidx, b.tidx, co.ignore_tree)
        if p.tree_matches:
            for side, d, root_of in (("a", a, lambda m: m.a), ("b", b, lambda m: m.b)):
                supp, own = co.own_nodes(d)
                if supp:
                    hit = sum(1 for m in p.tree_matches for x in structure.walk(root_of(m)) if id(x) not in supp)
                else:
                    hit = sum(m.size for m in p.tree_matches)
                setattr(p, f"ast_{side}", _own_share(hit, own, d.tidx.total, cfg.min_own))

    p.ncd = signals.ncd_similarity(a.blob, b.blob, a.cblob, b.cblob)
    p.prof = signals.profile_similarity(a.prof, b.prof)
    if p.blocks:
        p.rename = signals.rename_evidence(a.toks, b.toks, a.norm, b.norm, p.blocks)

    p.score, p.reasons = fuse(p, cfg)
    return p


def fuse(p: Pair, cfg: Config) -> tuple[float, list[str]]:
    """Combine the signals, redistributing weight when one is unavailable."""
    w = dict(cfg.weights)
    have_ast = bool(p.a.tidx and p.b.tidx)
    if not have_ast:
        w["fp"] += w.pop("ast")
        w["ast"] = 0.0
    vals = {
        "fp": p.fp,
        "ast": p.ast,
        "rename": p.rename.score,
        "ncd": _rescale(p.ncd, 0.30, 0.95),
        "prof": _rescale(p.prof, 0.55, 0.98),
    }
    score = sum(w[k] * vals[k] for k in w)

    # Two independent strong signals mean more than one; reward agreement.
    strong = sum(1 for k in ("fp", "ast", "rename") if vals[k] >= 0.45)
    if strong >= 2:
        score = min(1.0, score * 1.18)

    reasons = []
    if p.fp >= 0.35:
        reasons.append(f"{round(100 * p.fp)}% of tokens in shared passages")
    if p.ast >= 0.35:
        reasons.append(f"{round(100 * p.ast)}% of normalized structure identical")
    if p.ast >= 0.35 and p.fp < 0.2:
        reasons.append("structure matches although the text does not -- rewritten, not retyped")
    if p.rename.score >= 0.4 and p.rename.renamed > 0.5:
        reasons.append(f"systematic renaming ({p.rename.distinct} identifiers, 1:1)")
    if vals["ncd"] >= 0.6 and p.fp < 0.2 and p.ast < 0.2:
        reasons.append("compresses as one file -- heavy but shallow obfuscation")
    if not reasons and score >= cfg.threshold:
        reasons.append("weak, diffuse similarity across several measures")
    return min(1.0, score), reasons


def _rescale(v: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def add_z_scores(pairs: list[Pair], population: Sequence[float]) -> tuple[float, float]:
    """Score a pair against the cohort, not against an absolute scale.

    In a cohort where everyone solves the same small problem, 40%
    similarity is background noise; in a diverse one it is remarkable. The
    z-score is what makes the number mean the same thing in both.

    *population* must be every pair that was scored, not just the ones
    that survived the threshold: statistics taken from an already-filtered
    tail understate the spread and inflate every z-score.
    """
    if len(population) < 8:
        return (0.0, 0.0)
    med = statistics.median(population)
    mad = statistics.median([abs(v - med) for v in population]) or 1e-9
    scale = 1.4826 * mad
    for p in pairs:
        p.z = (p.score - med) / scale
    return (med, scale)


# --- clustering ----------------------------------------------------------


class _DSU:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class Cluster:
    members: list[str]
    pairs: list[Pair]

    @property
    def mean_score(self) -> float:
        return sum(p.score for p in self.pairs) / len(self.pairs) if self.pairs else 0.0


def cluster(pairs: list[Pair], threshold: float, *, by_submission: bool = True) -> list[Cluster]:
    """Group transitively similar submissions.

    A ring of six students who shared one solution shows up as fifteen
    unremarkable-looking pairs in a flat list. As a cluster it is obvious.
    """
    dsu = _DSU()
    kept = [p for p in pairs if p.score >= threshold]  # clusters use the mosaic score
    for p in kept:
        ka = p.a.submission if by_submission else p.a.rel
        kb = p.b.submission if by_submission else p.b.rel
        if ka != kb:
            dsu.union(ka, kb)
    groups: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for p in kept:
        for key in (
            p.a.submission if by_submission else p.a.rel,
            p.b.submission if by_submission else p.b.rel,
        ):
            if key not in seen:
                seen.add(key)
                groups[dsu.find(key)].append(key)
    out: list[Cluster] = []
    for root, members in groups.items():
        if len(members) < 3:
            continue
        mset = set(members)
        inner = [
            p
            for p in kept
            if (p.a.submission if by_submission else p.a.rel) in mset
            and (p.b.submission if by_submission else p.b.rel) in mset
        ]
        out.append(Cluster(sorted(members), inner))
    out.sort(key=lambda c: (-len(c.members), -c.mean_score))
    return out


# --- top-level run -------------------------------------------------------


@dataclass
class Report:
    cfg: Config
    docs: list[Document]
    pairs: list[Pair]
    clusters: list[Cluster]
    cohort: Cohort
    base_docs: list[Document]
    median: float
    scale: float
    n_compared: int
    skipped: list[str]
    by_submission: bool


def run(
    roots: Sequence[str],
    cfg: Config | None = None,
    base_paths: Sequence[str] = (),
    *,
    progress=None,
) -> Report:
    """Analyse submissions on disk."""
    cfg = cfg or Config()
    base_dir = roots[0] if len(roots) == 1 and os.path.isdir(roots[0]) else os.path.commonpath(
        [os.path.abspath(r) for r in roots]
    )
    paths = iter_source_files(roots, cfg.lang)
    docs: list[Document] = []
    skipped: list[str] = []
    for i, path in enumerate(paths):
        if progress:
            progress("load", i + 1, len(paths), path)
        d = load_document(path, base_dir, cfg)
        if d is None:
            skipped.append(os.path.relpath(path, base_dir))
        else:
            docs.append(d)

    base_docs: list[Document] = []
    for bp in base_paths:
        for path in iter_source_files([bp], cfg.lang):
            d = load_document(path, os.path.dirname(path) or ".", cfg)
            if d is not None:
                base_docs.append(d)
    return analyse(docs, cfg, base_docs, progress=progress, skipped=skipped)


def is_source_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return "." in name and not name.startswith(".") and os.path.splitext(name)[1].lower() in tk.EXT_MAP


def run_sources(
    files: Iterable[tuple[str, str, bytes]],
    cfg: Config | None = None,
    base_files: Iterable[tuple[str, bytes]] = (),
    *,
    progress=None,
) -> Report:
    """Analyse submissions held in memory -- nothing is read from or written to disk.

    *files* yields (submission, relative path, raw bytes); *base_files*
    yields (relative path, raw bytes) of provided/skeleton code.
    """
    cfg = cfg or Config()
    items = list(files)
    docs: list[Document] = []
    skipped: list[str] = []
    for i, (sub, rel, raw) in enumerate(items):
        display = f"{sub}/{rel}"
        if progress:
            progress("load", i + 1, len(items), display)
        if not is_source_path(rel):
            continue  # not source code
        if cfg.lang and tk.lang_for_path(rel) != cfg.lang:
            continue
        text = decode_source(raw)
        d = make_document(display, text, cfg, submission=sub) if text is not None else None
        if d is None:
            skipped.append(display)
        else:
            docs.append(d)
    base_docs: list[Document] = []
    for rel, raw in base_files:
        if not is_source_path(rel):
            continue
        text = decode_source(raw)
        d = make_document(rel, text, cfg, submission="__base__") if text is not None else None
        if d is not None:
            base_docs.append(d)
    if cfg.by_dir is None:
        cfg.by_dir = True  # every in-memory file is labelled with its submission
    return analyse(docs, cfg, base_docs, progress=progress, skipped=skipped)


def analyse(
    docs: list[Document],
    cfg: Config,
    base_docs: Sequence[Document] = (),
    *,
    progress=None,
    skipped: Sequence[str] = (),
) -> Report:
    """Cohort statistics, pair scoring and clustering over loaded documents."""
    co = build_cohort(docs, cfg, base_docs)
    by_sub = cfg.by_dir
    if by_sub is None:
        by_sub = len({d.submission for d in docs}) > 1 and any("/" in d.rel for d in docs)

    cands = candidate_pairs(docs, co)
    pairs: list[Pair] = []
    population: list[float] = []
    for n, (i, j) in enumerate(sorted(cands)):
        a, b = docs[i], docs[j]
        if by_sub and a.submission == b.submission:
            continue
        if progress and n % 50 == 0:
            progress("compare", n + 1, len(cands), f"{a.rel} <-> {b.rel}")
        p = score_pair(a, b, co, cfg)
        population.append(p.score)
        if p.score >= cfg.threshold * 0.6 or p.moss >= cfg.threshold:
            pairs.append(p)

    med, scale = add_z_scores(pairs, population)
    # Keep a pair if either score clears the threshold, so the list can be
    # ranked by the mosaic score or by the MOSS score alone.
    pairs = [p for p in pairs if p.score >= cfg.threshold or p.moss >= cfg.threshold]
    pairs.sort(key=lambda p: -p.score)
    clusters = cluster(pairs, cfg.cluster_threshold, by_submission=by_sub)
    return Report(
        cfg=cfg,
        docs=docs,
        pairs=pairs,
        clusters=clusters,
        cohort=co,
        base_docs=list(base_docs),
        median=med,
        scale=scale,
        n_compared=len(cands),
        skipped=list(skipped),
        by_submission=by_sub,
    )
