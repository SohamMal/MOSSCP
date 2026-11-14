"""Tests for the memory-only rooms server (mosaic serve)."""

from __future__ import annotations

import builtins
import http.client
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mosaic import web  # noqa: E402

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")


def corpus_files(sub: str) -> list[tuple[str, bytes]]:
    out = []
    root = os.path.join(CORPUS, sub)
    for dirpath, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as fh:
                out.append((os.path.relpath(p, root).replace(os.sep, "/"), fh.read()))
    return out


#: Words that occur both in the corpus and in the report's own interface
#: text, so finding them proves nothing about leaked code.
UI_WORDS = {
    "count", "median", "return", "struct", "tokens", "files", "submissions", "report", "score",
    "match", "matches", "shared", "lines", "summary", "values", "items", "total", "print",
    "label", "order", "empty", "sorted", "string", "using", "while", "value", "index",
    "every", "middle",  # footer text; CSS vertical-align
    "source",  # the admin state's "source": "peer" field name
}


def identifiers(subs) -> set[str]:
    """Identifiers from the submissions' code: none may appear in a private report."""
    from mosaic import tokens as tk

    known = tk.PY_KEYWORDS | tk.C_KEYWORDS | tk.PY_BUILTINS | tk.C_BUILTINS
    words: set[str] = set()
    file_stems: set[str] = set()
    for sub in subs:
        for rel, data in corpus_files(sub):
            words |= set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", data.decode()))
            file_stems.add(os.path.splitext(os.path.basename(rel))[0])  # file names are shown by design
    return {w for w in words if w not in known and w.lower() not in UI_WORDS and w not in file_stems}


def leaked_words(words: set[str], text: str) -> list[str]:
    return sorted(w for w in words if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(w), text))


def multipart(meta: dict, files: list[tuple[str, bytes]]) -> tuple[str, bytes]:
    boundary = "----mosaic" + secrets.token_hex(8)
    chunks: list[bytes] = []

    def part(name: str, data: bytes) -> None:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="f"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
        )

    part("meta", json.dumps(meta).encode())
    part("manifest", json.dumps([p for p, _ in files]).encode())
    for _, data in files:
        part("file", data)
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class ServerCase(unittest.TestCase):
    limits = None
    create_key = None

    def setUp(self):
        self.srv = web.create_server("127.0.0.1", 0, limits=self.limits or web.Limits(),
                                     create_key=self.create_key)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.hub = self.srv.hub

    def tearDown(self):
        self.srv.stopping.set()
        self.srv.shutdown()
        self.thread.join(timeout=5)
        self.srv.server_close()
        self.hub.close_all()

    # -- http helpers --
    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            if isinstance(body, dict):
                body = json.dumps(body).encode()
                headers = {"Content-Type": "application/json", **(headers or {})}
            conn.request(method, path, body=body, headers=headers or {})
            res = conn.getresponse()
            data = res.read()
        finally:
            conn.close()
        payload = json.loads(data) if "json" in (res.getheader("Content-Type") or "") and data else data
        return res.status, payload, res

    def create(self, privacy="open", name="Room", hours=2, key=None):
        body = {"name": name, "privacy": privacy, "hours": hours}
        if key is not None:
            body["key"] = key
        status, j, _ = self.req("POST", "/api/rooms", body, {"X-Mosaic": "1"})
        self.assertEqual(status, 201, j)
        return j["code"], j["admin_token"]

    def admin(self, code, token, method, action, body=None, extra=None):
        h = {"X-Mosaic-Admin": token, "X-Mosaic": "1", **(extra or {})}
        if body is None and method == "POST":
            body = {}
        return self.req(method, f"/api/rooms/{code}/admin/{action}", body, h)

    def submit(self, code, name, files, **meta):
        ctype, body = multipart({"name": name, **meta}, files)
        return self.req("POST", f"/api/rooms/{code}/submit", body, {"Content-Type": ctype, "X-Mosaic": "1"})

    def run_check(self, code, token, timeout=30):
        status, j, _ = self.admin(code, token, "POST", "run", {"threshold": 0.25})
        self.assertEqual(status, 202, j)
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, st, _ = self.admin(code, token, "GET", "state")
            if st["analysis"]["status"] in ("done", "error"):
                self.assertEqual(st["analysis"]["status"], "done", st["analysis"])
                return st
            time.sleep(0.1)
        self.fail("check did not finish")


