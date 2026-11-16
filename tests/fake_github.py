"""A tiny in-memory stand-in for the parts of the GitHub API the archive uses."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGitHub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, token: str = "test-token", repo: str = "me/reports", empty: bool = False):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.token, self.repo = token, repo
        self.objects: dict[str, dict] = {}
        self.head: str | None = None
        self.commits = 0
        self.lock = threading.Lock()
        if not empty:
            self.head = self._commit({}, None, "init")
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def _sha(self, kind: str, payload) -> str:
        return hashlib.sha1((kind + json.dumps(payload, sort_keys=True)).encode()).hexdigest()

    def _commit(self, files: dict[str, bytes], parent, message) -> str:
        tree = self._sha("tree", {k: hashlib.sha1(v).hexdigest() for k, v in files.items()})
        self.objects[tree] = {"type": "tree", "files": dict(files)}
        sha = self._sha("commit", [tree, parent, message, self.commits])
        self.objects[sha] = {"type": "commit", "tree": tree, "parent": parent, "message": message}
        self.commits += 1
        return sha

    def files(self) -> dict[str, bytes]:
        if not self.head:
            return {}
        return self.objects[self.objects[self.head]["tree"]]["files"]

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: FakeGitHub

    def log_message(self, *a):
        pass

    def _reply(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.server.token}":
            self._reply(401, {"message": "Bad credentials"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._auth():
            return
        s, r = self.server, f"/repos/{self.server.repo}"
        with s.lock:
            if self.path == r:
                return self._reply(200, {"default_branch": "main"})
            if self.path == f"{r}/git/ref/heads/main":
                if not s.head:
                    return self._reply(409, {"message": "Git Repository is empty."})
                return self._reply(200, {"object": {"sha": s.head}})
            m = re.fullmatch(rf"{r}/git/commits/(\w+)", self.path)
            if m and m.group(1) in s.objects:
                return self._reply(200, {"tree": {"sha": s.objects[m.group(1)]["tree"]}})
        self._reply(404, {"message": "Not Found"})

    def do_POST(self):
        if not self._auth():
            return
        s, r, body = self.server, f"/repos/{self.server.repo}", self._body()
        with s.lock:
            if not s.head:
                return self._reply(409, {"message": "Git Repository is empty."})
            if self.path == f"{r}/git/blobs":
                data = base64.b64decode(body["content"])
                sha = hashlib.sha1(data).hexdigest()
                s.objects[sha] = {"type": "blob", "data": data}
                return self._reply(201, {"sha": sha})
            if self.path == f"{r}/git/trees":
                files = dict(s.objects[body["base_tree"]]["files"])
                for e in body["tree"]:
                    files[e["path"]] = s.objects[e["sha"]]["data"]
                tree = s._sha("tree", {k: hashlib.sha1(v).hexdigest() for k, v in files.items()})
                s.objects[tree] = {"type": "tree", "files": files}
                return self._reply(201, {"sha": tree})
            if self.path == f"{r}/git/commits":
                sha = s._sha("commit", [body["tree"], body["parents"], body["message"], s.commits])
                s.objects[sha] = {"type": "commit", "tree": body["tree"], "parent": body["parents"][0],
                                  "message": body["message"]}
                s.commits += 1
                return self._reply(201, {"sha": sha})
        self._reply(404, {"message": "Not Found"})

    def do_PATCH(self):
        if not self._auth():
            return
        s, body = self.server, self._body()
        with s.lock:
            if self.path == f"/repos/{s.repo}/git/refs/heads/main":
                if s.objects.get(body["sha"], {}).get("parent") != s.head:
                    return self._reply(422, {"message": "Update is not a fast forward"})
                s.head = body["sha"]
                return self._reply(200, {"object": {"sha": s.head}})
        self._reply(404, {"message": "Not Found"})

    def do_PUT(self):
        if not self._auth():
            return
        s, body = self.server, self._body()
        m = re.fullmatch(rf"/repos/{s.repo}/contents/(.+)", self.path)
        with s.lock:
            if m and not s.head:
                s.head = s._commit({m.group(1): base64.b64decode(body["content"])}, None, body["message"])
                return self._reply(201, {"commit": {"sha": s.head}})
        self._reply(422, {"message": "unexpected"})
