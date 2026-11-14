"""Predictor rooms: build each peer's mine.cpp against bpsim, run it on the
server's traces, and publish only the cohort's average and median MPKI.

Privacy is structural, not a matter of hiding columns:

* A submission's source is held in memory only until it is compiled, then
  dropped. What remains is the compiled program, which nobody can download.
* The evaluator receives the programs with no names attached, in shuffled
  order. Per-submission MPKIs live in one local list inside `evaluate()`,
  are reduced to aggregates, and the list is cleared. No room, upload, log
  line or API response ever holds an individual MPKI -- not the admin's,
  not the submitter's own.
* Submitters see only their own build status (rule violations, compiler
  errors, storage bits). Program output is never shown to anyone.

Building and running happen in a private temporary folder, one job at a time
across the whole server, with CPU, memory and file-size limits, no network
where the OS allows it, and a minimal environment. The folder is deleted as
soon as the job ends, so at most one submission's program is ever on disk.
"""

from __future__ import annotations

import importlib.util
import os
import queue
import random
import re
import resource
import shutil
import signal
import statistics
import subprocess
import tempfile
import threading
import time

PREDICTOR_NAME = "mine"
SOURCE_NAME = "mine.cpp"
MAX_SOURCE_BYTES = 256 * 1024
TRACE_SUFFIXES = (".trace", ".trace.gz", ".txt", ".txt.gz", ".gz")

DEFAULT_HARNESS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bpsim")

CXXFLAGS = ["-O2", "-std=c++17", "-Wall", "-Wextra"]

# Allowed origins of every file the preprocessor pulls in. Anything else
# (a trace, /etc/passwd, the server's own files) is refused before compiling,
# so compiler errors can never echo another file's contents.
SYSTEM_INCLUDE_PREFIXES = ("/usr/include/", "/usr/lib/gcc/", "/usr/local/include/", "/usr/lib/llvm")

# Defence in depth beyond the course's own check_rules.py: constructs that
# could pull outside data into the program at build time.
EXTRA_FORBIDDEN = [
    (re.compile(r"\b(asm|__asm|__asm__)\b"), "inline assembly is not allowed"),
    (re.compile(r"\bincbin\b"), "embedding files is not allowed"),
    (re.compile(r"\b_Pragma\b"), "_Pragma is not allowed"),
    (re.compile(r"^\s*#\s*(embed|import|include_next)\b", re.M),
     "this preprocessor directive is not allowed"),
    (re.compile(r"\b__attribute__\s*\(\s*\(\s*(constructor|destructor|section|alias|ifunc)\b"),
     "this attribute is not allowed"),
]

RESULT_LINE = re.compile(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)\s*$")
TOTAL_LINE = re.compile(r"^\s*TOTAL\s+(\d+)\s*/\s*(\d+)\s+bits\s*$", re.M)
# The only program messages ever relayed (to the submitter alone), so a
# predictor cannot smuggle data out through its own error text.
STORAGE_ERROR = re.compile(
    r"^storage error \[[\w .:+-]{0,40}\]: (width must be 1\.\.64 bits|table needs at least one entry|"
    r"need at least one set and one way|tagBits must be 0\.\.64|value -?\d{1,20} does not fit in "
    r"\d{1,2}-bit (un)?signed field|index \d{1,20} out of range \(size \d{1,20}\)|"
    r"set \d{1,20} out of range \(sets \d{1,20}\)|way \d{1,20} out of range \(ways \d{1,20}\)|"
    r"access to an invalid way|\w+\(\) needs an LRU cache|shiftIn\(\) needs an unsigned register|"
    r"storage may only be created as a member of your predictor, in its constructor "
    r"\(not in predict\(\)/update\(\), not as a global\))$"
)


class BuildResult:
    def __init__(self, ok: bool, messages: list[str], binary: bytes | None = None, bits: int | None = None):
        self.ok = ok
        self.messages = messages
        self.binary = binary
        self.bits = bits


class RunFailed(Exception):
    """A submission did not produce a valid result. Carries no detail on
    purpose: failures are only ever counted."""


def _set_limits(cpu_s: int, mem_mb: int, fsize_mb: int):
    def pre() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 2))
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_mb << 20, fsize_mb << 20))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))

    return pre


