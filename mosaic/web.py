"""Memory-only rooms: an admin opens a room, peers join with its code and
upload, the admin runs the check, and closing the room deletes everything.

    python -m mosaic serve              # then open http://127.0.0.1:8765/create

One server hosts many rooms. Run it on your own machine (peers on the same
network, or anywhere through a tunnel) or on a small cloud host so anyone
can join; see the README.

Nothing is ever written to disk. Uploads, unpacked archives and reports
exist only in this process's memory, and a room's share of it is dropped
the moment the room is closed, expires, or the server stops.

Private rooms go further: the source text never leaves the server at all.
Reports show the shape of each file with the matching regions marked, and
identifier names are withheld -- from the admin as much as from anyone.
Privacy is chosen when the room is created and cannot be changed later.

Standard library only.
"""

from __future__ import annotations

import argparse
import collections
import gc
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import tarfile
import threading
import time
import traceback
import webbrowser
import zipfile
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import report as report_mod
from .engine import Config, is_source_path, run_sources
from .tokens import EXT_MAP, LANGS

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_index.html")

ARCHIVE_EXT = (".zip", ".tar", ".tar.gz", ".tgz")
JUNK_DIRS = {"__MACOSX", ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
MAX_NESTING = 2  # an LMS export zip that holds one zip per student
MAX_ARCHIVE_ENTRIES = 20_000

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_LEN = 8
DURATIONS = (1, 2, 4, 8, 12, 24, 48, 72)


class Limits:
    def __init__(
        self,
        peer_mb: int = 25,
        host_mb: int = 256,
        room_mb: int = 512,
        total_mb: int = 2048,
        max_rooms: int = 50,
        max_room_hours: float = 24.0,
        peer_files: int = 500,
        max_uploads: int = 3000,
    ) -> None:
        mb = 1024 * 1024
        self.peer_bytes = peer_mb * mb
        self.host_bytes = host_mb * mb
        self.room_bytes = room_mb * mb
        self.total_bytes = total_mb * mb
        self.max_rooms = max_rooms
        self.max_room_hours = max_room_hours
        self.peer_files = peer_files
        self.max_uploads = max_uploads


class LimitError(Exception):
    pass


# --- paths & archives (all in memory) ------------------------------------


def safe_relpath(name: str) -> str | None:
    """Normalize an uploaded/archived path; drops drives, absolute roots and `..`."""
    parts = []
    for seg in re.split(r"[\\/]+", name or ""):
        if seg in ("", ".", ".."):
            continue
        if re.match(r"^[A-Za-z]:$", seg):
            continue
        seg = re.sub(r'[<>:"|?*\x00-\x1f]', "_", seg).strip()
        if seg:
            parts.append(seg)
    return "/".join(parts) if parts else None


def is_archive(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in ARCHIVE_EXT)


def _archive_stem(name: str) -> str:
    low = name.lower()
    for ext in (".tar.gz", ".tgz", ".zip", ".tar"):
        if low.endswith(ext):
            return name[: -len(ext)]
    return name


def _junk(rel: str) -> bool:
    return any(seg in JUNK_DIRS for seg in rel.split("/"))


def _wanted(rel: str) -> bool:
    return not _junk(rel) and (is_source_path(rel) or is_archive(rel))


def _archive_members(rel: str, data: bytes, budget: list[int], entries: list[int]):
    """Yield (path, bytes) for the wanted members of an in-memory archive."""
    if rel.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                mrel = safe_relpath(info.filename)
                if not mrel or not _wanted(mrel):
                    continue
                entries[0] += 1
                budget[0] -= info.file_size
                if entries[0] > MAX_ARCHIVE_ENTRIES or budget[0] < 0:
                    raise LimitError("the archives expand to more than the size limit")
                yield mrel, zf.read(info)  # zipfile caps output at the declared size
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf:
            if not m.isfile():
                continue
            mrel = safe_relpath(m.name)
            if not mrel or not _wanted(mrel):
                continue
            entries[0] += 1
            budget[0] -= m.size
            if entries[0] > MAX_ARCHIVE_ENTRIES or budget[0] < 0:
                raise LimitError("the archives expand to more than the size limit")
            fh = tf.extractfile(m)
            if fh is not None:
                yield mrel, fh.read()


def expand_in_memory(files: list[tuple[str, bytes]], budget_bytes: int) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Unpack archives (up to MAX_NESTING deep) and keep only source files."""
    out: list[tuple[str, bytes]] = []
    warnings: list[str] = []
    budget = [budget_bytes]
    entries = [0]
    pending = files
    for depth in range(MAX_NESTING + 1):
        nxt: list[tuple[str, bytes]] = []
        for rel, data in pending:
            if _junk(rel):
                continue
            if is_archive(rel):
                if depth >= MAX_NESTING:
                    warnings.append(f"skipped {rel}: archives nested too deeply")
                    continue
                stem = _archive_stem(rel)
                try:
                    for mrel, mdata in _archive_members(rel, data, budget, entries):
                        nxt.append((f"{stem}/{mrel}", mdata))
                except LimitError:
                    raise
                except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError) as exc:
                    warnings.append(f"could not open {rel.rsplit('/', 1)[-1]}: {exc}")
            elif is_source_path(rel):
                out.append((rel, data))
        pending = nxt
        if not pending:
            break
    return out, warnings


def strip_wrapper(files: list[tuple[str, bytes]], *, keep_one_level: bool = False) -> list[tuple[str, bytes]]:
    """Remove leading folders that every path shares.

    With keep_one_level, stop while the next level still holds folders, so
    `assignment3/alice/a.c` keeps `alice` (the submission) but a single
    `alice/a.c` upload is not flattened into loose files.
    """
    while files:
        if any("/" not in rel for rel, _ in files):
            break
        firsts = {rel.split("/", 1)[0] for rel, _ in files}
        if len(firsts) != 1:
            break
        stripped = [(rel.split("/", 1)[1], d) for rel, d in files]
        if keep_one_level and all("/" not in rel for rel, _ in stripped):
            break
        files = stripped
    return files


def clean_label(raw: str) -> str:
    s = re.sub(r"[^\w .@()+-]+", " ", raw or "", flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip(" .")[:60]


def normalize_code(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()[:16]


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LEN else code


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# --- rooms ---------------------------------------------------------------


class Upload:
    """One submission. Its files exist only in this object."""

    def __init__(self, label: str, source: str, files: list[tuple[str, bytes]], secret: str | None = None) -> None:
        self.id = secrets.token_hex(6)
        self.label = label
        self.source = source  # "peer" | "host"
        self.files: dict[str, bytes] = dict(files)
        self.created = time.time()
        self.updated = self.created
        self.secret_hash = _hash(secret) if secret else None

    @property
    def size(self) -> int:
        return sum(len(d) for d in self.files.values())

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "source": self.source,
            "files": len(self.files),
            "bytes": self.size,
            "created": self.created,
            "updated": self.updated,
            "paths": sorted(self.files)[:200],
        }


class Room:
    def __init__(self, code: str, name: str, private: bool, ttl_seconds: float, limits: Limits) -> None:
        self.code = code
        self.name = name
        self._private = bool(private)
        self.instructions = ""
        self.admin_token = secrets.token_urlsafe(24)
        self.created = time.time()
        self.expires = self.created + ttl_seconds
        self.accepting = True
        self.limits = limits
        self.uploads: dict[str, Upload] = {}
        self.skeleton: dict[str, bytes] = {}
        self.version = 0  # bumps whenever the set of files changes
        self.analysis: dict = {"status": "idle"}
        self.report_html: bytes | None = None
        self.report_json: bytes | None = None
        self.report_links: dict[str, float] = {}
        self.ended = False
        self.discarded = 0
        self.lock = threading.RLock()

    @property
    def private(self) -> bool:
        """Fixed at creation. There is deliberately no setter."""
        return self._private

    # -- helpers --
    def total_bytes(self) -> int:
        with self.lock:
            return sum(u.size for u in self.uploads.values()) + sum(len(d) for d in self.skeleton.values())

    def unique_label(self, label: str, exclude: str | None = None) -> str:
        taken = {u.label.lower() for u in self.uploads.values() if u.id != exclude}
        if label.lower() not in taken:
            return label
        n = 2
        while f"{label} ({n})".lower() in taken:
            n += 1
        return f"{label} ({n})"

    def touch(self) -> None:
        self.version += 1

    def wipe(self) -> None:
        """Drop every upload and report of this room."""
        with self.lock:
            self.discarded += len(self.uploads)
            for u in self.uploads.values():
                u.files.clear()
            self.uploads.clear()
            self.skeleton.clear()
            self.report_html = None
            self.report_json = None
            self.report_links.clear()
            self.analysis = {"status": "idle"}
            self.accepting = False
            self.ended = True

    def new_report_link(self, ttl: float = 1800.0) -> str:
        now = time.time()
        with self.lock:
            for k in [k for k, exp in self.report_links.items() if exp < now]:
                del self.report_links[k]
            token = secrets.token_urlsafe(18)
            self.report_links[token] = now + ttl
        return token

    def report_link_ok(self, token: str) -> bool:
        with self.lock:
            exp = self.report_links.get(token)
            return exp is not None and exp >= time.time() and self.report_html is not None

    def host_state(self, join_links: list[str]) -> dict:
        with self.lock:
            a = dict(self.analysis)
            a.pop("trace", None)
            if a.get("status") == "done":
                a["stale"] = a.get("version") != self.version
            return {
                "code": display_code(self.code),
                "name": self.name,
                "private": self.private,
                "instructions": self.instructions,
                "accepting": self.accepting,
                "created": self.created,
                "expires": self.expires,
                "now": time.time(),
                "join_links": join_links,
                "uploads": sorted(
                    (u.public() for u in self.uploads.values()), key=lambda x: x["label"].lower()
                ),
                "skeleton": sorted(self.skeleton),
                "total_bytes": self.total_bytes(),
                "limits": {"room_bytes": self.limits.room_bytes, "peer_bytes": self.limits.peer_bytes},
                "analysis": a,
                "languages": sorted(LANGS),
            }

    def peer_info(self) -> dict:
        return {
            "code": display_code(self.code),
            "name": self.name,
            "private": self.private,
            "instructions": self.instructions,
            "accepting": self.accepting and not self.ended,
            "expires": self.expires,
            "now": time.time(),
            "limits": {"peer_bytes": self.limits.peer_bytes, "peer_files": self.limits.peer_files},
            "extensions": sorted(set(EXT_MAP) | set(ARCHIVE_EXT)),
        }


class Hub:
    """All rooms on this server."""

    def __init__(self, limits: Limits, create_key: str | None = None) -> None:
        self.limits = limits
        self.create_key = create_key or None
        self.rooms: dict[str, Room] = {}
        self.lock = threading.RLock()
        self.closed_rooms = 0
        self.discarded = 0

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if code not in self.rooms:
                return code

    def create(self, name: str, private: bool, hours: float) -> Room:
        with self.lock:
            if len(self.rooms) >= self.limits.max_rooms:
                raise LimitError("this server has reached its room limit; try again later")
            hours = max(0.25, min(float(hours), self.limits.max_room_hours))
            room = Room(self._new_code(), name, private, hours * 3600, self.limits)
            self.rooms[room.code] = room
            return room

    def get(self, code: str) -> Room | None:
        with self.lock:
            room = self.rooms.get(normalize_code(code))
        if room is None or room.ended:
            return None
        return room

    def close(self, code: str) -> int:
        """Wipe a room and forget it. Returns how many submissions were dropped."""
        with self.lock:
            room = self.rooms.pop(normalize_code(code), None)
        if room is None:
            return 0
        n = len(room.uploads)
        room.wipe()
        with self.lock:
            self.closed_rooms += 1
            self.discarded += n
        gc.collect()
        return n

    def close_all(self) -> int:
        n = 0
        for code in list(self.rooms):
            n += self.close(code)
        return n

    def expire_due(self) -> list[str]:
        now = time.time()
        with self.lock:
            due = [c for c, r in self.rooms.items() if r.expires <= now]
        for code in due:
            self.close(code)
        return due

    def total_bytes(self) -> int:
        with self.lock:
            rooms = list(self.rooms.values())
        return sum(r.total_bytes() for r in rooms)

    def room_for_report_link(self, token: str) -> Room | None:
        with self.lock:
            rooms = list(self.rooms.values())
        for r in rooms:
            if r.report_link_ok(token):
                return r
        return None

    def check_space(self, room: Room, incoming: int, freed: int = 0) -> str | None:
        if room.total_bytes() - freed + incoming > self.limits.room_bytes:
            return "this room is out of space; ask the host"
        if self.total_bytes() - freed + incoming > self.limits.total_bytes:
            return "the server is out of space; try again later"
        return None


# --- analysis ------------------------------------------------------------


def top_pairs(pairs, n: int) -> list:
    """The top n by mosaic score plus the top n by MOSS score, so either
    ranking can be shown in full."""
    by_mosaic = pairs[:n]
    seen = {id(p) for p in by_mosaic}
    extra = [p for p in sorted(pairs, key=lambda p: -p.moss)[:n] if id(p) not in seen]
    return by_mosaic + extra


def summarize(rep, elapsed: float) -> dict:
    top = [
        {
            "a": p.a.rel,
            "b": p.b.rel,
            "sa": p.a.submission,
            "sb": p.b.submission,
            "score": round(p.score, 4),
            "moss": round(p.moss, 4),
            "moss_a": round(p.fp_a, 4),
            "moss_b": round(p.fp_b, 4),
            "z": round(p.z, 2),
            "fp": round(p.fp, 4),
            "ast": round(p.ast, 4),
            "rename": round(p.rename.score, 4),
            "reasons": p.reasons,
        }
        for p in top_pairs(rep.pairs, 30)
    ]
    return {
        "files": len(rep.docs),
        "submissions": len({d.submission for d in rep.docs}),
        "pairs_examined": rep.n_compared,
        "above_threshold": len(rep.pairs),
        "flagged": sum(1 for p in rep.pairs if p.score >= 0.5),
        "flagged_moss": sum(1 for p in rep.pairs if p.moss >= 0.5),
        "median": round(rep.median, 4),
        "suppressed": rep.cohort.suppressed_fp + rep.cohort.suppressed_tree,
        "skipped": len(rep.skipped),
        "clusters": [{"members": c.members, "mean": round(c.mean_score, 4)} for c in rep.clusters],
        "top": top,
        "elapsed": round(elapsed, 2),
    }


def start_analysis(room: Room, opts: dict) -> str | None:
    """Kick off a check in a worker thread. Returns an error message or None."""
    try:
        cfg = Config(
            threshold=max(0.05, min(0.95, float(opts.get("threshold", 0.25)))),
            lang=opts.get("lang") if opts.get("lang") in LANGS else None,
            by_dir=True,
        )
    except (TypeError, ValueError):
        return "bad options"
    with room.lock:
        if room.analysis.get("status") == "running":
            return "a check is already running"
        if len(room.uploads) < 2:
            return "need at least two submissions to compare"
        snapshot = [
            (u.label, rel, data) for u in room.uploads.values() for rel, data in u.files.items()
        ]
        skeleton = list(room.skeleton.items())
        version = room.version
        private = room.private
        room.analysis = {
            "status": "running",
            "phase": "starting",
            "done": 0,
            "total": 0,
            "started": time.time(),
            "options": {"threshold": cfg.threshold, "lang": cfg.lang},
        }

    def progress(phase: str, i: int, n: int, what: str) -> None:
        with room.lock:
            if room.analysis.get("status") == "running":
                room.analysis.update(phase=phase, done=i, total=n)

    def work() -> None:
        try:
            t0 = time.time()
            rep = run_sources(snapshot, cfg, skeleton, progress=progress)
            if not rep.docs:
                raise ValueError("none of the uploaded files is source code mosaic can read")
            html = report_mod.build_html(rep, title=room.name, redact=private).encode("utf-8")
            js = report_mod.build_json(rep, redact=private).encode("utf-8")
            summary = summarize(rep, time.time() - t0)
            with room.lock:
                if room.ended:
                    return
                room.report_html, room.report_json = html, js
                room.analysis = {
                    "status": "done",
                    "summary": summary,
                    "version": version,
                    "finished": time.time(),
                    "options": {"threshold": cfg.threshold, "lang": cfg.lang},
                }
        except Exception as exc:  # surface every failure in the dashboard
            with room.lock:
                room.analysis = {
                    "status": "error",
                    "error": str(exc) or exc.__class__.__name__,
                    "trace": traceback.format_exc(limit=3),
                }
        finally:
            del snapshot[:]

    threading.Thread(target=work, daemon=True, name=f"mosaic-check-{room.code}").start()
    return None


# --- multipart -----------------------------------------------------------


def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    """(field name, payload) pairs in order. Paths travel in a JSON manifest,
    not in multipart filenames, which browsers encode inconsistently."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("expected multipart/form-data")
    head = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    msg = BytesParser(policy=policy.HTTP).parsebytes(head + body)
    if not msg.is_multipart():
        raise ValueError("expected multipart/form-data")
    return [
        (str(part.get_param("name", header="content-disposition") or ""), part.get_payload(decode=True) or b"")
        for part in msg.iter_parts()
    ]


def collect_files(parts: list[tuple[str, bytes]], field: str, manifest_field: str) -> list[tuple[str, bytes]]:
    names = {n: p for n, p in parts if n == manifest_field}
    manifest = json.loads(names.get(manifest_field, b"[]") or b"[]")
    payloads = [p for n, p in parts if n == field]
    if len(manifest) != len(payloads):
        raise ValueError("file list does not match the manifest")
    out = []
    for raw, data in zip(manifest, payloads):
        rel = safe_relpath(str(raw))
        if rel and _wanted(rel):
            out.append((rel, data))
    return out


def form_json(parts: list[tuple[str, bytes]], field: str) -> dict:
    for n, p in parts:
        if n == field:
            val = json.loads(p or b"{}")
            return val if isinstance(val, dict) else {}
    return {}


# --- network helpers -----------------------------------------------------


def lan_addresses() -> list[str]:
    """This machine's LAN IPv4 addresses. Sends no packets."""
    found: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # UDP connect only picks a route
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith("127.") and not ip.startswith("169.254.")]


