"""Self-contained HTML report, plus a JSON export.

The report is the product, not the score. A number alone cannot be acted
on; what an instructor needs is the passage, the counterpart passage, and
the reason the pair was flagged.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
from collections import defaultdict
from typing import Iterable, Sequence

from .engine import Cluster, Document, Pair, Report
from .structure import TreeMatch
from .winnow import Block

PALETTE = 8  # distinct block colours

_CSS = """
:root{
  --bg:#f7f7f8; --panel:#fff; --ink:#1a1a1c; --muted:#6a6a73; --line:#e3e3e7;
  --accent:#3b5bdb; --warn:#c2410c; --hot:#b91c1c; --ok:#15803d;
  --code:#fbfbfc; --codeink:#24242a;
  --b0:#fde68a; --b1:#bfdbfe; --b2:#bbf7d0; --b3:#fecaca;
  --b4:#ddd6fe; --b5:#fed7aa; --b6:#a5f3fc; --b7:#f5d0fe;
  --astline:#8b5cf6;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#131316; --panel:#1b1b1f; --ink:#ececf1; --muted:#9a9aa6; --line:#2e2e35;
    --accent:#8ea2ff; --warn:#fb923c; --hot:#f87171; --ok:#4ade80;
    --code:#15151a; --codeink:#dcdce4;
    --b0:#6b5a12; --b1:#1e3a5f; --b2:#14502f; --b3:#5f1f1f;
    --b4:#3b2f6b; --b5:#6b3f16; --b6:#164e5a; --b7:#54185a;
  }
}
:root[data-theme="dark"]{
  --bg:#131316; --panel:#1b1b1f; --ink:#ececf1; --muted:#9a9aa6; --line:#2e2e35;
  --accent:#8ea2ff; --warn:#fb923c; --hot:#f87171; --ok:#4ade80;
  --code:#15151a; --codeink:#dcdce4;
  --b0:#6b5a12; --b1:#1e3a5f; --b2:#14502f; --b3:#5f1f1f;
  --b4:#3b2f6b; --b5:#6b3f16; --b6:#164e5a; --b7:#54185a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:1280px;margin:0 auto;padding:32px 16px 96px}
h1{font-size:1.6rem;margin:0 0 4px;letter-spacing:-0.01em}
h2{font-size:1.08rem;margin:40px 0 12px;letter-spacing:-0.01em}
.sub{color:var(--muted);font-size:.9rem;margin:0 0 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card .n{font-size:1.5rem;font-weight:620;font-variant-numeric:tabular-nums;letter-spacing:-0.02em}
.card .l{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:left;font-weight:600;color:var(--muted);font-size:.76rem;
  text-transform:uppercase;letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:middle}
tr:last-child td{border-bottom:0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85em}
.score{font-weight:650;font-variant-numeric:tabular-nums}
.sev-hot{color:var(--hot)} .sev-warn{color:var(--warn)} .sev-low{color:var(--muted)}
.bars{display:flex;gap:3px;align-items:flex-end;height:22px}
.bar{width:9px;background:var(--line);border-radius:2px 2px 0 0;position:relative}
.bar i{position:absolute;bottom:0;left:0;right:0;background:var(--accent);border-radius:2px 2px 0 0;display:block}
.chip{display:inline-block;padding:1px 7px;border-radius:999px;font-size:.72rem;
  border:1px solid var(--line);color:var(--muted);margin:0 4px 4px 0;white-space:nowrap}
.chip.hot{color:var(--hot);border-color:var(--hot)}
.chip.warn{color:var(--warn);border-color:var(--warn)}
.hist{display:flex;gap:2px;align-items:flex-end;height:70px;margin:10px 0 4px}
.hist div{flex:1;background:var(--accent);opacity:.75;border-radius:2px 2px 0 0;min-height:1px}
.hist div.over{background:var(--hot);opacity:.9}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.72rem}
details.pair{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:10px 0}
details.pair>summary{cursor:pointer;padding:12px 14px;display:flex;gap:12px;
  align-items:center;flex-wrap:wrap;list-style:none}
details.pair>summary::-webkit-details-marker{display:none}
details.pair>summary::before{content:"\\25B8";color:var(--muted);transition:transform .15s}
details.pair[open]>summary::before{transform:rotate(90deg)}
.pbody{padding:0 14px 14px}
.sig{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:4px 0 14px}
.sig .s{border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.sig .s b{display:block;font-size:1.05rem;font-variant-numeric:tabular-nums}
.sig .s span{color:var(--muted);font-size:.74rem;text-transform:uppercase;letter-spacing:.04em}
.meter{height:3px;background:var(--line);border-radius:2px;margin-top:6px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--accent)}
.side{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:900px){.side{grid-template-columns:1fr}}
.codebox{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--code)}
.codehead{padding:6px 10px;border-bottom:1px solid var(--line);color:var(--muted);
  font-size:.78rem;display:flex;justify-content:space-between;gap:8px}
