"""Test suite. Run with: python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mosaic import cast, engine, pyast, report, structure, tokens  # noqa: E402
from mosaic.winnow import fingerprint, kgram_hashes, winnow  # noqa: E402

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")


def c_tree(src: str):
    return structure.prepare(cast.build(tokens.tokenize(src, "c")))


def py_tree(src: str):
    return structure.prepare(pyast.build(src))


def norm(src: str, lang: str):
    return tokens.normalize(tokens.tokenize(src, lang), lang)


class TestTokenizer(unittest.TestCase):
    def test_comments_and_strings_dropped(self):
        src = '// gone\nint x = 1; /* also gone */\n#define Y 2\nchar *s = "text";'
        vals = [t.value for t in tokens.tokenize(src, "c")]
        self.assertNotIn("gone", " ".join(vals))
        self.assertNotIn("define", vals)
        self.assertIn("int", vals)
        self.assertIn('"text"', vals)

    def test_python_triple_quoted_string(self):
        src = 'def f():\n    """doc\n    lines\n    """\n    return 1\n'
        kinds = [(t.kind, t.value) for t in tokens.tokenize(src, "python")]
        self.assertEqual(sum(1 for k, _ in kinds if k == tokens.STRING), 1)
        self.assertIn(("KEYWORD", "return"), kinds)

    def test_line_numbers(self):
        src = "a = 1\nb = 2\nc = 3\n"
        toks = tokens.tokenize(src, "python")
        self.assertEqual(toks[0].line, 1)
        self.assertEqual([t.line for t in toks if t.value == "c"], [3])

    def test_normalization_is_rename_invariant(self):
        a = norm("def f(values):\n    return len(values) + 1\n", "python")
        b = norm("def g(items):\n    return len(items) + 1\n", "python")
        self.assertEqual(a, b)

    def test_library_names_survive_only_in_call_position(self):
        # `values` as a variable must collapse; len(...) as a call must not
        a = norm("x = values\n", "python")
        self.assertEqual(a, ["V", "=", "V"])
        b = norm("x = len(y)\n", "python")
        self.assertIn("len", b)


class TestWinnowing(unittest.TestCase):
    def test_guarantee_property(self):
        """Any shared passage of >= k + w - 1 tokens must share a fingerprint."""
        import random

        rnd = random.Random(7)
        alphabet = [f"t{i}" for i in range(20)]
        k, w = 5, 4
        shared = [rnd.choice(alphabet) for _ in range(k + w - 1)]
        for _ in range(30):
            a = [rnd.choice(alphabet) for _ in range(20)] + shared + [rnd.choice(alphabet) for _ in range(20)]
            b = [rnd.choice(alphabet) for _ in range(13)] + shared + [rnd.choice(alphabet) for _ in range(9)]
            fa = fingerprint(a, k, w)
            fb = fingerprint(b, k, w)
            self.assertTrue(fa.hashes & fb.hashes, "winnowing guarantee violated")

    def test_window_selection_density(self):
        hashes = kgram_hashes([f"x{i % 7}" for i in range(400)], 5)
        sel = winnow(hashes, 8)
        # density is bounded by one pick per window
        self.assertLessEqual(len(sel), len(hashes))
        self.assertGreater(len(sel), len(hashes) // 16)

    def test_short_input(self):
        self.assertEqual(kgram_hashes(["a", "b"], 5), [])
        self.assertEqual(len(fingerprint(["a", "b"], 5, 4).index), 0)


class TestPythonStructure(unittest.TestCase):
    def test_renaming_and_reordering_give_identical_tree(self):
        a = py_tree(
            "def f(xs):\n    t = 0\n    for x in xs:\n        t += x\n    return t\n"
            "def g(n):\n    return n * 2\n"
        )
        b = py_tree(
            "def second(k):\n    return k * 2\n"
            "def first(items):\n    acc = 0\n    for item in items:\n        acc += item\n    return acc\n"
        )
        self.assertEqual(a.hash, b.hash)

    def test_augmented_assignment_matches_explicit(self):
        a = py_tree("def f(x):\n    x += 1\n    return x\n")
        b = py_tree("def f(x):\n    x = x + 1\n    return x\n")
        self.assertEqual(a.hash, b.hash)

    def test_comparison_direction_canonicalized(self):
        a = py_tree("def f(a, b):\n    return a > b\n")
        b = py_tree("def f(a, b):\n    return b < a\n")
        self.assertEqual(a.hash, b.hash)

    def test_docstrings_and_comments_ignored(self):
        a = py_tree('def f():\n    """explain"""\n    return 1\n')
        b = py_tree("def f():\n    # explain\n    return 1\n")
        self.assertEqual(a.hash, b.hash)

    def test_counted_while_matches_for_loop(self):
        a = py_tree(
            "def f(rows):\n"
            "    out = []\n"
            "    for item in rows:\n"
            "        out.append(item.strip())\n"
            "    return out\n"
        )
        b = py_tree(
            "def g(lines):\n"
            "    res = []\n"
            "    i = 0\n"
            "    while i < len(lines):\n"
            "        e = lines[i]\n"
            "        res.append(e.strip())\n"
            "        i = i + 1\n"
            "    return res\n"
        )
        self.assertEqual(a.hash, b.hash)

    def test_counted_while_with_increment_first(self):
        a = py_tree(
            "def f(rows):\n"
            "    n = 0\n"
            "    for item in rows:\n"
            "        n += len(item)\n"
            "    return n\n"
        )
        b = py_tree(
            "def g(rows):\n"
            "    n = 0\n"
            "    i = 0\n"
            "    while i < len(rows):\n"
            "        item = rows[i]\n"
            "        i = i + 1\n"
            "        n += len(item)\n"
            "    return n\n"
        )
        self.assertEqual(a.hash, b.hash)

    def test_uncounted_while_is_not_rewritten(self):
        """A while that is not an index loop must keep its own shape."""
        a = py_tree("def f(rows):\n    for item in rows:\n        print(item)\n")
        b = py_tree("def g(rows):\n    while rows:\n        print(rows.pop())\n")
        self.assertNotEqual(a.hash, b.hash)

    def test_different_algorithms_differ(self):
        a = py_tree("def f(xs):\n    t = 0\n    for x in xs:\n        t += x\n    return t\n")
        b = py_tree("def f(xs):\n    return sum(xs)\n")
        self.assertNotEqual(a.hash, b.hash)

    def test_unparseable_file_returns_none(self):
        self.assertIsNone(pyast.build("def f(:\n  pass"))


class TestCStructure(unittest.TestCase):
    def test_for_and_while_are_equivalent(self):
        a = c_tree("void f(int n){ int s = 0; for (int i = 0; i < n; i++) { s += i; } }")
        b = c_tree("void g(int m){ int acc = 0; int j = 0; while (j < m) { acc = acc + j; j = j + 1; } }")
        self.assertEqual(a.hash, b.hash)

    def test_switch_matches_if_else_chain(self):
        a = c_tree('void f(int c){ switch (c) { case 1: puts("a"); break; case 2: puts("b"); break; default: puts("c"); } }')
        b = c_tree('void g(int k){ if (k == 1) { puts("a"); } else if (k == 2) { puts("b"); } else { puts("c"); } }')
        self.assertEqual(a.hash, b.hash)

    def test_declaration_types_are_stripped(self):
        a = c_tree("void f(){ int x = 5; x = x + 1; }")
        b = c_tree("void f(){ long long y = 5; y = y + 1; }")
        self.assertEqual(a.hash, b.hash)

    def test_postfix_and_prefix_increment_match_explicit(self):
        a = c_tree("void f(int x){ x++; }")
        b = c_tree("void f(int x){ x = x + 1; }")
        c = c_tree("void f(int x){ ++x; }")
        self.assertEqual(a.hash, b.hash)
        self.assertEqual(a.hash, c.hash)

    def test_comparison_flip(self):
        a = c_tree("int f(int a, int b){ return a > b; }")
        b = c_tree("int f(int a, int b){ return b < a; }")
        self.assertEqual(a.hash, b.hash)

    def test_different_programs_differ(self):
        a = c_tree("int f(int n){ int s=0; for(int i=0;i<n;i++){ s+=i; } return s; }")
        b = c_tree("int f(int n){ return n * (n - 1) / 2; }")
        self.assertNotEqual(a.hash, b.hash)

    def test_parser_survives_broken_input(self):
        # must not raise or hang
        for junk in ("int main(void) { if (", "}}}{{{", "void f(){ for(;;) ", "#include <x>\n int"):
            self.assertIsNotNone(cast.build(tokens.tokenize(junk, "c")))


class TestSignals(unittest.TestCase):
    def test_rename_evidence_finds_bijection(self):
        from mosaic.signals import rename_evidence
        from mosaic.winnow import chain_blocks, shared_pairs

        sa = "def f(alpha, beta):\n    gamma = alpha + beta\n    delta = gamma * alpha\n    return delta - beta\n" * 3
        sb = sa.replace("alpha", "aa").replace("beta", "bb").replace("gamma", "cc").replace("delta", "dd")
        ta, tb = tokens.tokenize(sa, "python"), tokens.tokenize(sb, "python")
        na, nb = tokens.normalize(ta, "python"), tokens.normalize(tb, "python")
        fa, fb = fingerprint(na, 8, 6), fingerprint(nb, 8, 6)
        blocks = chain_blocks(shared_pairs(fa, fb), 8, 48)
        ev = rename_evidence(ta, tb, na, nb, blocks)
        self.assertGreaterEqual(ev.distinct, 4)
        self.assertGreater(ev.consistency, 0.9)
        self.assertGreater(ev.injective, 0.9)
        self.assertGreater(ev.renamed, 0.5)

    def test_ncd_bounds(self):
        from mosaic.signals import ncd_similarity

        blob = b"abcdefghij" * 40
        self.assertGreater(ncd_similarity(blob, blob), 0.85)
        self.assertLess(ncd_similarity(blob, os.urandom(400)), 0.5)


class TestEngineOnCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = engine.run([CORPUS], engine.Config(threshold=0.0))
        cls.by_key = {
            tuple(sorted((p.a.submission, p.b.submission))): p for p in cls.rep.pairs
        }

    def get(self, a, b):
        return self.by_key[tuple(sorted((a, b)))]

    def test_all_corpus_files_loaded(self):
        self.assertEqual(len(self.rep.docs), 9)
        self.assertTrue(all(d.tidx is not None for d in self.rep.docs))

    def test_renamed_copy_is_detected(self):
        p = self.get("s01_original", "s02_renamed")
        self.assertGreater(p.score, 0.9)
        self.assertGreater(p.ast, 0.95)

    def test_structural_disguise_beats_token_matching(self):
        """The headline claim: structure catches what fingerprints miss."""
        p = self.get("c01_original", "c02_disguised")
        self.assertLess(p.fp, 0.60, "token overlap should be modest after disguise")
        self.assertGreater(p.ast, 0.85, "structure should still match")
        self.assertGreater(p.score, 0.60)

    def test_refactored_copy_is_detected(self):
        p = self.get("s01_original", "s03_refactored")
        self.assertGreater(p.score, 0.5)

    def test_independent_work_scores_low(self):
        plagiarised = {
            tuple(sorted(x))
            for x in [
                ("s01_original", "s02_renamed"),
                ("s01_original", "s03_refactored"),
                ("s02_renamed", "s03_refactored"),
                ("c01_original", "c02_disguised"),
            ]
        }
        worst_clean = max(
            (p.score for k, p in self.by_key.items() if k not in plagiarised), default=0.0
        )
        best_dirty = min(self.by_key[k].score for k in plagiarised)
        self.assertLess(worst_clean, 0.4, "an independent pair scored too high")
        self.assertGreater(best_dirty, worst_clean + 0.15, "separation is too narrow")

    def test_cluster_finds_the_ring(self):
        rep = engine.run([CORPUS], engine.Config())
        members = {m for c in rep.clusters for m in c.members}
        self.assertTrue({"s01_original", "s02_renamed", "s03_refactored"} <= members)

    def test_languages_are_not_cross_compared(self):
        for p in self.rep.pairs:
            self.assertEqual(p.a.lang, p.b.lang)

    def test_base_file_suppression(self):
        base = os.path.join(CORPUS, "s01_original")
        rep = engine.run([CORPUS], engine.Config(threshold=0.0), [base])
        pair = {
            tuple(sorted((p.a.submission, p.b.submission))): p for p in rep.pairs
        }.get(("s01_original", "s02_renamed"))
        before = self.get("s01_original", "s02_renamed").score
        self.assertTrue(pair is None or pair.score < before)


class TestCohortStatistics(unittest.TestCase):
    def test_z_uses_the_full_scored_population(self):
        from mosaic.engine import Pair, add_z_scores

        docs = engine.run([CORPUS], engine.Config(threshold=0.0)).docs
        pairs = [Pair(docs[0], docs[1])]
        pairs[0].score = 0.9
        # a wide population must give a smaller z than a narrow one
        wide = add_z_scores(pairs, [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.9])
        z_wide = pairs[0].z
        add_z_scores(pairs, [0.44, 0.45, 0.46, 0.45, 0.44, 0.46, 0.45, 0.45, 0.9])
        self.assertLess(z_wide, pairs[0].z)
        self.assertGreater(wide[1], 0.0)

    def test_small_cohort_gets_no_z(self):
        from mosaic.engine import Pair, add_z_scores

        docs = engine.run([CORPUS], engine.Config(threshold=0.0)).docs
        p = Pair(docs[0], docs[1])
        self.assertEqual(add_z_scores([p], [0.1, 0.2]), (0.0, 0.0))
        self.assertEqual(p.z, 0.0)

    def test_boilerplate_is_suppressed_in_a_large_cohort(self):
        """Shared skeleton code must stop counting as evidence."""
        import tempfile

        skeleton = (
            "import sys\n\n\n"
            "def parse_args(argv):\n"
            "    if len(argv) < 2:\n"
            "        print('usage')\n"
            "        raise SystemExit(2)\n"
            "    return argv[1]\n\n\n"
            "def load(path):\n"
            "    with open(path) as fh:\n"
            "        return fh.readlines()\n\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(14):
                d = os.path.join(tmp, f"s{i:02d}")
                os.makedirs(d)
                body = f"\n\ndef work_{i}(rows):\n    return [r * {i + 2} for r in rows]\n"
                with open(os.path.join(d, "a.py"), "w", encoding="utf-8") as fh:
                    fh.write(skeleton + body)
            rep = engine.run([tmp], engine.Config(threshold=0.0))
            self.assertGreater(rep.cohort.suppressed_fp + rep.cohort.suppressed_tree, 0)
            if rep.pairs:
                self.assertLess(max(p.fp for p in rep.pairs), 0.5)

    def test_copied_pair_keeps_its_score_as_the_cohort_grows(self):
        """Recognising the skeleton (from 10 submissions on) must not make a
        full copy look less copied: scores are measured on each file's own
        code, so new arrivals do not pull existing pairs down."""
        import random

        from mosaic.engine import run_sources

        skeleton = (
            "import sys\n\n\n"
            "def parse_args(argv):\n"
            "    if len(argv) < 2:\n"
            "        print('usage: prog FILE')\n"
            "        raise SystemExit(2)\n"
            "    return argv[1]\n\n\n"
            "def load(path):\n"
            "    with open(path) as fh:\n"
            "        return [line.strip() for line in fh if line.strip()]\n\n\n"
        )
        copied = (
            "def solve(rows):\n"
            "    best, seen = 0, {}\n"
            "    for i, r in enumerate(rows):\n"
            "        key = len(r) % 7\n"
            "        if key in seen and i - seen[key] > best:\n"
            "            best = i - seen[key]\n"
            "        seen.setdefault(key, i)\n"
            "    while best > 100:\n"
            "        best //= 3\n"
            "    return best\n\n\n"
            "def report(rows, best):\n"
            "    width = max((len(r) for r in rows), default=0)\n"
            "    lines = []\n"
            "    for i, r in enumerate(rows[:best]):\n"
            "        if i % 2:\n"
            "            lines.append(r.rjust(width, '.'))\n"
            "        else:\n"
            "            lines.append(r.upper()[: width // 2])\n"
            "    return '\\n'.join(lines) or 'empty'\n"
        )

        def expr(r, depth):
            if depth == 0:
                return r.choice(["x", "y", "z", str(r.randrange(2, 50))])
            op = r.choice(["+", "-", "*", "//", "%", "^", "|", "&", "<<"])
            if r.random() < 0.25:
                return f"{r.choice(['abs', 'min', 'max'])}({expr(r, depth - 1)}, {expr(r, depth - 1)})" \
                    if r.random() < 0.5 else f"(-{expr(r, depth - 1)})"
            return f"({expr(r, depth - 1)} {op} {expr(r, depth - 1)})"

        def own(i):
            r = random.Random(1000 + i)
            return "".join(f"def f{j}(x, y, z):\n    return {expr(r, 4)}\n\n\n" for j in range(3))

        def copy_scores(n):
            subs = [("alice", skeleton + copied), ("bob", skeleton + copied.replace("best", "longest"))]
            subs += [(f"s{i:02d}", skeleton + own(i)) for i in range(n - 2)]
            rep = run_sources([(s, "a.py", c.encode()) for s, c in subs], engine.Config(by_dir=True))
            p = next(p for p in rep.pairs if {p.a.submission, p.b.submission} == {"alice", "bob"})
            return p.score, p.moss, rep.cohort.suppressed_fp

        small, large = copy_scores(5), copy_scores(16)
        self.assertEqual(small[2], 0)  # no suppression yet ...
        self.assertGreater(large[2], 0)  # ... then the skeleton is recognised
        self.assertGreaterEqual(large[0], small[0] - 0.02)
        self.assertGreaterEqual(large[1], small[1] - 0.05)
        self.assertGreater(large[1], 0.85)


class TestReportOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = engine.run([CORPUS], engine.Config())

    def test_html_is_self_contained(self):
        html = report.build_html(self.rep)
        self.assertIn("<!doctype html>", html)
        self.assertNotIn("http://", html.split("<style>")[0])
        self.assertEqual(html.count("<details"), html.count("</details>"))
        self.assertIn("s01_original/stats.py", html)

    def test_json_round_trips(self):
        data = json.loads(report.build_json(self.rep))
        self.assertIn("pairs", data)
        self.assertTrue(data["pairs"])
        top = data["pairs"][0]
        self.assertIn("signals", top)
        self.assertIn("token_blocks", top)
        for blk in top["token_blocks"]:
            self.assertEqual(len(blk["a_lines"]), 2)
            self.assertLessEqual(blk["a_lines"][0], blk["a_lines"][1])

    def test_algorithm_toggle_and_filters(self):
        from mosaic import moss

        mrep = moss.run_paths([CORPUS])
        html = report.build_html(self.rep, moss_rep=mrep)
        self.assertIn('data-algo="moss" aria-pressed="true"', html)  # MOSS first by default
        self.assertIn('data-algo="mosaic"', html)
        self.assertIn('<section data-view="moss">', html)
        self.assertIn('<section data-view="mosaic">', html)
        self.assertIn('id="q"', html)
        self.assertIn('id="minpct"', html)
        self.assertIn("Lines Matched", html)
        for p in mrep.shown:
            self.assertIn('data-names="%s' % p.a.name.lower(), html)
        self.assertIn('document.body.dataset.start = "mosaic"', report.build_html(self.rep, moss_rep=mrep, view="mosaic"))
        # without MOSS results there is only the mosaic view
        plain = report.build_html(self.rep)
        self.assertNotIn('data-algo=', plain)
        data = json.loads(report.build_json(self.rep, moss_rep=mrep))
        self.assertEqual(set(data), {"redacted", "moss", "mosaic"})
        self.assertEqual(len(data["moss"]["pairs"]), len(mrep.shown))

    def test_pairs_kept_when_either_score_clears_threshold(self):
        thr = self.rep.cfg.threshold
        for p in self.rep.pairs:
            self.assertTrue(p.score >= thr or p.moss >= thr)
        self.assertEqual([p.score for p in self.rep.pairs],
                         sorted((p.score for p in self.rep.pairs), reverse=True))

    def test_line_mapping_in_range(self):
        for p in self.rep.pairs:
            for bl in p.blocks:
                s, e = report.block_lines(p.a, bl.a0, bl.a1)
                self.assertGreaterEqual(s, 1)
                self.assertLessEqual(e, p.a.n_lines)


class TestCli(unittest.TestCase):
    def test_end_to_end(self):
        import tempfile

        from mosaic.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.html")
            js = os.path.join(tmp, "r.json")
            code = main([CORPUS, "-o", out, "--json", js, "--quiet"])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.getsize(out) > 10_000)
            with open(js, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertTrue(data["mosaic"]["pairs"])
            self.assertTrue(data["moss"]["pairs"])

    def test_missing_path_is_an_error(self):
        from mosaic.cli import main

        self.assertEqual(main(["/definitely/not/here", "--quiet"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