class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], collections.deque] = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()

    def allow(self, bucket: str, ip: str, limit: int, window: float) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits[(bucket, ip)]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self.hits) > 50_000:  # keep the table bounded
                for k in [k for k, v in self.hits.items() if not v][:10_000]:
                    del self.hits[k]
            return True


# --- HTTP ----------------------------------------------------------------


class RoomServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, hub: Hub, *, public_url: str | None = None, behind_proxy: bool = False) -> None:
        super().__init__(addr, Handler)
        self.hub = hub
        self.public_url = public_url.rstrip("/") if public_url else None
        self.behind_proxy = behind_proxy
        self.rate = RateLimiter()
        self.stopping = threading.Event()

    def base_urls(self) -> list[str]:
        """Addresses peers might use to reach this server, best first."""
        if self.public_url:
            return [self.public_url]
        port = self.server_address[1]
        host = self.server_address[0]
        urls = []
        if host in ("0.0.0.0", "::", ""):
            urls += [f"http://{ip}:{port}" for ip in lan_addresses()]
        elif host not in ("127.0.0.1", "localhost", "::1"):
            urls.append(f"http://{host}:{port}")
        urls.append(f"http://127.0.0.1:{port}")
        return urls

    def join_links(self, room: Room) -> list[str]:
        return [f"{u}/join/{display_code(room.code)}" for u in self.base_urls()]

    @property
    def local_only(self) -> bool:
        return not self.public_url and self.server_address[0] in ("127.0.0.1", "localhost", "::1")


