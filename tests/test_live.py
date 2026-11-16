"""MOSS engine, automatic checks, peer leaderboard and the GitHub archive."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mosaic import archive, moss, web  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402
from test_web import CORPUS, ServerCase, corpus_files  # noqa: E402


class TestMossEngine(unittest.TestCase):
    def setUp(self):
        self.rep = moss.run_paths([CORPUS])

    def test_renamed_copy_ranks_first(self):
        top = self.rep.pairs[0]
        self.assertEqual({top.a.name, top.b.name}, {"s01_original", "s02_renamed"})
        self.assertGreater(min(top.pct_a, top.pct_b), 0.9)
        self.assertEqual([p.lines for p in self.rep.pairs], sorted((p.lines for p in self.rep.pairs), reverse=True))

    def test_pair_score_independent_of_other_submissions(self):
        """Adding unrelated submissions must not change a pair's numbers."""
        def subs(names):
            return [(n, rel, data) for n in names for rel, data in corpus_files(n)]

        two = moss.run_sources(subs(["s01_original", "s02_renamed"]))
        many = moss.run_sources(subs(["s01_original", "s02_renamed", "s03_refactored", "s05_independent",
                                      "s06_independent", "c01_original", "c02_disguised"]))
        get = lambda r: next(p for p in r.pairs if {p.a.name, p.b.name} == {"s01_original", "s02_renamed"})
        a, b = get(two), get(many)
        self.assertEqual((a.pct_a, a.pct_b, a.lines), (b.pct_a, b.pct_b, b.lines))

    def test_dash_m_ignores_passages_shared_by_many(self):
        shared = "def helper(xs):\n    total = 0\n    for x in xs:\n        total += x * x - 1\n    return total\n"
        files = [(f"s{i:02d}", "a.py", (shared + f"\nprint({i})\n").encode()) for i in range(12)]
        self.assertTrue(moss.run_sources(files, moss.MossConfig(max_matches=20)).pairs)
        self.assertFalse(moss.run_sources(files, moss.MossConfig(max_matches=10)).pairs)

    def test_base_file_is_ignored(self):
        base = corpus_files("s01_original")
        files = [(n, rel, d) for n in ("s01_original", "s02_renamed") for rel, d in corpus_files(n)]
        self.assertFalse(moss.run_sources(files, base_files=base).pairs)

    def test_same_submission_files_never_compared(self):
        files = [("alice", "a.py", b"x = 1\n" * 50), ("alice", "b.py", b"x = 1\n" * 50)]
        self.assertFalse(moss.run_sources(files).pairs)


class TestArchive(unittest.TestCase):
    def test_commits_and_overwrites(self):
        gh = FakeGitHub()
        try:
            ar = archive.GitHubArchive(gh.repo, gh.token, api=gh.url)
            ar.save("room-1", {"report.html": b"v1", "results.json": b"{}"}, "first")
            ar.save("room-1", {"report.html": b"v2", "results.json": b"{}"}, "second")
            self.assertEqual(gh.files()["reports/room-1/report.html"], b"v2")
            self.assertEqual(gh.commits, 3)  # init + two saves
        finally:
            gh.stop()

    def test_empty_repo_gets_a_first_commit(self):
        gh = FakeGitHub(empty=True)
        try:
            archive.GitHubArchive(gh.repo, gh.token, api=gh.url).save("r", {"report.html": b"x"}, "m")
            self.assertEqual(gh.files()["reports/r/report.html"], b"x")
        finally:
            gh.stop()

    def test_bad_token_is_reported(self):
        gh = FakeGitHub()
        try:
            with self.assertRaises(archive.ArchiveError) as cm:
                archive.GitHubArchive(gh.repo, "wrong", api=gh.url).save("r", {"a": b"x"}, "m")
            self.assertIn("401", str(cm.exception))
        finally:
            gh.stop()

    def test_from_env(self):
        self.assertEqual(archive.from_env({}), (None, None))
        ar, why = archive.from_env({"MOSAIC_GITHUB_REPO": "nope"})
        self.assertIsNone(ar)
        self.assertTrue(why)
        ar, why = archive.from_env({"MOSAIC_GITHUB_REPO": "me/reports", "MOSAIC_GITHUB_TOKEN": "t"})
        self.assertIsNotNone(ar)
        self.assertNotIn("t", ar.describe().replace("github.com/me/reports", ""))