def make_undumpable() -> bool:
    """Stop same-user processes (such as a submitted program) from reading this
    server's memory through /proc or ptrace. Linux only; best effort."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE, 0
    except Exception:
        return False


class Harness:
    """The course simulator (src/, tools/check_rules.py) plus the traces."""

    def __init__(
        self,
        root: str = DEFAULT_HARNESS,
        traces_dir: str | None = None,
        *,
        run_seconds: int = 600,
        build_seconds: int = 180,
        cxx: str | None = None,
    ) -> None:
        self.root = os.path.abspath(root)
        self.src = os.path.join(self.root, "src")
        self.rules_path = os.path.join(self.root, "tools", "check_rules.py")
        self.traces_dir = os.path.abspath(traces_dir or os.path.join(self.root, "traces"))
        self.run_seconds = run_seconds
        self.build_seconds = build_seconds
        self.cxx = cxx or os.environ.get("CXX") or shutil.which("g++") or shutil.which("c++")
        self.lock = threading.Lock()  # one sandboxed job at a time, server-wide
        self._base: str | None = None
        self._main_o: str | None = None
        self._rules = None
        self._netns: list[str] | None = None
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None

    # -- availability --
    @property
    def traces(self) -> list[str]:
        try:
            names = sorted(os.listdir(self.traces_dir))
        except OSError:
            return []
        return [
            os.path.join(self.traces_dir, n)
            for n in names
            if n.endswith(TRACE_SUFFIXES) and os.path.isfile(os.path.join(self.traces_dir, n))
        ]

    def trace_names(self) -> list[str]:
        return [os.path.basename(t) for t in self.traces]

    def problem(self) -> str | None:
        """Why predictor rooms cannot run here, or None."""
        if not self.cxx:
            return "no C++ compiler (g++) on this server"
        for f in ("main.cpp", "predictor.h", "storage.h"):
            if not os.path.isfile(os.path.join(self.src, f)):
                return f"the simulator is missing src/{f} (looked in {self.root})"
        if not os.path.isfile(self.rules_path):
            return "the simulator is missing tools/check_rules.py"
        if not self.traces:
            return f"no traces found in {self.traces_dir}"
        if not shutil.which("gzip") and any(t.endswith(".gz") for t in self.traces):
            return "gzip is needed to read .gz traces"
        return None

    # -- sandbox --
    def _basedir(self) -> str:
        if self._base is None or not os.path.isdir(self._base):
            self._base = tempfile.mkdtemp(prefix="mosaic-bp-")
            os.chmod(self._base, 0o700)
        return self._base

    def _netns_prefix(self) -> list[str]:
        """Run jobs without network when unprivileged namespaces are allowed."""
        if self._netns is None:
            self._netns = []
            exe = shutil.which("unshare")
            if exe:
                try:
                    r = subprocess.run([exe, "-r", "-n", "true"], capture_output=True, timeout=10)
                    if r.returncode == 0:
                        self._netns = [exe, "-r", "-n", "--"]
                except (OSError, subprocess.SubprocessError):
                    pass
        return self._netns

    @property
    def network_isolated(self) -> bool:
        return bool(self._netns_prefix())

    def _exec(self, argv: list[str], cwd: str, timeout: float, *, mem_mb: int, fsize_mb: int,
              out_cap: int = 1 << 20) -> tuple[int | None, str, str]:
        """Run argv in cwd with limits. Output goes to files under the size
        limit, so a chatty program cannot fill the server's memory."""
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": cwd, "TMPDIR": cwd}
        out_path, err_path = os.path.join(cwd, ".out"), os.path.join(cwd, ".err")
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            try:
                p = subprocess.Popen(
                    self._netns_prefix() + argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err, start_new_session=True,
                    preexec_fn=_set_limits(int(timeout) + 1, mem_mb, fsize_mb),
                )
            except OSError as exc:
                return None, "", str(exc)
            try:
                code = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
                code = None
            else:
                try:  # reap anything the job left behind (e.g. a gzip child)
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass

        def read(path: str) -> str:
            try:
                with open(path, "rb") as fh:
                    return fh.read(out_cap).decode("utf-8", "replace")
            finally:
                os.remove(path)

        return code, read(out_path), read(err_path)

    def _ensure_main_o(self) -> str | None:
        """Compile the simulator's own main.cpp once; it holds no user data."""
        if self._main_o and os.path.isfile(self._main_o):
            return None
        d = os.path.join(self._basedir(), "harness")
        os.makedirs(d, exist_ok=True)
        obj = os.path.join(d, "main.o")
        code, _, err = self._exec(
            [self.cxx, *CXXFLAGS, "-I", self.src, "-c", os.path.join(self.src, "main.cpp"), "-o", obj],
            d, self.build_seconds, mem_mb=2048, fsize_mb=64,
        )
        if code != 0:
            return "the simulator itself failed to build: " + err[-500:]
        self._main_o = obj
        return None

    def _check_rules(self, path: str) -> list[str]:
        if self._rules is None:
            spec = importlib.util.spec_from_file_location("bpsim_check_rules", self.rules_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._rules = mod
        try:
            problems = self._rules.check(path)
        except Exception as exc:  # the checker choking is itself a reason to stop
            return [f"the rule checker could not read the file ({exc.__class__.__name__})"]
        return [f"line {ln}: [{rule}] {msg}" for ln, rule, msg in problems]

    @staticmethod
    def _extra_checks(source: str) -> list[str]:
        joined = source.replace("\\\r\n", "").replace("\\\n", "")  # undo line splices
        out = []
        for pat, msg in EXTRA_FORBIDDEN:
            m = pat.search(joined)
            if m:
                out.append(f"line {joined.count(chr(10), 0, m.start()) + 1}: {msg}")
        return out

    def _includes_ok(self, job: str, src_path: str) -> list[str]:
        code, out, err = self._exec(
            [self.cxx, "-std=c++17", "-I", self.src, "-M", src_path],
            job, 60, mem_mb=2048, fsize_mb=8,
        )
        if code != 0:
            return []  # the compile step reports the error properly
        deps = out.replace("\\\n", " ").split(":", 1)[-1].split()
        allowed_src = os.path.realpath(self.src) + os.sep
        bad = []
        for dep in deps:
            real = os.path.realpath(dep if os.path.isabs(dep) else os.path.join(job, dep))
            if real == os.path.realpath(src_path):
                continue
            if real.startswith(allowed_src) or real.startswith(SYSTEM_INCLUDE_PREFIXES):
                continue
            bad.append("includes a file outside the simulator and the standard library")
            break
        return bad

    def _clean_compiler_output(self, text: str, job: str) -> list[str]:
        text = text.replace(job + os.sep, "").replace(self.src + os.sep, "src/")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) > 40:
            lines = lines[:40] + [f"... {len(lines) - 40} more lines"]
        return [ln[:300] for ln in lines]

    # -- build --
    def build(self, source: bytes) -> BuildResult:
        """Check, compile and size-check one mine.cpp. The source exists on
        disk only for the duration of this call."""
        with self.lock:
            err = self._ensure_main_o()
            if err:
                return BuildResult(False, [err])
            job = tempfile.mkdtemp(prefix="job-", dir=self._basedir())
            try:
                src_path = os.path.join(job, SOURCE_NAME)
                with open(src_path, "wb") as fh:
                    fh.write(source)
                text = source.decode("utf-8", "replace")
                problems = self._check_rules(src_path) + self._extra_checks(text)
                if not problems:
                    problems = self._includes_ok(job, src_path)
                if problems:
                    return BuildResult(False, ["Your code breaks the storage rules:"] + problems)

                obj = os.path.join(job, "mine.o")
                exe = os.path.join(job, "bpsim")
                code, _, cerr = self._exec(
                    [self.cxx, *CXXFLAGS, "-I", self.src, "-c", src_path, "-o", obj],
                    job, self.build_seconds, mem_mb=2048, fsize_mb=64,
                )
                if code != 0:
                    head = "Compiling took too long." if code is None else "Your code does not compile:"
                    return BuildResult(False, [head] + self._clean_compiler_output(cerr, job))
                os.remove(src_path)  # the program no longer needs the source
                code, _, lerr = self._exec(
                    [self.cxx, "-s", "-o", exe, self._main_o, obj], job, self.build_seconds,
                    mem_mb=2048, fsize_mb=64,
                )
                if code != 0:
                    return BuildResult(False, ["Your code compiles but does not link:"]
                                       + self._clean_compiler_output(lerr, job))

                code, out, serr = self._exec([exe, "-s", PREDICTOR_NAME], job, 30, mem_mb=1024, fsize_mb=1)
                if code == 2 and "unknown predictor" in serr:
                    return BuildResult(False, [f'Register your predictor as "{PREDICTOR_NAME}": '
                                               f'REGISTER_PREDICTOR("{PREDICTOR_NAME}", YourClass);'])
                if code is None:
                    return BuildResult(False, ["Your predictor's constructor took too long."])
                first = serr.strip().splitlines()[0] if serr.strip() else ""
                if code != 0:
                    msg = [first] if STORAGE_ERROR.match(first) else []
                    return BuildResult(False, ["Your predictor stops while it is being created."] + msg)
                m = TOTAL_LINE.search(out)
                if not m:
                    return BuildResult(False, ["Could not read your predictor's storage size."])
                bits, budget = int(m.group(1)), int(m.group(2))
                if bits > budget:
                    return BuildResult(False, [f"Your predictor uses {bits:,} bits; the budget is {budget:,}."], bits=bits)
                with open(exe, "rb") as fh:
                    binary = fh.read()
                return BuildResult(True, [], binary=binary, bits=bits)
            finally:
                shutil.rmtree(job, ignore_errors=True)

    # -- run --
    def run(self, binary: bytes) -> list[float]:
        """MPKI per trace for one compiled submission. The caller must not
        keep the result beyond aggregating it."""
        traces = self.traces
        with self.lock:
            job = tempfile.mkdtemp(prefix="job-", dir=self._basedir())
            try:
                exe = os.path.join(job, "bpsim")
                with open(exe, "wb") as fh:
                    fh.write(binary)
                os.chmod(exe, 0o700)
                code, out, _ = self._exec(
                    [exe, "-p", PREDICTOR_NAME, *traces], job, self.run_seconds, mem_mb=1024, fsize_mb=1,
                )
            finally:
                shutil.rmtree(job, ignore_errors=True)
        if code != 0:
            raise RunFailed()
        results = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line or line.startswith(("predictor:", "trace ", "average MPKI:")):
                continue
            m = RESULT_LINE.match(line)
            if not m:
                raise RunFailed()  # unexpected output: don't trust any of it
            results.append((m.group(1), float(m.group(5))))
        if [n for n, _ in results] != [os.path.basename(t) for t in traces]:
            raise RunFailed()
        return [v for _, v in results]

    # -- background builds --
    def submit_build(self, fn) -> None:
        """Queue a build callback; builds run one after another."""
        self._queue.put(fn)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._drain, daemon=True, name="mosaic-bp-build")
            self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                fn = self._queue.get(timeout=30)
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                pass

    def shutdown(self) -> None:
        if self._base:
            shutil.rmtree(self._base, ignore_errors=True)
            self._base = None
            self._main_o = None


