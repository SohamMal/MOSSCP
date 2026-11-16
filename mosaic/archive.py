"""Keep a permanent copy of each room's latest report in a private GitHub
repository.

Rooms live in memory, and free hosts wipe their disk on every restart, so
the one durable place is outside the server. After every check the room's
report.html and results.json are committed to

    <repo>/<prefix>/<created date>-<room name>-<code>/

in a single commit that replaces the previous copy (git history keeps the
older versions). Configure with MOSAIC_GITHUB_REPO (owner/name) and
MOSAIC_GITHUB_TOKEN (a fine-grained token with Contents: read and write on
that one repository). Standard library only.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


class ArchiveError(Exception):
    pass


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:limit].strip("-") or "room"


class GitHubArchive:
    def __init__(self, repo: str, token: str, *, branch: str | None = None, prefix: str = "reports",
                 api: str = API, timeout: float = 60.0) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
            raise ValueError("MOSAIC_GITHUB_REPO must look like owner/name")
        if not token:
            raise ValueError("MOSAIC_GITHUB_TOKEN is not set")
        self.repo = repo
        self._token = token
        self.branch = branch
        self.prefix = prefix.strip("/")
        self.api = api.rstrip("/")
        self.timeout = timeout
        self.lock = threading.Lock()  # one commit at a time keeps the branch linear

    def describe(self) -> str:
        return f"github.com/{self.repo}" + (f" ({self.branch})" if self.branch else "")

    def folder_url(self, folder: str) -> str:
        return f"https://github.com/{self.repo}/tree/{self.branch or 'HEAD'}/{self.prefix}/{folder}"

    # -- HTTP --
    def _call(self, method: str, path: str, body: dict | None = None, ok=(200, 201)) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api + path, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "mosaic-archive",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                raw = res.read()
                status = res.status
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise ArchiveError(f"cannot reach GitHub: {exc}") from None
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if status not in ok:
            msg = payload.get("message") if isinstance(payload, dict) else None
            raise ArchiveError(f"GitHub {method} {path.split('?')[0]} -> {status}: {msg or 'error'}")
        return payload

    # -- saving --
    def save(self, folder: str, files: dict[str, bytes], message: str) -> str:
        """Commit files under <prefix>/<folder>/, replacing earlier copies.
        Returns the commit sha."""
        with self.lock:
            for attempt in range(3):
                try:
                    return self._commit(folder, files, message)
                except ArchiveError as exc:
                    # another writer moved the branch: rebuild on the new head
                    if attempt < 2 and ("422" in str(exc) or "409" in str(exc)):
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise
        raise ArchiveError("could not save")

    def _commit(self, folder: str, files: dict[str, bytes], message: str) -> str:
        r = f"/repos/{self.repo}"
        if not self.branch:
            self.branch = self._call("GET", r).get("default_branch") or "main"
        try:
            head = self._call("GET", f"{r}/git/ref/heads/{self.branch}")["object"]["sha"]
        except ArchiveError as exc:
            if "404" in str(exc) or "409" in str(exc):  # empty repository: create the first commit
                self._call("PUT", f"{r}/contents/{self.prefix}/README.md", {
                    "message": "Start the mosaic report archive",
                    "content": base64.b64encode(b"# mosaic reports\n\nOne folder per room.\n").decode(),
                    "branch": self.branch,
                })
                head = self._call("GET", f"{r}/git/ref/heads/{self.branch}")["object"]["sha"]
            else:
                raise
        base_tree = self._call("GET", f"{r}/git/commits/{head}")["tree"]["sha"]
        entries = []
        for name, data in files.items():
            blob = self._call("POST", f"{r}/git/blobs", {
                "content": base64.b64encode(data).decode(), "encoding": "base64",
            })
            entries.append({"path": f"{self.prefix}/{folder}/{name}", "mode": "100644", "type": "blob",
                            "sha": blob["sha"]})
        tree = self._call("POST", f"{r}/git/trees", {"base_tree": base_tree, "tree": entries})
        commit = self._call("POST", f"{r}/git/commits", {
            "message": message, "tree": tree["sha"], "parents": [head],
        })
        self._call("PATCH", f"{r}/git/refs/heads/{self.branch}", {"sha": commit["sha"], "force": False})
        return commit["sha"]


def clean_repo(raw: str) -> str:
    """Accept owner/name, or a pasted GitHub URL / clone address."""
    s = (raw or "").strip().strip("'\"").strip()
    s = re.sub(r"^(https?://)?(www\.)?github\.com[/:]", "", s, flags=re.I)
    s = re.sub(r"^git@github\.com:", "", s, flags=re.I)
    s = re.sub(r"(\.git)?/*$", "", s)
    parts = [p for p in s.split("/") if p]
    return "/".join(parts[:2]) if len(parts) >= 2 else s


def from_env(env) -> tuple[GitHubArchive | None, str | None]:
    """(archive, problem). Neither variable set means archiving is off."""
    raw_repo = env.get("MOSAIC_GITHUB_REPO") or ""
    repo = clean_repo(raw_repo)
    token = (env.get("MOSAIC_GITHUB_TOKEN") or "").strip()
    if not repo and not token:
        return None, None
    try:
        return GitHubArchive(repo, token, branch=(env.get("MOSAIC_GITHUB_BRANCH") or "").strip() or None,
                             prefix=(env.get("MOSAIC_GITHUB_PREFIX") or "reports")), None
    except ValueError as exc:
        if "owner/name" in str(exc):
            return None, f"MOSAIC_GITHUB_REPO must look like owner/name (got {raw_repo.strip()!r})"
        return None, str(exc)