pre{margin:0;max-height:62vh;overflow:auto;padding:6px 0;color:var(--codeink)}
pre code{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12.5px;line-height:1.5;white-space:pre}
.ln{display:inline-block;width:3.2em;padding-right:.8em;text-align:right;color:var(--muted);
  user-select:none;opacity:.65}
.row{display:block;padding-left:4px;border-left:3px solid transparent}
.row.m{border-left-color:var(--muted)}
.row.ast{border-left-color:var(--astline)}
.row.hl{outline:1px solid var(--accent);outline-offset:-1px}
.b0{background:var(--b0)}.b1{background:var(--b1)}.b2{background:var(--b2)}.b3{background:var(--b3)}
.b4{background:var(--b4)}.b5{background:var(--b5)}.b6{background:var(--b6)}.b7{background:var(--b7)}
.toggles{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:0 0 10px;font-size:.84rem;color:var(--muted)}
.toggles label{display:flex;gap:5px;align-items:center;cursor:pointer}
.renames{font-size:.82rem;color:var(--muted);margin:0 0 12px}
.renames code{background:var(--code);border:1px solid var(--line);border-radius:4px;padding:1px 5px;margin-right:6px;display:inline-block;margin-bottom:4px}
.cluster{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px}
.cluster .mem{font-size:.85rem}
.note{color:var(--muted);font-size:.82rem}
a{color:var(--accent)}
.sil{display:inline-block;height:.7em;vertical-align:middle;border-radius:3px;
  background:currentColor;opacity:.32;filter:blur(1.6px)}
.private{border:1px solid var(--accent);color:var(--ink);border-radius:10px;padding:10px 14px;
  margin:0 0 18px;font-size:.88rem;background:color-mix(in srgb,var(--accent) 8%,transparent)}