class TestLiveRooms(ServerCase):
    def setUp(self):
        self.gh = FakeGitHub()
        ar = archive.GitHubArchive(self.gh.repo, self.gh.token, api=self.gh.url)
        self.srv = web.create_server("127.0.0.1", 0, limits=web.Limits(), archive=ar, auto_delay=0.3)
        import threading

        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.auto = threading.Thread(target=web.watch_auto, args=(self.srv, 0.1), daemon=True)
        self.auto.start()
        self.hub = self.srv.hub

    def tearDown(self):
        super().tearDown()
        self.gh.stop()

    def wait(self, cond, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = cond()
            if v:
                return v
            time.sleep(0.1)
        self.fail("timed out")

    def test_auto_run_leaderboard_and_archive(self):
        code, tok = self.create("private", name="Assignment 3")
        for sub in ("s01_original", "s02_renamed", "s05_independent"):
            self.assertEqual(self.submit(code, sub, corpus_files(sub))[0], 201)

        # a check starts by itself and peers see the leaderboard, MOSS first
        info = self.wait(lambda: (lambda i: i if i["leaderboard"]["status"] == "done" and not i["leaderboard"]["pending"] else None)(
            self.req("GET", f"/api/rooms/{code}/info")[1]))
        self.assertEqual([u["label"] for u in info["uploaders"]], ["s01_original", "s02_renamed", "s05_independent"])
        top = info["leaderboard"]["moss"][0]
        self.assertEqual({top["a"], top["b"]}, {"s01_original", "s02_renamed"})
        self.assertEqual(set(top), {"a", "b", "pct_a", "pct_b", "lines"})
        self.assertTrue(info["leaderboard"]["mosaic"])
        self.assertTrue(info["archived"])

        # the report is committed to the archive, under a stable folder
        st = self.wait(lambda: (lambda s: s if s["archive"].get("state") == "saved" else None)(
            self.admin(code, tok, "GET", "state")[1]))
        folder = self.hub.get(code).archive_dir
        self.assertIn("assignment-3", folder)
        saved = self.gh.files()
        html1 = saved[f"reports/{folder}/report.html"]
        self.assertIn(b'data-view="moss"', html1)
        self.assertIn("s02_renamed", json.loads(saved[f"reports/{folder}/results.json"])["moss"]["submissions"])
        self.assertIn(folder, st["archive"]["url"])

        # a new upload triggers another check, which overwrites the saved copy
        commits = self.gh.commits
        self.assertEqual(self.submit(code, "s03_refactored", corpus_files("s03_refactored"))[0], 201)
        self.wait(lambda: self.gh.commits > commits)
        self.wait(lambda: b"s03_refactored" in self.gh.files()[f"reports/{folder}/report.html"])
        self.assertEqual(len([k for k in self.gh.files() if k.startswith(f"reports/{folder}/")]), 2)

        # closing the room deletes it here but the saved report stays
        self.admin(code, tok, "POST", "end", {})
        self.assertIn(f"reports/{folder}/report.html", self.gh.files())

    def test_no_auto_run_with_one_submission(self):
        code, _ = self.create()
        self.submit(code, "s01_original", corpus_files("s01_original"))
        time.sleep(1.0)
        self.assertEqual(self.hub.get(code).analysis["status"], "idle")
        self.assertEqual(self.gh.commits, 1)

    def test_debounce_waits_for_quiet(self):
        room = self.hub.create("x", False, 1)
        room.uploads = {"a": web.Upload("a", "peer", [("a.py", b"x=1\n" * 30)]),
                        "b": web.Upload("b", "peer", [("b.py", b"y=2\n" * 30)])}
        room.touch()
        self.assertEqual(self.hub.auto_due(room.last_change + 0.1), [])
        self.assertEqual(self.hub.auto_due(room.last_change + 0.5), [room])


if __name__ == "__main__":
    unittest.main()
