"""Command line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser

from . import report as report_mod
from .engine import Config, run
from .tokens import LANGS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mosaic",
        description=(
            "Source-code similarity detection. Combines MOSS-style winnowing "
            "fingerprints with normalized-structure matching, consistent-rename "
            "detection, compression distance and cohort statistics. "
            "Run `mosaic serve` to collect submissions from peers through a "
            "memory-only web session."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("paths", nargs="+", help="submission directories or files")
    p.add_argument("-o", "--out", default="mosaic-report.html", help="HTML report path")
    p.add_argument("--json", dest="json_out", default=None, help="also write JSON results here")
    p.add_argument("--open", action="store_true", help="open the report when finished")

    g = p.add_argument_group("corpus")
    g.add_argument("--lang", choices=sorted(LANGS), default=None,
                   help="restrict to one language (default: detect per file)")
    g.add_argument("--base", action="append", default=[],
                   help="provided/skeleton code to ignore (repeatable)")
    g.add_argument("--by-dir", dest="by_dir", action="store_true", default=None,
                   help="treat each top-level directory as one submission")
    g.add_argument("--by-file", dest="by_dir", action="store_false",
                   help="treat every file as its own submission")
    g.add_argument("--min-tokens", type=int, default=40, help="skip files smaller than this")

    g = p.add_argument_group("sensitivity")
    g.add_argument("-k", type=int, default=14, help="k-gram length in tokens (noise threshold)")
    g.add_argument("-w", type=int, default=12, help="winnowing window")
    g.add_argument("--min-subtree", type=int, default=6,
                   help="smallest structural subtree counted as a match (nodes)")
    g.add_argument("--max-gap", type=int, default=48,
                   help="token gap tolerated inside one matched passage")
    g.add_argument("-t", "--threshold", type=float, default=0.25,
                   help="report pairs whose mosaic or MOSS score reaches this")
    g.add_argument("--cluster-threshold", type=float, default=0.45,
                   help="edge score for transitive grouping")
    g.add_argument("--max-df", type=float, default=0.30,
                   help="suppress material shared by more than this share of submissions")

    g = p.add_argument_group("report")
    g.add_argument("--detail", type=int, default=60, help="pairs to expand with side-by-side code")
    g.add_argument("--max-lines", type=int, default=400, help="code lines per pane")
    g.add_argument("--top", type=int, default=20, help="pairs to print to the terminal")
    g.add_argument("--rank-by", choices=("mosaic", "moss"), default="mosaic",
                   help="order pairs by the mosaic score or by the MOSS score")
    g.add_argument("--title", default="Similarity report")
    g.add_argument("-q", "--quiet", action="store_true")
    return p


def _progress(quiet: bool):
    state = {"last": 0.0}

    def cb(phase: str, i: int, n: int, what: str) -> None:
        if quiet:
            return
        now = time.time()
        if now - state["last"] < 0.15 and i != n:
            return
        state["last"] = now
        bar = f"\r  {phase:8s} {i}/{n} "
        sys.stderr.write(bar + what[-52:].ljust(52))
        sys.stderr.flush()
        if i == n:
            sys.stderr.write("\n")

    return cb


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        from .web import main as serve_main

        return serve_main(argv[1:])
    args = build_parser().parse_args(argv)

    for path in args.paths:
        if not os.path.exists(path):
            sys.stderr.write(f"mosaic: no such path: {path}\n")
            return 2

    cfg = Config(
        k=args.k,
        w=args.w,
        min_subtree=args.min_subtree,
        max_df=args.max_df,
        min_tokens=args.min_tokens,
        max_gap=args.max_gap,
        threshold=args.threshold,
        cluster_threshold=args.cluster_threshold,
        by_dir=args.by_dir,
        lang=args.lang,
    )

    t0 = time.time()
    rep = run(args.paths, cfg, args.base, progress=_progress(args.quiet))
    elapsed = time.time() - t0

    if not rep.docs:
        sys.stderr.write("mosaic: no source files found (check --lang / --min-tokens)\n")
        return 1

    report_mod.write_html(
        rep, args.out, detail=args.detail, max_lines=args.max_lines, title=args.title,
        rank_by=args.rank_by,
    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(report_mod.build_json(rep))

    if not args.quiet:
        _print_summary(rep, args.top, elapsed, args.out, args.json_out, args.rank_by)
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))
    return 0


def _print_summary(rep, top: int, elapsed: float, out: str, json_out: str | None,
                   rank_by: str = "mosaic") -> None:
    n_sub = len({d.submission for d in rep.docs})
    print()
    print(f"  {len(rep.docs)} files, {n_sub} submissions, "
          f"{rep.n_compared:,} candidate pairs examined in {elapsed:.1f}s")
    if rep.cohort.suppressed_fp or rep.cohort.suppressed_tree:
        print(f"  suppressed as provided material: {rep.cohort.suppressed_fp} fingerprints, "
              f"{rep.cohort.suppressed_tree} subtrees")
    if rep.skipped:
        print(f"  skipped {len(rep.skipped)} file(s) as too small or unreadable")
    print()
    if not rep.pairs:
        print("  nothing above threshold")
    else:
        ranked = sorted(rep.pairs, key=lambda p: -p.moss) if rank_by == "moss" else rep.pairs
        print(f"  ranked by {'MOSS' if rank_by == 'moss' else 'mosaic'} score")
        print(f"  {'mosaic':>6} {'moss':>5} {'z':>5} {'str':>5}  pair")
        print(f"  {'-'*6} {'-'*5} {'-'*5} {'-'*5}  {'-'*48}")
        for p in ranked[:top]:
            z = f"{p.z:5.1f}" if p.z else "    -"
            print(f"  {p.score*100:5.0f}% {p.moss*100:4.0f}% {z} {p.ast*100:4.0f}%  "
                  f"{p.a.rel} <-> {p.b.rel}")
            if p.reasons:
                print(f"         {'; '.join(p.reasons[:2])}")
        if len(rep.pairs) > top:
            print(f"  ... and {len(rep.pairs) - top} more")
    if rep.clusters:
        print()
        for c in rep.clusters:
            print(f"  cluster of {len(c.members)} (mean {c.mean_score*100:.0f}%): "
                  f"{', '.join(c.members)}")
    print()
    print(f"  report: {out}")
    if json_out:
        print(f"  json:   {json_out}")


if __name__ == "__main__":
    raise SystemExit(main())