.footer{margin-top:48px;color:var(--muted);font-size:.78rem;border-top:1px solid var(--line);padding-top:14px}
"""

_JS = """
(function(){
  document.addEventListener('mouseover', function(e){
    var el = e.target.closest('[data-blk]');
    if(!el) return;
    var panel = el.closest('.pbody'); if(!panel) return;
    var k = el.getAttribute('data-blk');
    panel.querySelectorAll('[data-blk="'+k+'"]').forEach(function(n){n.classList.add('hl');});
  });
  document.addEventListener('mouseout', function(e){
    var el = e.target.closest('[data-blk]');
    if(!el) return;
    var panel = el.closest('.pbody'); if(!panel) return;
    panel.querySelectorAll('.hl').forEach(function(n){n.classList.remove('hl');});
  });
  document.addEventListener('change', function(e){
    if(e.target.name !== 'layer') return;
    var panel = e.target.closest('.pbody');
    var showFp = panel.querySelector('input[value="fp"]').checked;
    var showAst = panel.querySelector('input[value="ast"]').checked;
    panel.querySelectorAll('.row').forEach(function(r){
      var isAst = r.classList.contains('ast');
      var isFp = r.hasAttribute('data-blk');
      var on = (isFp && showFp) || (isAst && showAst);
      r.style.opacity = (isFp||isAst) ? (on ? '1' : '0.28') : '1';
      r.dataset.dim = on ? '' : '1';
      if(isFp && !showFp) r.classList.add('nofill'); else r.classList.remove('nofill');
    });
    panel.querySelectorAll('.nofill span.b0,.nofill span.b1').forEach(function(){});
  });
})();
"""


# --- line mapping --------------------------------------------------------


def block_lines(doc: Document, lo: int, hi: int) -> tuple[int, int]:
    """Token range [lo, hi) -> 1-based inclusive line range."""
    toks = doc.toks
    if not toks:
        return (1, 1)
    lo = max(0, min(lo, len(toks) - 1))
    hi = max(lo + 1, min(hi, len(toks)))
    return (toks[lo].line, toks[hi - 1].line)


def fp_line_map(doc: Document, blocks: Sequence[Block], side: str) -> dict[int, int]:
    """line -> block index, for colouring."""
    out: dict[int, int] = {}
    for idx, bl in enumerate(blocks):
        lo, hi = (bl.a0, bl.a1) if side == "a" else (bl.b0, bl.b1)
        s, e = block_lines(doc, lo, hi)
        for ln in range(s, e + 1):
            out.setdefault(ln, idx)
    return out


def ast_line_set(matches: Sequence[TreeMatch], side: str) -> set[int]:
    out: set[int] = set()
    for m in matches:
        n = m.a if side == "a" else m.b
        if n.line:
            for ln in range(n.line, max(n.line, n.end_line) + 1):
                out.add(ln)
    return out


# --- rendering helpers ---------------------------------------------------


def sev_class(score: float) -> str:
    return "sev-hot" if score >= 0.6 else ("sev-warn" if score >= 0.4 else "sev-low")


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def render_code(doc: Document, blk: dict[int, int], ast_lines: set[int], max_lines: int) -> str:
    lines = doc.source.splitlines() or [""]
    interesting = set(blk) | ast_lines
    rows: list[str] = []
    shown = 0
    skipping = False
    for i, text in enumerate(lines, start=1):
        near = any((i + d) in interesting for d in range(-3, 4))
        if not near:
            if not skipping:
                rows.append('<span class="row note">%s   ...</span>' % _ln(""))
                skipping = True
            continue
        skipping = False
        if shown >= max_lines:
            rows.append('<span class="row note">%s   ... truncated</span>' % _ln(""))
            break
        shown += 1
        cls = ["row"]
        attrs = ""
        if i in blk:
            cls.append("m")
            cls.append("b%d" % (blk[i] % PALETTE))
            attrs = ' data-blk="%d"' % blk[i]
        elif i in ast_lines:
            cls.append("ast")
        rows.append(
            '<span class="%s"%s>%s%s</span>'
            % (" ".join(cls), attrs, _ln(i), html.escape(text) or " ")
        )
    return "".join(rows)


def _shape(text: str) -> tuple[int, int]:
    """(indent, length) of a line, coarsened so the shape says little."""
    expanded = text.replace("\t", "    ")
    indent = len(expanded) - len(expanded.lstrip(" "))
    body = len(expanded.strip())
    if body == 0:
        return (0, 0)
    return (min(40, (indent // 2) * 2), min(96, ((body + 3) // 4) * 4))


def render_silhouette(doc: Document, blk: dict[int, int], ast_lines: set[int], max_lines: int) -> str:
    """Like render_code, but every line is a blurred bar: no characters of
    the source reach the page, only where the matching regions are."""
    lines = doc.source.splitlines() or [""]
    interesting = set(blk) | ast_lines
    rows: list[str] = []
    shown = 0
    skipping = False
    for i, text in enumerate(lines, start=1):
        near = any((i + d) in interesting for d in range(-3, 4))
        if not near:
            if not skipping:
                rows.append('<span class="row note">%s   ...</span>' % _ln(""))
                skipping = True
            continue
        skipping = False
        if shown >= max_lines:
            rows.append('<span class="row note">%s   ... truncated</span>' % _ln(""))
            break
        shown += 1
        cls = ["row"]
        attrs = ""
        if i in blk:
            cls.append("m")
            cls.append("b%d" % (blk[i] % PALETTE))
            attrs = ' data-blk="%d"' % blk[i]
        elif i in ast_lines:
            cls.append("ast")
        indent, width = _shape(text)
        bar = (
            '<span class="sil" style="margin-left:%dch;width:%dch"></span>' % (indent, width)
            if width
            else " "
        )
        rows.append('<span class="%s"%s>%s%s</span>' % (" ".join(cls), attrs, _ln(i), bar))
    return "".join(rows)


def _ln(i) -> str:
    return '<span class="ln">%s</span>' % (i if i != "" else "")


def _bars(p: Pair) -> str:
    vals = [("fingerprint", p.fp), ("structure", p.ast), ("rename", p.rename.score),
            ("compression", p.ncd), ("profile", p.prof)]
    out = ['<span class="bars" title="%s">' % "  ".join("%s %s" % (n, _pct(v)) for n, v in vals)]
    for _, v in vals:
        out.append('<span class="bar" style="height:22px"><i style="height:%d%%"></i></span>' % round(100 * max(0.03, v)))
    out.append("</span>")
    return "".join(out)


def _histogram(pairs: Sequence[Pair], threshold: float, bins: int = 28) -> str:
    if not pairs:
        return '<p class="note">No pairs scored.</p>'
    counts = [0] * bins
    for p in pairs:
        b = min(bins - 1, int(p.score * bins))
        counts[b] += 1
    top = max(counts) or 1
    cells = []
    for i, c in enumerate(counts):
        over = (i + 0.5) / bins >= threshold
        h = round(100 * c / top)
        cells.append(
            '<div class="%s" style="height:%d%%" title="%.2f-%.2f: %d pairs"></div>'
            % ("over" if over else "", max(1, h), i / bins, (i + 1) / bins, c)
        )
    return (
        '<div class="hist">%s</div><div class="axis"><span>0.0</span>'
        '<span>similarity score</span><span>1.0</span></div>' % "".join(cells)
    )


def _signal_grid(p: Pair) -> str:
    items = [
        ("Fused score", p.score, "weighted, agreement-boosted"),
        ("Fingerprint", p.fp, "MOSS-style token overlap"),
        ("Structure", p.ast, "normalized tree match"),
        ("Rename map", p.rename.score, "1:1 identifier correspondence"),
        ("Compression", p.ncd, "1 - NCD"),
        ("Profile", p.prof, "construct histogram"),
    ]
    out = ['<div class="sig">']
    for label, val, note in items:
        out.append(
            '<div class="s"><span>%s</span><b>%s</b><div class="meter">'
            '<i style="width:%d%%"></i></div><span class="note">%s</span></div>'
            % (html.escape(label), _pct(val), round(100 * val), html.escape(note))
        )
    out.append("</div>")
    return "".join(out)


def _rename_table(p: Pair, redact: bool = False) -> str:
    ev = p.rename
    if ev.distinct < 3:
        return ""
    if redact:
        return (
            '<div class="renames"><b>Identifier correspondence</b> &mdash; %s'
            ' <span class="note">(names hidden in this private room)</span></div>'
            % html.escape(ev.summary)
        )
    cells = "".join(
        "<code>%s &rarr; %s</code>" % (html.escape(a), html.escape(b))
        for a, b, _ in ev.examples
        if a != b
    )
    if not cells:
        cells = '<span class="note">identifiers unchanged</span>'
    return (
        '<div class="renames"><b>Identifier correspondence</b> &mdash; %s<br>%s</div>'
        % (html.escape(ev.summary), cells)
    )


def _pair_detail(p: Pair, cfg_lines: int, redact: bool = False) -> str:
    blk_a = fp_line_map(p.a, p.blocks, "a")
    blk_b = fp_line_map(p.b, p.blocks, "b")
    ast_a = ast_line_set(p.tree_matches, "a")
    ast_b = ast_line_set(p.tree_matches, "b")
    chips = "".join(
        '<span class="chip %s">%s</span>'
        % ("hot" if p.score >= 0.6 else "warn", html.escape(r))
        for r in p.reasons
    )
    summary = (
        '<summary><span class="score %s">%s</span>'
        '<span class="mono">%s</span><span class="note">vs</span>'
        '<span class="mono">%s</span>%s<span style="margin-left:auto">%s</span></summary>'
        % (
            sev_class(p.score),
            _pct(p.score),
            html.escape(p.a.rel),
            html.escape(p.b.rel),
            ('<span class="chip">z=%.1f</span>' % p.z) if p.z else "",
            _bars(p),
        )
    )
    toggles = (
        '<div class="toggles"><label><input type="checkbox" name="layer" value="fp" checked> '
        'token passages (%d)</label><label><input type="checkbox" name="layer" value="ast" checked> '
        'structural matches (%d)</label><span class="note">hover a highlighted passage to find its counterpart</span></div>'
        % (len(p.blocks), len(p.tree_matches))
    )
    body = (
        '<div class="pbody">%s%s%s%s<div class="side">%s%s</div></div>'
        % (
            _signal_grid(p),
            '<div style="margin:-6px 0 12px">%s</div>' % chips if chips else "",
            _rename_table(p, redact),
            toggles,
            _codebox(p.a, blk_a, ast_a, cfg_lines, redact),
            _codebox(p.b, blk_b, ast_b, cfg_lines, redact),
        )
    )
    return '<details class="pair">%s%s</details>' % (summary, body)


def _codebox(doc: Document, blk: dict[int, int], ast_lines: set[int], max_lines: int,
             redact: bool = False) -> str:
    render = render_silhouette if redact else render_code
    return (
        '<div class="codebox"><div class="codehead"><span class="mono">%s</span>'
        '<span>%d lines &middot; %d tokens</span></div><pre><code>%s</code></pre></div>'
        % (
            html.escape(doc.rel),
            doc.n_lines,
            doc.n_tokens,
            render(doc, blk, ast_lines, max_lines),
        )
    )


def _clusters(clusters: Sequence[Cluster]) -> str:
    if not clusters:
        return '<p class="note">No transitive groups above the cluster threshold.</p>'
    out = []
    for c in clusters:
        out.append(
            '<div class="cluster"><b>%d submissions</b> &middot; '
            '<span class="note">%d internal pairs, mean %s</span><div class="mem mono">%s</div></div>'
            % (
                len(c.members),
                len(c.pairs),
                _pct(c.mean_score),
                html.escape(", ".join(c.members)),
            )
        )
    return "".join(out)


def _submission_table(rep: Report) -> str:
    """Aggregate file pairs up to submission pairs."""
    agg: dict[tuple[str, str], list[Pair]] = defaultdict(list)
    for p in rep.pairs:
        key = tuple(sorted((p.a.submission, p.b.submission)))
        agg[key].append(p)
    rows = []
    for (a, b), ps in sorted(agg.items(), key=lambda kv: -max(p.score for p in kv[1])):
        best = max(ps, key=lambda p: p.score)
        rows.append(
            "<tr><td class='mono'>%s</td><td class='mono'>%s</td>"
            "<td class='score %s'>%s</td><td>%s</td><td class='note'>%s</td></tr>"
            % (
                html.escape(a),
                html.escape(b),
                sev_class(best.score),
                _pct(best.score),
                _bars(best),
                html.escape("%d file pair%s" % (len(ps), "" if len(ps) == 1 else "s")),
            )
        )
    if not rows:
        return '<p class="note">Nothing above threshold.</p>'
    return (
        "<div class='panel'><table><thead><tr><th>Submission</th><th>Submission</th>"
        "<th>Top score</th><th>Signals</th><th></th></tr></thead><tbody>%s</tbody></table></div>"
        % "".join(rows)
    )


def build_html(
    rep: Report,
    *,
    detail: int = 60,
    max_lines: int = 400,
    title: str = "Similarity report",
    redact: bool = False,
) -> str:
    """Render the report. With redact=True no source text is included at all:
    code panes show blurred line shapes with the matching regions marked."""
    cfg = rep.cfg
    n_sub = len({d.submission for d in rep.docs})
    flagged = [p for p in rep.pairs if p.score >= 0.5]
    cards = [
        (len(rep.docs), "files"),
        (n_sub, "submissions"),
        (f"{rep.n_compared:,}", "pairs examined"),
        (len(rep.pairs), "above threshold"),
        (len(flagged), "score &ge; 50%"),
        (len(rep.clusters), "clusters"),
    ]
    card_html = "".join(
        '<div class="card"><div class="n">%s</div><div class="l">%s</div></div>' % (n, l)
        for n, l in cards
    )
    details = "".join(_pair_detail(p, max_lines, redact) for p in rep.pairs[:detail])
    private_note = (
        '<div class="private"><b>Private room.</b> Code is hidden from everyone, including the host. '
        "Each pane shows the shape of a file; coloured bars mark the passages that match "
        "the other submission.</div>"
        if redact
        else ""
    )
    more = (
        '<p class="note">%d further pairs are listed above the threshold but not expanded here '
        "(raise --detail to include them).</p>" % (len(rep.pairs) - detail)
        if len(rep.pairs) > detail
        else ""
    )
    suppressed = (
        "%d shared fingerprints and %d shared subtrees were treated as provided material"
        % (rep.cohort.suppressed_fp, rep.cohort.suppressed_tree)
        if (rep.cohort.suppressed_fp or rep.cohort.suppressed_tree)
        else "no automatic boilerplate suppression (cohort too small)"
    )
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
<div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">{len(rep.docs)} files in {n_sub} submissions &middot; k={cfg.k}, w={cfg.w},
 min subtree {cfg.min_subtree} &middot; threshold {cfg.threshold:.2f} &middot; {stamp}</p>
{private_note}

<div class="cards">{card_html}</div>
<div class="panel" style="margin-top:10px">
  {_histogram(rep.pairs, cfg.threshold)}
  <p class="note" style="margin:8px 0 0">Scores are judged against this cohort:
  median {rep.median:.3f}, robust scale {rep.scale:.3f}. A z-score above 3 means the pair
  stands far outside the normal level of similarity for this assignment.
  {html.escape(suppressed)}.</p>
</div>

<h2>Submission pairs</h2>
{_submission_table(rep)}

<h2>Clusters</h2>
<div class="panel">{_clusters(rep.clusters)}</div>

<h2>Evidence</h2>
<p class="note">Left border: grey = shared token passage (MOSS-style), purple = identical
normalized structure. Background colour pairs a passage with its counterpart.</p>
{details}
{more}

<div class="footer">Generated by mosaic. Similarity is evidence, not a verdict &mdash;
every flagged pair needs a human to read it.</div>
</div>
<script>{_JS}</script>
"""