class Handler(BaseHTTPRequestHandler):
    server: RoomServer
    server_version = "mosaic"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("MOSAIC_WEB_LOG"):
            super().log_message(fmt, *args)

    # -- responses --
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # keep submissions out of browser caches
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if ctype.startswith("text/html"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src data:; connect-src 'self'; form-action 'self'; base-uri 'none'; "
                "frame-ancestors 'none'",
            )
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    def _page(self) -> None:
        with open(INDEX_PATH, "rb") as fh:
            self._send(200, fh.read(), "text/html; charset=utf-8")

    # -- request helpers --
    @property
    def hub(self) -> Hub:
        return self.server.hub

    @property
    def ip(self) -> str:
        if self.server.behind_proxy:
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _csrf_ok(self) -> bool:
        # A custom header forces a CORS preflight that this server never
        # approves, so other websites cannot post here through a browser.
        return self.headers.get("X-Mosaic") == "1"

    def _room(self, code: str) -> Room | None:
        """Look a room up, rate-limiting misses so codes cannot be guessed."""
        room = self.hub.get(code)
        if room is None:
            if not self.server.rate.allow("miss", self.ip, 40, 600):
                self._error(429, "too many attempts; wait a few minutes")
                return None
            self._error(404, "no open room with that code")
            return None
        return room

    def _admin(self, code: str) -> Room | None:
        room = self.hub.get(code)
        token = self.headers.get("X-Mosaic-Admin") or ""
        if room is None or not hmac.compare_digest(token.encode(), room.admin_token.encode()):
            self._error(403, "admin access only")
            return None
        return room

    def _body(self, limit: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "bad Content-Length")
            return None
        if length <= 0:
            self._error(400, "empty request")
            return None
        if length > limit:
            # Read and discard a moderately oversized body so the client gets
            # the 413 message instead of a reset connection.
            if length <= 64 * 1024 * 1024:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True
            self._error(413, f"upload is {length / 1e6:.1f} MB; the limit is {limit / 1e6:.0f} MB")
            return None
        return self.rfile.read(length)

    def _json_body(self) -> dict | None:
        body = self._body(64 * 1024)
        if body is None:
            return None
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            self._error(400, "bad JSON")
            return None
        if not isinstance(data, dict):
            self._error(400, "bad JSON")
            return None
        return data

    # -- routing --
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/create") or re.match(r"^/(join|j|room)/[A-Za-z0-9-]+/?$", path):
            return self._page()
        if path == "/api/health":
            return self._json({"ok": True})
        if path == "/api/config":
            return self._json(
                {
                    "create_key_required": bool(self.hub.create_key),
                    "durations": [d for d in DURATIONS if d <= self.hub.limits.max_room_hours],
                    "local_only": self.server.local_only,
                }
            )

        m = re.match(r"^/r/([A-Za-z0-9_-]{16,64})$", path)
        if m:
            room = self.hub.room_for_report_link(m.group(1))
            if room is None:
                return self._send(
                    404,
                    b"<!doctype html><meta charset=utf-8><title>mosaic</title>"
                    b"<p style='font-family:system-ui;padding:24px'>This report link has expired or the "
                    b"room was closed. Open the report again from the room's admin page.</p>",
                    "text/html; charset=utf-8",
                )
            with room.lock:
                data = room.report_html or b""
            return self._send(200, data, "text/html; charset=utf-8")

        m = re.match(r"^/api/rooms/([A-Za-z0-9-]+)/info$", path)
        if m:
            room = self._room(m.group(1))
            if room is not None:
                self._json(room.peer_info())
            return

        m = re.match(r"^/api/rooms/([A-Za-z0-9-]+)/admin/(state|report\.html|report\.json)$", path)
        if m:
            room = self._admin(m.group(1))
            if room is None:
                return
            what = m.group(2)
            if what == "state":
                return self._json(room.host_state(self.server.join_links(room)))
            with room.lock:
                data = room.report_html if what.endswith("html") else room.report_json
            if data is None:
                return self._error(404, "no report yet")
            ctype = "text/html; charset=utf-8" if what.endswith("html") else "application/json; charset=utf-8"
            return self._send(200, data, ctype)
        return self._error(404, "not found")

    def do_POST(self):
        if not self._csrf_ok():
            return self._error(403, "forbidden")
        path = urlsplit(self.path).path

        if path == "/api/rooms":
            return self._create_room()

        m = re.match(r"^/api/rooms/([A-Za-z0-9-]+)/submit$", path)
        if m:
            room = self._room(m.group(1))
            if room is not None:
                self._peer_submit(room)
            return

        m = re.match(r"^/api/rooms/([A-Za-z0-9-]+)/admin/([a-z-]+)$", path)
        if m:
            room = self._admin(m.group(1))
            if room is None:
                return
            action = m.group(2)
            if action == "upload":
                return self._host_upload(room)
            data = self._json_body()
            if data is None:
                return
            if action == "settings":
                with room.lock:
                    # privacy is intentionally not settable here
                    if "name" in data:
                        room.name = str(data["name"]).strip()[:120] or room.name
                    if "instructions" in data:
                        room.instructions = str(data["instructions"]).strip()[:2000]
                    if "accepting" in data:
                        room.accepting = bool(data["accepting"])
                return self._json({"ok": True, "private": room.private})
            if action == "run":
                err = start_analysis(room, data)
                return self._error(409, err) if err else self._json({"started": True}, 202)
            if action == "report-link":
                if room.report_html is None:
                    return self._error(404, "no report yet")
                return self._json({"url": "/r/" + room.new_report_link()})
            if action == "end":
                n = self.hub.close(room.code)
                return self._json({"ended": True, "discarded": n})
        return self._error(404, "not found")

    def do_DELETE(self):
        if not self._csrf_ok():
            return self._error(403, "forbidden")
        path = urlsplit(self.path).path

        m = re.match(r"^/api/rooms/([A-Za-z0-9-]+)/submit/([0-9a-f]{12})$", path)
        if m:
            room = self._room(m.group(1))
            if room is None:
                return
            secret = self.headers.get("X-Mosaic-Receipt") or ""
            with room.lock:
                u = room.uploads.get(m.group(2))
                if u is None or not u.secret_hash or not hmac.compare_digest(u.secret_hash, _hash(secret)):
                    return self._error(404, "no such submission")
                u.files.clear()
                del room.uploads[u.id]
                room.touch()
            return self._json({"withdrawn": True})

        m = re.match(r"^/api/rooms/([A-Za-z0-9-]+)/admin/(uploads/([0-9a-f]{12})|skeleton)$", path)
        if m:
            room = self._admin(m.group(1))
            if room is None:
                return
            with room.lock:
                if m.group(3):
                    u = room.uploads.pop(m.group(3), None)
                    if u is None:
                        return self._error(404, "no such submission")
                    u.files.clear()
                else:
                    room.skeleton.clear()
                room.touch()
            return self._json({"deleted": True})
        return self._error(404, "not found")

    # -- actions --
    def _create_room(self):
        if not self.server.rate.allow("create", self.ip, 10, 3600):
            return self._error(429, "too many rooms created from this address; try again later")
        data = self._json_body()
        if data is None:
            return
        if self.hub.create_key and not hmac.compare_digest(
            str(data.get("key", "")).encode(), self.hub.create_key.encode()
        ):
            return self._error(403, "wrong room-creation key")
        name = str(data.get("name", "")).strip()[:120]
        if not name:
            return self._error(400, "give the room a name")
        privacy = data.get("privacy")
        if privacy not in ("private", "open"):
            return self._error(400, "choose whether code is visible in the report")
        try:
            hours = float(data.get("hours", 4))
        except (TypeError, ValueError):
            return self._error(400, "bad duration")
        try:
            room = self.hub.create(name, privacy == "private", hours)
        except LimitError as exc:
            return self._error(503, str(exc))
        return self._json(
            {
                "code": display_code(room.code),
                "admin_token": room.admin_token,
                "admin_path": f"/room/{display_code(room.code)}",
                "join_links": self.server.join_links(room),
                "private": room.private,
                "expires": room.expires,
            },
            201,
        )

    def _peer_submit(self, room: Room):
        if not room.accepting:
            return self._error(403, "the host is not accepting submissions right now")
        if not self.server.rate.allow("submit", self.ip, 20, 600):
            return self._error(429, "too many uploads from this address; try again in a few minutes")
        body = self._body(room.limits.peer_bytes)
        if body is None:
            return
        try:
            parts = parse_multipart(self.headers.get("Content-Type", ""), body)
            del body
            meta = form_json(parts, "meta")
            files = collect_files(parts, "file", "manifest")
            del parts
            files, warnings = expand_in_memory(files, room.limits.peer_bytes * 4)
        except LimitError as exc:
            return self._error(413, str(exc))
        except (ValueError, KeyError):
            return self._error(400, "could not read the upload")
        files = strip_wrapper(files)
        if not files:
            return self._error(400, "no source files found in the upload (supported: "
                               + ", ".join(sorted(EXT_MAP)) + ")")
        if len(files) > room.limits.peer_files:
            return self._error(413, f"too many files ({len(files)}); the limit is {room.limits.peer_files}")
        label = clean_label(str(meta.get("name", "")))
        if not label:
            return self._error(400, "please enter your name or roll number")
        replace_id = str(meta.get("replace_id") or "")
        receipt = str(meta.get("receipt") or "")
        incoming = sum(len(d) for _, d in files)

        with room.lock:
            if room.ended:
                return self._error(410, "this room has been closed")
            existing = room.uploads.get(replace_id) if replace_id else None
            if existing is not None and not (
                existing.secret_hash and hmac.compare_digest(existing.secret_hash, _hash(receipt))
            ):
                existing = None
            err = self.hub.check_space(room, incoming, existing.size if existing else 0)
            if err:
                return self._error(507, err)
            if existing is None and len(room.uploads) >= room.limits.max_uploads:
                return self._error(507, "this room is full; ask the host")
            if existing is not None:
                existing.files.clear()
                existing.files.update(files)
                existing.label = room.unique_label(label, exclude=existing.id)
                existing.updated = time.time()
                up, secret = existing, receipt
            else:
                secret = secrets.token_urlsafe(16)
                up = Upload(room.unique_label(label), "peer", files, secret)
                room.uploads[up.id] = up
            room.touch()
            result = {
                "id": up.id,
                "receipt": secret,
                "label": up.label,
                "files": sorted(up.files),
                "bytes": up.size,
                "at": up.updated,
                "replaced": existing is not None,
                "warnings": warnings,
            }
        return self._json(result, 201)

    def _host_upload(self, room: Room):
        """Bulk upload from the admin page: one folder (or zip) per submission."""
        body = self._body(room.limits.host_bytes)
        if body is None:
            return
        try:
            parts = parse_multipart(self.headers.get("Content-Type", ""), body)
            del body
            meta = form_json(parts, "meta")
            kind = meta.get("kind", "submissions")
            files = collect_files(parts, "file", "manifest")
            del parts
            files, warnings = expand_in_memory(files, room.limits.host_bytes * 4)
        except LimitError as exc:
            return self._error(413, str(exc))
        except (ValueError, KeyError):
            return self._error(400, "could not read the upload")
        if not files:
            return self._error(400, "no source files found in the upload")
        incoming = sum(len(d) for _, d in files)

        with room.lock:
            if kind == "skeleton":
                err = self.hub.check_space(room, incoming, sum(len(d) for d in room.skeleton.values()))
                if err:
                    return self._error(507, err)
                room.skeleton = dict(strip_wrapper(files))
                room.touch()
                return self._json({"skeleton": sorted(room.skeleton), "warnings": warnings}, 201)

            err = self.hub.check_space(room, incoming)
            if err:
                return self._error(507, err)
            files = strip_wrapper(files, keep_one_level=True)
            groups: dict[str, list[tuple[str, bytes]]] = collections.defaultdict(list)
            for rel, data in files:
                if "/" in rel:
                    sub, inner = rel.split("/", 1)
                else:  # loose files: one submission per file
                    sub, inner = rel.rsplit(".", 1)[0], rel
                groups[sub].append((inner, data))
            added = []
            by_label = {u.label.lower(): u for u in room.uploads.values() if u.source == "host"}
            for sub, gfiles in groups.items():
                label = clean_label(sub) or "submission"
                prev = by_label.get(label.lower())
                if prev is not None:  # re-uploading the same folder replaces it
                    prev.files.clear()
                    prev.files.update(gfiles)
                    prev.updated = time.time()
                    added.append(prev.label)
                    continue
                up = Upload(room.unique_label(label), "host", gfiles)
                room.uploads[up.id] = up
                added.append(up.label)
            room.touch()
        return self._json({"added": added, "warnings": warnings}, 201)


