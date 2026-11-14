"""Predictor rooms: build peers' mine.cpp, run on traces, publish only the
class average and median MPKI. Needs g++; skipped without it."""

from __future__ import annotations

import gzip
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mosaic import bpeval, web  # noqa: E402
from test_web import ServerCase  # noqa: E402

HARNESS = bpeval.DEFAULT_HARNESS

ALWAYS_TAKEN = b"""#include "predictor.h"
class AlwaysTaken : public BranchPredictor {
 public:
  bool predict(uint64_t) override { return true; }
  void update(uint64_t, bool) override {}
};
REGISTER_PREDICTOR("mine", AlwaysTaken);
"""

LAST_OUTCOME = b"""#include "predictor.h"
class LastOutcome : public BranchPredictor {
  Register last_{1, 1, Unsigned, "last"};
 public:
  bool predict(uint64_t) override { return last_.get() == 1; }
  void update(uint64_t, bool taken) override { last_.shiftIn(taken); }
};
REGISTER_PREDICTOR("mine", LastOutcome);
"""

BIMODAL = b"""#include "predictor.h"
class Bimodal : public BranchPredictor {
  static constexpr unsigned BITS = 10;
  Table ctr_{1u << BITS, 2, 2, Unsigned, "counters"};
  static uint64_t idx(uint64_t pc) { return pc & ((1u << BITS) - 1); }
 public:
  bool predict(uint64_t pc) override { return ctr_.get(idx(pc)) >= 2; }
  void update(uint64_t pc, bool taken) override { taken ? ctr_.inc(idx(pc)) : ctr_.dec(idx(pc)); }
};
REGISTER_PREDICTOR("mine", Bimodal);
"""

HANGS = b"""#include "predictor.h"
class Hangs : public BranchPredictor {
  Register r_{8, 0, Unsigned, "r"};
 public:
  bool predict(uint64_t) override { while (r_.get() >= 0) r_.set((r_.get() + 1) & 255); return true; }
  void update(uint64_t, bool) override {}
};
REGISTER_PREDICTOR("mine", Hangs);
"""

OVER_BUDGET = b"""#include "predictor.h"
class Big : public BranchPredictor {
  Table t_{1u << 16, 2, 0, Unsigned, "big"};
 public:
  bool predict(uint64_t) override { return true; }
  void update(uint64_t, bool) override {}
};
REGISTER_PREDICTOR("mine", Big);
"""

RULE_BREAKER = b"""#include "predictor.h"
#include <vector>
class Cheat : public BranchPredictor {
  std::vector<int> hist_;
 public:
  bool predict(uint64_t) override { return true; }
  void update(uint64_t, bool) override {}
};
REGISTER_PREDICTOR("mine", Cheat);
"""

SNEAKY_INCLUDE = b"""#include "predictor.h"
#inc\\
lude "/etc/hostname"
class S : public BranchPredictor {
 public:
  bool predict(uint64_t) override { return true; }
  void update(uint64_t, bool) override {}
};
REGISTER_PREDICTOR("mine", S);
"""


# -- an independent reference simulator, to check the server's numbers --


def ref_always(trace):
    return sum(1 for _, t in trace if not t)


def ref_last(trace):
    last, miss = True, 0
    for _, t in trace:
        miss += last != t
        last = t
    return miss


def ref_bimodal(trace):
    ctr, miss = [2] * 1024, 0
    for pc, t in trace:
        i = pc & 1023
        miss += (ctr[i] >= 2) != t
        ctr[i] = min(3, ctr[i] + 1) if t else max(0, ctr[i] - 1)
    return miss