def build_json(rep: Report, *, redact: bool = False) -> str:
    """Machine-readable results. With redact=True identifier names are left
    out; line ranges and scores remain."""
    out = {
        "redacted": redact,
        "config": {
            "k": rep.cfg.k,
            "w": rep.cfg.w,
            "min_subtree": rep.cfg.min_subtree,
            "threshold": rep.cfg.threshold,
            "max_df": rep.cfg.max_df,
        },
        "cohort": {
            "files": len(rep.docs),
            "submissions": sorted({d.submission for d in rep.docs}),
            "pairs_examined": rep.n_compared,
            "median_score": rep.median,
            "robust_scale": rep.scale,
            "suppressed_fingerprints": rep.cohort.suppressed_fp,
            "suppressed_subtrees": rep.cohort.suppressed_tree,
            "skipped": rep.skipped,
        },
        "pairs": [
            {
                "a": p.a.rel,
                "b": p.b.rel,
                "submissions": [p.a.submission, p.b.submission],
                "score": round(p.score, 4),
                "z": round(p.z, 2),
                "signals": {
                    "fingerprint": {"a": round(p.fp_a, 4), "b": round(p.fp_b, 4)},
                    "structure": {"a": round(p.ast_a, 4), "b": round(p.ast_b, 4)},
                    "rename": round(p.rename.score, 4),
                    "compression": round(p.ncd, 4),
                    "profile": round(p.prof, 4),
                },
                "rename_map": []
                if redact
                else [{"from": a, "to": b, "count": c} for a, b, c in p.rename.examples],
                "renamed_identifiers": p.rename.distinct,
                "reasons": p.reasons,
                "token_blocks": [
                    {
                        "a_lines": list(block_lines(p.a, bl.a0, bl.a1)),
                        "b_lines": list(block_lines(p.b, bl.b0, bl.b1)),
                        "tokens": bl.len_a,
                    }
                    for bl in p.blocks
                ],
                "structural_matches": [
                    {
                        "a_lines": [m.a.line, m.a.end_line],
                        "b_lines": [m.b.line, m.b.end_line],
                        "nodes": m.size,
                    }
                    for m in p.tree_matches[:50]
                ],
            }
            for p in rep.pairs
        ],
        "clusters": [
            {"members": c.members, "mean_score": round(c.mean_score, 4)} for c in rep.clusters
        ],
    }
    return json.dumps(out, indent=2)


def write_html(rep: Report, path: str, **kw) -> str:
    text = build_html(rep, **kw)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path