# --- entry point ---------------------------------------------------------


def create_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    *,
    limits: Limits | None = None,
    create_key: str | None = None,
    public_url: str | None = None,
    behind_proxy: bool = False,
) -> RoomServer:
    hub = Hub(limits or Limits(), create_key)
    return RoomServer((host, port), hub, public_url=public_url, behind_proxy=behind_proxy)


def watch_expiry(srv: RoomServer, interval: float = 15.0, log=print) -> None:
    while not srv.stopping.wait(interval):
        for code in srv.hub.expire_due():
            log(f"  room {display_code(code)} expired -- its files were deleted")


KEEP_AWAKE_SECONDS = 10 * 60  # Render's free plan sleeps after 15 idle minutes


def _ping(url: str) -> None:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "mosaic-keep-awake"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def keep_awake(srv: RoomServer, interval: float = KEEP_AWAKE_SECONDS, ping=_ping, log=print) -> None:
    """While any room is open, request our own public address now and then.

    Free hosts such as Render put a service to sleep after a stretch with no
    inbound traffic, and sleeping restarts the process, which wipes every
    room. Going out through the public URL counts as a visit, so a room
    lives for the time its admin chose. With no rooms open the pings stop
    and the host may sleep as usual -- nothing would be lost.
    """
    if not srv.public_url:
        return
    url = srv.public_url + "/api/health"
    while not srv.stopping.wait(interval):
        with srv.hub.lock:
            busy = bool(srv.hub.rooms)
        if not busy:
            continue
        try:
            ping(url)
        except Exception as exc:  # a missed ping is retried next round
            log(f"  keep-awake ping failed: {exc}")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    # Render sets RENDER_EXTERNAL_URL (the public https address) and RENDER;
    # picking them up means a Render deploy needs no manual URL setting.
    on_render = bool(env.get("RENDER"))
    p = argparse.ArgumentParser(
        prog="mosaic serve",
        description="Host memory-only rooms that peers join with a code to submit code.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default=env.get("MOSAIC_HOST", "0.0.0.0"),
                   help="interface to listen on; 127.0.0.1 keeps it to this machine")
    p.add_argument("--port", type=int, default=int(env.get("PORT", "8765")),
                   help="port (cloud hosts set PORT for you)")
    p.add_argument("--public-url", default=env.get("MOSAIC_PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL"),
                   help="the address peers use, when behind a tunnel or cloud host")
    p.add_argument("--create-key", default=env.get("MOSAIC_CREATE_KEY"),
                   help="require this key to create rooms (recommended on a public server)")
    p.add_argument("--behind-proxy", action="store_true",
                   default=env.get("MOSAIC_BEHIND_PROXY") == "1" or on_render,
                   help="trust X-Forwarded-For (set when running behind a cloud load balancer)")
    p.add_argument("--max-rooms", type=int, default=_env_int("MOSAIC_MAX_ROOMS", 50))
    p.add_argument("--max-room-hours", type=float, default=float(_env_int("MOSAIC_MAX_ROOM_HOURS", 24)),
                   help="longest a room may stay open")
    p.add_argument("--max-upload-mb", type=int, default=_env_int("MOSAIC_MAX_UPLOAD_MB", 25), help="per peer upload")
    p.add_argument("--max-room-mb", type=int, default=_env_int("MOSAIC_MAX_ROOM_MB", 512),
                   help="memory budget per room")
    p.add_argument("--max-total-mb", type=int, default=_env_int("MOSAIC_MAX_TOTAL_MB", 2048),
                   help="memory budget for the whole server")
    p.add_argument("--keep-awake", action=argparse.BooleanOptionalAction,
                   default=env.get("MOSAIC_KEEP_AWAKE", "1" if on_render else "0") == "1",
                   help="while rooms are open, ping the public URL every 10 minutes so a free "
                        "host does not put the server to sleep (on by default on Render)")
    p.add_argument("--open", action="store_true", help="open the create-room page in your browser")
    args = p.parse_args(argv)

    limits = Limits(
        peer_mb=args.max_upload_mb,
        room_mb=args.max_room_mb,
        total_mb=args.max_total_mb,
        max_rooms=args.max_rooms,
        max_room_hours=args.max_room_hours,
    )
    try:
        srv = create_server(args.host, args.port, limits=limits, create_key=args.create_key,
                            public_url=args.public_url, behind_proxy=args.behind_proxy)
    except OSError as exc:
        print(f"mosaic: cannot listen on {args.host}:{args.port}: {exc}")
        return 1

    local = f"http://127.0.0.1:{srv.server_address[1]}"
    print()
    print("  mosaic rooms server is running. Everything is held in memory only.")
    print()
    print(f"  Create a room:   {(srv.public_url or local)}/create")
    urls = srv.base_urls()
    shown = [u for u in urls if "127.0.0.1" not in u] or urls
    print(f"  Peers join at:   {shown[0]}   (with the room code)")
    for u in shown[1:]:
        print(f"                   {u}")
    if srv.local_only:
        print("  (listening on this computer only -- peers elsewhere cannot connect)")
    elif not args.create_key:
        print("  Note: anyone who can reach this server can create rooms. Use --create-key to restrict.")
    print()
    print("  Closing a room deletes its files. Ctrl+C stops the server and deletes every room.")
    print()

    threading.Thread(target=watch_expiry, args=(srv,), daemon=True).start()
    if args.keep_awake:
        if srv.public_url:
            threading.Thread(target=keep_awake, args=(srv,), daemon=True, name="mosaic-keep-awake").start()
            print("  Keep-awake is on: while a room is open the server pings itself so it does not sleep.")
            print()
        else:
            print("  --keep-awake needs --public-url (or MOSAIC_PUBLIC_URL); it is off.")
            print()
    if args.open:
        webbrowser.open(local + "/create")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stopping.set()
        n = srv.hub.close_all()
        srv.server_close()
    total = srv.hub.discarded
    print(f"  server stopped -- {srv.hub.closed_rooms} room(s) closed, {total} submission(s) deleted"
          f"{'' if n == total else f' ({n} at shutdown)'}; nothing was written to disk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