class TestRooms(ServerCase):
    def test_pages(self):
        for path in ("/", "/create", "/join/ABCD-EFGH", "/room/ABCD-EFGH"):
            status, body, res = self.req("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(b"mosaic", body)
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))

    def test_create_validates(self):
        h = {"X-Mosaic": "1"}
        self.assertEqual(self.req("POST", "/api/rooms", {"name": "", "privacy": "open"}, h)[0], 400)
        self.assertEqual(self.req("POST", "/api/rooms", {"name": "x"}, h)[0], 400)  # privacy is required
        self.assertEqual(self.req("POST", "/api/rooms", {"name": "x", "privacy": "maybe"}, h)[0], 400)
        self.assertEqual(self.req("POST", "/api/rooms", {"name": "x", "privacy": "open"})[0], 403)  # CSRF

    def test_codes_are_forgiving_and_rooms_are_isolated(self):
        code_a, tok_a = self.create(name="A")
        code_b, tok_b = self.create(name="B")
        self.assertRegex(code_a, r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        status, info, _ = self.req("GET", f"/api/rooms/{code_a.lower().replace('-', '')}/info")
        self.assertEqual(status, 200)
        self.assertEqual(info["name"], "A")
        self.assertNotIn("admin_token", json.dumps(info))
        # a room's admin token does not open another room
        self.assertEqual(self.admin(code_b, tok_a, "GET", "state")[0], 403)
        self.assertEqual(self.admin(code_a, tok_a, "GET", "state")[0], 200)
        # the room code is not an admin credential
        self.assertEqual(self.admin(code_a, code_a, "GET", "state")[0], 403)

    def test_privacy_is_locked_at_creation(self):
        code, tok = self.create("private")
        status, j, _ = self.admin(code, tok, "POST", "settings", {"private": False, "privacy": "open", "name": "Renamed"})
        self.assertEqual(status, 200)
        self.assertTrue(j["private"])
        _, st, _ = self.admin(code, tok, "GET", "state")
        self.assertTrue(st["private"])
        self.assertEqual(st["name"], "Renamed")  # other settings still change
        room = self.hub.get(code)
        with self.assertRaises(AttributeError):
            room.private = False

    def test_wrong_codes_are_rate_limited(self):
        statuses = [self.req("GET", f"/api/rooms/ZZZZ{i:04d}/info")[0] for i in range(45)]
        self.assertEqual(statuses[0], 404)
        self.assertIn(429, statuses)

    def test_room_limit(self):
        self.hub.limits.max_rooms = 2
        self.create()
        self.create()
        status, j, _ = self.req("POST", "/api/rooms", {"name": "x", "privacy": "open"}, {"X-Mosaic": "1"})
        self.assertEqual(status, 503)


class TestCreateKey(ServerCase):
    create_key = "s3cret"

    def test_key_required(self):
        status, _, _ = self.req("POST", "/api/rooms", {"name": "x", "privacy": "open"}, {"X-Mosaic": "1"})
        self.assertEqual(status, 403)
        status, _, _ = self.req("POST", "/api/rooms", {"name": "x", "privacy": "open", "key": "nope"}, {"X-Mosaic": "1"})
        self.assertEqual(status, 403)
        self.create(key="s3cret")
        _, cfg, _ = self.req("GET", "/api/config")
        self.assertTrue(cfg["create_key_required"])


class TestPeerFlow(ServerCase):
    def test_submit_replace_withdraw(self):
        code, _ = self.create()
        status, r, _ = self.submit(code, "Alice", corpus_files("s01_original"))
        self.assertEqual(status, 201, r)
        self.assertEqual(r["files"], ["stats.py"])

        status, bad, _ = self.submit(code, "Alice", corpus_files("s04_independent"), replace_id=r["id"], receipt="forged")
        self.assertEqual(bad["label"], "Alice (2)")  # forged receipt -> new submission, not a takeover

        status, rep, _ = self.submit(code, "Alice", corpus_files("s05_independent"), replace_id=r["id"], receipt=r["receipt"])
        self.assertTrue(rep["replaced"])
        self.assertEqual(rep["files"], ["summary.py"])

        base = f"/api/rooms/{code}/submit/{r['id']}"
        self.assertEqual(self.req("DELETE", base, headers={"X-Mosaic": "1", "X-Mosaic-Receipt": "x"})[0], 404)
        self.assertEqual(self.req("DELETE", base, headers={"X-Mosaic": "1", "X-Mosaic-Receipt": r["receipt"]})[0], 200)

    def test_name_required_and_source_required(self):
        code, _ = self.create()
        self.assertEqual(self.submit(code, "  ", corpus_files("s01_original"))[0], 400)
        self.assertEqual(self.submit(code, "Dan", [("readme.md", b"hello")])[0], 400)

    def test_zip_unpacked_and_traversal_blocked(self):
        code, _ = self.create()
        inner = zip_bytes({"helper.py": b"def helper(x):\n    return x * 2\n" * 10})
        outer = zip_bytes({
            "proj/main.py": b"import sys\n" + b"print(sys.argv)\n" * 10,
            "../../evil.py": b"print('escaped')\n" * 10,
            "proj/notes.txt": b"not code",
            "proj/__MACOSX/._main.py": b"junk",
            "proj/lib.zip": inner,
        })
        status, r, _ = self.submit(code, "Bob", [("submission.zip", outer)])
        self.assertEqual(status, 201, r)
        files = set(r["files"])
        self.assertIn("proj/main.py", files)
        self.assertIn("proj/lib/helper.py", files)
        self.assertIn("evil.py", files)
        self.assertFalse(any(".." in f or f.endswith(".txt") or "__MACOSX" in f for f in files))

    def test_closed_for_submissions(self):
        code, tok = self.create()
        self.admin(code, tok, "POST", "settings", {"accepting": False})
        self.assertEqual(self.submit(code, "Carol", corpus_files("s01_original"))[0], 403)


class TestLimits(ServerCase):
    limits = web.Limits(peer_mb=1, host_mb=2, room_mb=2, total_mb=3)

    def test_oversized_upload(self):
        code, _ = self.create()
        self.assertEqual(self.submit(code, "Eve", [("big.py", b"x = 1\n" * 300_000)])[0], 413)

    def test_zip_bomb(self):
        code, _ = self.create()
        bomb = zip_bytes({f"f{i}.py": b"a = 1\n" * 200_000 for i in range(6)})
        self.assertLess(len(bomb), 1024 * 1024)
        self.assertEqual(self.submit(code, "Mallory", [("bomb.zip", bomb)])[0], 413)

    def test_room_memory_budget(self):
        code, _ = self.create()
        chunk = [("a.py", (b"x = 1\n" * 150_000))]  # ~0.9 MB each
        self.assertEqual(self.submit(code, "A", chunk)[0], 201)
        self.assertEqual(self.submit(code, "B", chunk)[0], 201)
        self.assertEqual(self.submit(code, "C", chunk)[0], 507)  # room budget is 2 MB


class TestAnalysis(ServerCase):
    def load(self, code, subs):
        for sub in subs:
            status, r, _ = self.submit(code, sub, corpus_files(sub))
            self.assertEqual(status, 201, r)

    def test_open_room_report_shows_code(self):
        code, tok = self.create("open")
        self.load(code, ["s01_original", "s02_renamed", "s04_independent"])
        st = self.run_check(code, tok)
        top = st["analysis"]["summary"]["top"][0]
        self.assertEqual({top["sa"], top["sb"]}, {"s01_original", "s02_renamed"})
        _, html, _ = self.admin(code, tok, "GET", "report.html")
        self.assertIn(b"read_numbers", html)
        _, js, _ = self.admin(code, tok, "GET", "report.json")
        self.assertFalse(js["redacted"])
        self.assertTrue(any(p["rename_map"] for p in js["pairs"]))

    def test_private_room_never_reveals_code(self):
        subs = ["s01_original", "s02_renamed", "s03_refactored", "c01_original", "c02_disguised"]
        code, tok = self.create("private")
        self.load(code, subs)
        st = self.run_check(code, tok)
        self.assertGreater(st["analysis"]["summary"]["top"][0]["score"], 0.6)  # still detects

        _, html, _ = self.admin(code, tok, "GET", "report.html")
        _, js, _ = self.admin(code, tok, "GET", "report.json")
        _, link, _ = self.admin(code, tok, "POST", "report-link")
        _, linked_html, _ = self.req("GET", link["url"])
        _, state, _ = self.admin(code, tok, "GET", "state")
        surfaces = {
            "report.html": html.decode(),
            "report.json": json.dumps(js),
            "report link": linked_html.decode(),
            "admin state": json.dumps(state),
        }
        secret_words = identifiers(subs)
        self.assertGreater(len(secret_words), 15)
        for where, text in surfaces.items():
            leaked = leaked_words(secret_words, text)
            self.assertEqual(leaked, [], f"code leaked into {where}: {leaked[:10]}")
        self.assertIn('class="sil"', surfaces["report.html"])
        self.assertTrue(js["redacted"])
        self.assertTrue(all(p["rename_map"] == [] for p in js["pairs"]))
        # whole source lines must not appear either
        self.assertNotIn("values.append(float(part))", surfaces["report.html"])

    def test_bulk_upload_and_stale_flag(self):
        code, tok = self.create()
        entries = {}
        for sub in ("s01_original", "s02_renamed", "s04_independent"):
            for rel, data in corpus_files(sub):
                entries[f"assignment3/{sub}/{rel}"] = data
        ctype, body = multipart({"kind": "submissions"}, [("all.zip", zip_bytes(entries))])
        status, r, _ = self.admin(code, tok, "POST", "upload", body, {"Content-Type": ctype})
        self.assertEqual(status, 201, r)
        self.assertEqual(sorted(r["added"]), ["s01_original", "s02_renamed", "s04_independent"])
        self.run_check(code, tok)
        self.submit(code, "Late", corpus_files("s05_independent"))
        _, st, _ = self.admin(code, tok, "GET", "state")
        self.assertTrue(st["analysis"]["stale"])

    def test_run_needs_two(self):
        code, tok = self.create()
        self.submit(code, "Solo", corpus_files("s01_original"))
        self.assertEqual(self.admin(code, tok, "POST", "run", {})[0], 409)

    def test_report_link(self):
        code, tok = self.create()
        self.assertEqual(self.admin(code, tok, "POST", "report-link")[0], 404)
        self.load(code, ["s01_original", "s02_renamed"])
        self.run_check(code, tok)
        _, j, _ = self.admin(code, tok, "POST", "report-link")
        status, html, _ = self.req("GET", j["url"])
        self.assertEqual(status, 200)
        self.assertIn(b"s01_original", html)
        self.assertEqual(self.req("GET", "/r/" + "x" * 24)[0], 404)
        self.admin(code, tok, "POST", "end")
        self.assertEqual(self.req("GET", j["url"])[0], 404)


class TestClosing(ServerCase):
    def test_close_deletes_room_and_spares_others(self):
        code_a, tok_a = self.create(name="A")
        code_b, tok_b = self.create(name="B")
        self.submit(code_a, "x", corpus_files("s01_original"))
        self.submit(code_a, "y", corpus_files("s02_renamed"))
        self.submit(code_b, "z", corpus_files("s04_independent"))
        self.run_check(code_a, tok_a)
        room_a = self.hub.get(code_a)

        status, r, _ = self.admin(code_a, tok_a, "POST", "end")
        self.assertEqual((status, r["discarded"]), (200, 2))
        self.assertEqual(room_a.uploads, {})
        self.assertIsNone(room_a.report_html)
        self.assertTrue(room_a.ended)
        self.assertEqual(self.req("GET", f"/api/rooms/{code_a}/info")[0], 404)
        self.assertEqual(self.submit(code_a, "late", corpus_files("s01_original"))[0], 404)
        self.assertEqual(self.admin(code_a, tok_a, "GET", "state")[0], 403)
        # the other room is untouched and the server keeps running
        _, st, _ = self.admin(code_b, tok_b, "GET", "state")
        self.assertEqual(len(st["uploads"]), 1)

    def test_expiry(self):
        code, _ = self.create()
        self.submit(code, "x", corpus_files("s01_original"))
        room = self.hub.get(code)
        room.expires = time.time() + 0.3
        t = threading.Thread(target=web.watch_expiry, args=(self.srv, 0.05, lambda *_: None), daemon=True)
        t.start()
        deadline = time.time() + 5
        while self.hub.get(code) is not None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNone(self.hub.get(code))
        self.assertEqual(room.uploads, {})

    def test_keep_awake_pings_only_while_rooms_are_open(self):
        hits: list[str] = []
        stop = threading.Event()
        old_url, self.srv.public_url = self.srv.public_url, "https://example.invalid"
        old_stop, self.srv.stopping = self.srv.stopping, stop
        try:
            t = threading.Thread(target=web.keep_awake, args=(self.srv, 0.05, hits.append, lambda *_: None),
                                 daemon=True)
            t.start()
            time.sleep(0.3)
            self.assertEqual(hits, [])  # no rooms: let the host sleep
            code, _ = self.create()
            deadline = time.time() + 5
            while not hits and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn("https://example.invalid/api/health", hits)
            self.hub.close(code)
            time.sleep(0.15)
            n = len(hits)
            time.sleep(0.3)
            self.assertEqual(len(hits), n)
        finally:
            stop.set()
            self.srv.public_url, self.srv.stopping = old_url, old_stop

    def test_shutdown_deletes_everything(self):
        code, _ = self.create()
        self.submit(code, "x", corpus_files("s01_original"))
        room = self.hub.get(code)
        self.assertEqual(self.hub.close_all(), 1)
        self.assertEqual(room.uploads, {})
        self.assertEqual(self.hub.rooms, {})

    def test_nothing_is_written_to_disk(self):
        """Two rooms, uploads, zips, checks, reports, closing -- with every
        write-mode open() trapped. Reading the page template is allowed."""
        writes: list[str] = []
        real_open, real_os_open = builtins.open, os.open

        def guarded_open(file, mode="r", *a, **kw):
            if any(c in mode for c in "wax+"):
                writes.append(f"{file} ({mode})")
            return real_open(file, mode, *a, **kw)

        def guarded_os_open(p, flags, *a, **kw):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                writes.append(f"os.open {p}")
            return real_os_open(p, flags, *a, **kw)

        payloads = {s: corpus_files(s) for s in ("s01_original", "s02_renamed", "c01_original", "c02_disguised")}
        builtins.open, os.open = guarded_open, guarded_os_open
        try:
            for privacy in ("open", "private"):
                code, tok = self.create(privacy)
                for sub, files in payloads.items():
                    z = zip_bytes({f"{sub}/{rel}": d for rel, d in files})
                    self.assertEqual(self.submit(code, sub, [(f"{sub}.zip", z)])[0], 201)
                self.run_check(code, tok)
                self.admin(code, tok, "GET", "report.html")
                self.admin(code, tok, "GET", "report.json")
                self.admin(code, tok, "POST", "end")
        finally:
            builtins.open, os.open = real_open, real_os_open
        self.assertEqual(writes, [], f"files were written: {writes}")


class TestHelpers(unittest.TestCase):
    def test_safe_relpath(self):
        self.assertEqual(web.safe_relpath("../../etc/passwd"), "etc/passwd")
        self.assertEqual(web.safe_relpath("C:\\Windows\\x.py"), "Windows/x.py")
        self.assertIsNone(web.safe_relpath("../.."))

    def test_strip_wrapper(self):
        f = lambda *ps: [(p, b"") for p in ps]  # noqa: E731
        self.assertEqual([p for p, _ in web.strip_wrapper(f("a3/alice/x.c", "a3/bob/y.c"), keep_one_level=True)],
                         ["alice/x.c", "bob/y.c"])
        self.assertEqual([p for p, _ in web.strip_wrapper(f("alice/x.c"), keep_one_level=True)], ["alice/x.c"])

    def test_codes(self):
        self.assertEqual(web.normalize_code(" abcd-efgh "), "ABCDEFGH")
        self.assertEqual(web.display_code("ABCDEFGH"), "ABCD-EFGH")
        self.assertTrue(set(web.CODE_ALPHABET).isdisjoint("01OIL"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