def make_traces(d: str, seed: int = 7) -> dict[str, tuple[int, list]]:
    rng = random.Random(seed)
    out = {}
    for name, n in (("a.trace.gz", 4000), ("b.trace.gz", 3000), ("c.trace", 2500)):
        trace, pcs = [], [0x400000 + 4 * rng.randrange(300) for _ in range(40)]
        for i in range(n):
            pc = rng.choice(pcs)
            bias = (pc >> 2) % 5
            trace.append((pc, rng.random() < (0.1, 0.3, 0.5, 0.8, 0.95)[bias] if i % 7 else i % 2 == 0))
        instr = n * 5 + rng.randrange(1000)
        text = f"# instructions: {instr}\n" + "".join(f"{pc:x} {int(t)}\n" for pc, t in trace)
        path = os.path.join(d, name)
        if name.endswith(".gz"):
            with gzip.open(path, "wt") as fh:
                fh.write(text)
        else:
            with open(path, "w") as fh:
                fh.write(text)
        out[name] = (instr, trace)
    return out


def expected_mpkis(traces, ref) -> list[float]:
    return [round(1000.0 * ref(tr) / instr, 3) for _, (instr, tr) in sorted(traces.items())]


@unittest.skipUnless(shutil.which("g++") and os.path.isdir(HARNESS), "needs g++ and the bpsim harness")
class TestPredictorRooms(ServerCase):
    def setUp(self):
        self.tdir = tempfile.mkdtemp(prefix="mosaic-test-traces-")
        self.traces = make_traces(self.tdir)
        self.harness = bpeval.Harness(HARNESS, self.tdir, run_seconds=4)
        self.srv = web.create_server("127.0.0.1", 0, limits=web.Limits(), harness=self.harness)
        self.port = self.srv.server_address[1]
        import threading

        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.hub = self.srv.hub

    def tearDown(self):
        super().tearDown()
        self.harness.shutdown()
        shutil.rmtree(self.tdir, ignore_errors=True)

    # -- helpers --
    def create_bp(self, name="Part B"):
        status, j, _ = self.req("POST", "/api/rooms", {"name": name, "kind": "predictor", "hours": 2}, {"X-Mosaic": "1"})
        self.assertEqual(status, 201, j)
        self.assertEqual(j["kind"], "predictor")
        self.assertTrue(j["private"])
        return j["code"], j["admin_token"]

    def submit_cpp(self, code, name, src, filename="mine.cpp", **meta):
        status, j, _ = self.submit(code, name, [(filename, src)], **meta)
        self.assertEqual(status, 201, j)
        return j

    def build_status(self, code, j, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, st, _ = self.req("GET", f"/api/rooms/{code}/submit/{j['id']}", headers={"X-Mosaic-Receipt": j["receipt"]})
            self.assertEqual(status, 200, st)
            if st["status"] in ("ready", "failed"):
                return st
            time.sleep(0.1)
        self.fail("build did not finish")

    def evaluate(self, code, tok, timeout=60):
        status, j, _ = self.admin(code, tok, "POST", "run", {})
        self.assertEqual(status, 202, j)
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, st, _ = self.admin(code, tok, "GET", "state")
            if st["analysis"]["status"] in ("done", "error"):
                self.assertEqual(st["analysis"]["status"], "done", st["analysis"])
                return st
            time.sleep(0.1)
        self.fail("evaluation did not finish")

    # -- tests --
    def test_config_and_create(self):
        _, cfg, _ = self.req("GET", "/api/config")
        self.assertTrue(cfg["predictor"]["available"], cfg)
        code, tok = self.create_bp()
        _, info, _ = self.req("GET", f"/api/rooms/{code}/info")
        self.assertEqual(info["kind"], "predictor")
        self.assertIsNone(info["results"])
        _, st, _ = self.admin(code, tok, "GET", "state")
        self.assertEqual(sorted(st["traces"]), sorted(self.traces))

    def test_unavailable_without_harness(self):
        self.hub.harness = None
        status, j, _ = self.req("POST", "/api/rooms", {"name": "x", "kind": "predictor"}, {"X-Mosaic": "1"})
        self.assertEqual(status, 503, j)
        _, cfg, _ = self.req("GET", "/api/config")
        self.assertFalse(cfg["predictor"]["available"])

    def test_aggregates_are_correct_and_individuals_never_appear(self):
        code, tok = self.create_bp()
        subs = {"alice": ALWAYS_TAKEN, "bob": LAST_OUTCOME, "carol": BIMODAL}
        refs = {"alice": ref_always, "bob": ref_last, "carol": ref_bimodal}
        receipts = {}
        for name, src in subs.items():
            j = self.submit_cpp(code, name, src)
            receipts[name] = j
            self.assertNotIn("mpki", json.dumps(j).lower())
        for name, j in receipts.items():
            st = self.build_status(code, j)
            self.assertEqual(st["status"], "ready", st)
            self.assertEqual(set(st), {"status", "messages", "bits"})

        # source code is gone from memory once built
        room = self.hub.get(code)
        for u in room.uploads.values():
            self.assertEqual(u.files, {})
            self.assertTrue(u.binary)

        st = self.evaluate(code, tok)
        sm = st["analysis"]["summary"]
        per_trace = {n: expected_mpkis(self.traces, refs[n]) for n in subs}
        per_sub = {n: statistics.fmean(v) for n, v in per_trace.items()}
        self.assertEqual(sm["evaluated"], 3)
        self.assertEqual(sm["failed"], 0)
        self.assertAlmostEqual(sm["mpki"]["mean"], statistics.fmean(per_sub.values()), places=3)
        self.assertAlmostEqual(sm["mpki"]["median"], statistics.median(per_sub.values()), places=3)
        for j, row in enumerate(sm["per_trace"]):
            vals = [per_trace[n][j] for n in subs]
            self.assertAlmostEqual(row["mean"], statistics.fmean(vals), places=3)
            self.assertAlmostEqual(row["median"], statistics.median(vals), places=3)

        # Every response anyone can get. The median of an odd count is one
        # (anonymous) person's value by definition, so only the others are checked.
        _, info, _ = self.req("GET", f"/api/rooms/{code}/info")
        self.assertEqual(info["results"]["summary"], sm)
        texts = [json.dumps(st), json.dumps(info)]
        for name, j in receipts.items():
            _, b, _ = self.req("GET", f"/api/rooms/{code}/submit/{j['id']}", headers={"X-Mosaic-Receipt": j["receipt"]})
            texts.append(json.dumps(b))
        _, page, _ = self.req("GET", f"/room/{code}")
        texts.append(page.decode())
        blob = "\n".join(texts)
        median = statistics.median(per_sub.values())
        for name, v in per_sub.items():
            if abs(v - median) < 5e-4:
                continue
            for fmt in (f"{v:.3f}", f"{v:.2f}", f"{round(v, 3)}"):
                self.assertNotIn(fmt, blob, f"{name}'s MPKI leaked")
        # no response attaches anything to a name except the admin's name list
        self.assertNotIn("bits", json.dumps(st["uploads"]))
        self.assertNotIn("status", json.dumps(st["uploads"]))

        # the room keeps no individual result anywhere
        def walk(o):
            if isinstance(o, dict):
                for v in o.values():
                    yield from walk(v)
            elif isinstance(o, (list, tuple)):
                for v in o:
                    yield from walk(v)
            else:
                yield o

        stored = [x for x in walk(room.analysis) if isinstance(x, float)]
        for name, v in per_sub.items():
            if abs(v - median) >= 5e-4:
                self.assertFalse(any(abs(x - v) < 5e-4 for x in stored), name)

    def test_build_feedback_goes_only_to_the_submitter(self):
        code, tok = self.create_bp()
        cases = {
            "rules": (RULE_BREAKER, "storage rules"),
            "budget": (OVER_BUDGET, "budget is 65,536"),
            "compile": (ALWAYS_TAKEN.replace(b"return true;", b"return nope;"), "does not compile"),
            "unregistered": (ALWAYS_TAKEN.replace(b'"mine"', b'"other"'), 'Register your predictor as "mine"'),
            "sneaky": (SNEAKY_INCLUDE, "outside the simulator"),
        }
        receipts = {}
        for name, (src, _) in cases.items():
            receipts[name] = self.submit_cpp(code, name, src)
        for name, (_, want) in cases.items():
            st = self.build_status(code, receipts[name])
            self.assertEqual(st["status"], "failed", (name, st))
            self.assertIn(want, "\n".join(st["messages"]), name)
        _, hs, _ = self.admin(code, tok, "GET", "state")
        self.assertEqual(hs["builds"]["failed"], len(cases))
        dumped = json.dumps(hs)
        for name, (_, want) in cases.items():
            self.assertNotIn(want, dumped)
        # wrong receipt: nothing
        j = receipts["rules"]
        status, _, _ = self.req("GET", f"/api/rooms/{code}/submit/{j['id']}", headers={"X-Mosaic-Receipt": "nope"})
        self.assertEqual(status, 404)
        # nothing built: evaluation refuses
        status, _, _ = self.admin(code, tok, "POST", "run", {})
        self.assertEqual(status, 409)

    def test_hanging_predictor_is_killed_and_only_counted(self):
        code, tok = self.create_bp()
        for name, src in (("ok", ALWAYS_TAKEN), ("slow", HANGS)):
            self.assertEqual(self.build_status(code, self.submit_cpp(code, name, src))["status"], "ready")
        t0 = time.time()
        sm = self.evaluate(code, tok, timeout=30)["analysis"]["summary"]
        self.assertLess(time.time() - t0, 20)
        self.assertEqual((sm["evaluated"], sm["failed"]), (1, 1))

    def test_replace_withdraw_and_upload_rules(self):
        code, tok = self.create_bp()
        # a folder with mine.cpp among other files: only mine.cpp is used
        status, j, _ = self.submit(code, "dan", [("predictors/mine.cpp", ALWAYS_TAKEN), ("predictors/gshare.cpp", b"x")])
        self.assertEqual(status, 201, j)
        status, err, _ = self.submit(code, "eve", [("a.cpp", b"x"), ("b.cpp", b"y")])
        self.assertEqual(status, 400)
        self.assertIn("mine.cpp", err["error"])
        # the admin cannot add code to a predictor room
        from test_web import multipart

        ctype, body = multipart({"kind": "submissions"}, [("x/mine.cpp", ALWAYS_TAKEN)])
        status, _, _ = self.admin(code, tok, "POST", "upload", body, {"Content-Type": ctype})
        self.assertEqual(status, 403)

        self.assertEqual(self.build_status(code, j)["status"], "ready")
        j2 = self.submit_cpp(code, "dan", RULE_BREAKER, replace_id=j["id"], receipt=j["receipt"])
        self.assertTrue(j2["replaced"])
        self.assertEqual(self.build_status(code, j2)["status"], "failed")
        status, _, _ = self.req("DELETE", f"/api/rooms/{code}/submit/{j['id']}", None,
                                {"X-Mosaic": "1", "X-Mosaic-Receipt": j["receipt"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.hub.get(code).uploads, {})

    def test_nothing_left_on_disk_and_close_drops_programs(self):
        code, tok = self.create_bp()
        for name, src in (("a", ALWAYS_TAKEN), ("b", LAST_OUTCOME)):
            self.build_status(code, self.submit_cpp(code, name, src))
        self.evaluate(code, tok)
        base = self.harness._base
        leftovers = [os.path.join(dp, f) for dp, _, fs in os.walk(base) for f in fs]
        self.assertEqual([os.path.relpath(p, base) for p in leftovers], [os.path.join("harness", "main.o")])
        room = self.hub.get(code)
        uploads = list(room.uploads.values())
        self.admin(code, tok, "POST", "end", {})
        self.assertTrue(all(u.binary is None for u in uploads))
        self.harness.shutdown()
        self.assertFalse(os.path.exists(base))


if __name__ == "__main__":
    unittest.main()