# --- aggregation -----------------------------------------------------------


def _agg(values: list[float]) -> dict:
    return {"mean": round(statistics.fmean(values), 3), "median": round(statistics.median(values), 3)}


def evaluate(harness: Harness, binaries: list[bytes], progress=None, cancelled=None) -> dict:
    """Run every program and return ONLY cohort aggregates.

    `binaries` carries no names. It is shuffled so that progress timing says
    nothing about who is being run. Individual results are kept in local
    lists and cleared before returning.
    """
    order = list(range(len(binaries)))
    random.SystemRandom().shuffle(order)
    traces = harness.trace_names()
    per_sub: list[float] = []
    per_trace: list[list[float]] = [[] for _ in traces]
    failed = 0
    t0 = time.time()
    try:
        for i, idx in enumerate(order):
            if cancelled and cancelled():
                raise RuntimeError("cancelled")
            try:
                mpkis = harness.run(binaries[idx])
            except RunFailed:
                failed += 1
            else:
                per_sub.append(statistics.fmean(mpkis))
                for j, v in enumerate(mpkis):
                    per_trace[j].append(v)
                del mpkis
            if progress:
                progress(i + 1, len(order))
        out = {
            "evaluated": len(per_sub),
            "failed": failed,
            "traces": len(traces),
            "elapsed": round(time.time() - t0, 1),
        }
        if per_sub:
            out["mpki"] = _agg(per_sub)
            out["per_trace"] = [{"trace": name, **_agg(vals)} for name, vals in zip(traces, per_trace)]
        return out
    finally:
        per_sub.clear()
        for vals in per_trace:
            vals.clear()
        order.clear()
