#!/usr/bin/env python3
"""合成环境 + 独立验证器的单元测试（只用标准库）。python3 tests/test_envs.py"""
import json, os, subprocess, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
def run(*a): subprocess.run([PY] + list(a), cwd=ROOT, check=True)
def J(p): return json.load(open(p))

class PD(unittest.TestCase):
    def test_deterministic_and_reuse(self):
        t = tempfile.mkdtemp()
        for i in (1, 2):
            run("tools/pdsim.py", "chain", "--design", "syn-B", "--parent", "-", "--from", "FP", "--knobs", "idx:2,1,4,0",
                "--outdir", f"{t}/f{i}", "--ledger", f"{t}/l{i}.jsonl", "--out", f"{t}/r{i}.json")
        self.assertEqual(J(f"{t}/r1.json")["final"], J(f"{t}/r2.json")["final"])
        # 从 CTS 分支（复用 FP/PL）与全量重跑同一配置：QoR 相同、复用的阶段不进账本
        run("tools/pdsim.py", "chain", "--design", "syn-B", "--parent", f"{t}/f1/GR.json", "--from", "CTS", "--knobs", "idx:2,1,7,0",
            "--outdir", f"{t}/b", "--ledger", f"{t}/lb.jsonl", "--out", f"{t}/rb.json")
        run("tools/pdsim.py", "chain", "--design", "syn-B", "--parent", "-", "--from", "FP", "--knobs", "idx:2,1,7,0",
            "--outdir", f"{t}/c", "--ledger", f"{t}/lc.jsonl", "--out", f"{t}/rc.json")
        self.assertEqual(J(f"{t}/rb.json")["final"], J(f"{t}/rc.json")["final"])
        self.assertEqual(len(open(f"{t}/lb.jsonl").readlines()), 2)
        self.assertEqual(len(open(f"{t}/lc.jsonl").readlines()), 4)

class ECO(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(); run("tools/eco3d.py", "gen", "--design", "eco-S", "--seed", "2", "--out", f"{self.t}/c.json")
    def verify(self, after, allowed="*", target=""):
        args = ["tools/eco3d.py", "verify", "--before", f"{self.t}/c.json", "--after", after, "--allowed", allowed, "--out", f"{self.t}/v.json"]
        if target: args += ["--target-file", target]
        run(*args); return J(f"{self.t}/v.json")
    def test_clock_change_rejected(self):
        c = J(f"{self.t}/c.json"); clk = [n for n in c["hbt"] if c["nets"][n]["clock"]][0]
        occ = {tuple(p) for p in c["hbt"].values()}; blk = {tuple(b) for b in c["blocked"]}
        x, y = next((x, y) for x in range(c["W"]) for y in range(c["H"]) if (x, y) not in occ and (x, y) not in blk)
        c["hbt"][clk] = [x, y]; json.dump(c, open(f"{self.t}/a.json", "w"))
        v = self.verify(f"{self.t}/a.json"); self.assertFalse(v["gates"]["preservation"]); self.assertFalse(v["pass"])
    def test_undeclared_change_rejected(self):
        c = J(f"{self.t}/c.json"); n = sorted(m for m in c["hbt"] if not c["nets"][m]["clock"])[0]
        c["hbt"][n] = [c["hbt"][n][0], c["hbt"][n][1]]; c["hbt"][n][0] = (c["hbt"][n][0] + 20) % c["W"]
        json.dump(c, open(f"{self.t}/a.json", "w")); json.dump(["someone-else"], open(f"{self.t}/al.json", "w"))
        v = self.verify(f"{self.t}/a.json", allowed=f"@{self.t}/al.json"); self.assertFalse(v["gates"]["preservation"])
    def test_structural_change_rejected(self):
        c = J(f"{self.t}/c.json"); n = sorted(c["nets"])[10]; c["nets"][n]["pins"] = c["nets"][n]["pins"][:1] + c["nets"][n]["pins"]
        json.dump(c, open(f"{self.t}/a.json", "w")); v = self.verify(f"{self.t}/a.json"); self.assertFalse(v["gates"]["structural_eq"])
    def test_noop_passes_with_empty_target(self):
        json.dump([], open(f"{self.t}/t.json", "w")); v = self.verify(f"{self.t}/c.json", target=f"{self.t}/t.json")
        self.assertTrue(v["pass"]); self.assertEqual(v["delta_nets"], 0)

class Ledger(unittest.TestCase):
    def test_tamper_detected(self):
        import hashlib; t = tempfile.mkdtemp(); prev = "0" * 64; lines = []
        for i, (e, d) in enumerate([("observe", "a"), ("decide", "COMMIT x")]):
            h = hashlib.sha256(f"{prev}|{i}|{e}|{d}".encode()).hexdigest(); lines.append(json.dumps({"seq": i, "event": e, "data": d, "prev": prev, "hash": h})); prev = h
        open(f"{t}/l.jsonl", "w").write("\n".join(lines) + "\n")
        self.assertEqual(subprocess.run([PY, "tools/ledger_check.py", f"{t}/l.jsonl"], cwd=ROOT, capture_output=True).returncode, 0)
        open(f"{t}/l.jsonl", "w").write("\n".join(lines).replace("COMMIT x", "COMMIT y") + "\n")
        self.assertEqual(subprocess.run([PY, "tools/ledger_check.py", f"{t}/l.jsonl"], cwd=ROOT, capture_output=True).returncode, 1)

if __name__ == "__main__":
    unittest.main()
