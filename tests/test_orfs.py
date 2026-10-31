#!/usr/bin/env python3
"""真实 ORFS 后端的两项核对（需要 docker 与 openroad/orfs 镜像，约 2 分钟；不可用时跳过）。

1. 确定性：同一配置从头跑两次（不走缓存），四个阶段的 WNS/TNS/功耗/面积逐位相同。
2. 复用等价：从默认父分支的 PL 检查点在 CTS 处分叉 == 同配置从头全跑。
两者成立，结果缓存与检查点复用才不会改变搜索看到的数值。
"""
import json, os, subprocess, sys, tempfile
B = os.path.join(os.path.dirname(__file__), "..", "adapters", "openroad", "backend.py")
D = "gcd-nangate45"


def chain(tmp, name, knobs, parent="-", frm="FP", nocache=True):
    env = dict(os.environ, ORFS_NOCACHE="1" if nocache else "0")
    subprocess.run([sys.executable, B, "chain", "--design", D, "--parent", parent, "--from", frm, "--knobs", "idx:" + knobs,
                    "--outdir", f"{tmp}/{name}", "--ledger", f"{tmp}/{name}.ledger.jsonl", "--out", f"{tmp}/{name}.json"],
                   check=True, env=env)
    d = json.load(open(f"{tmp}/{name}.json"))
    return {n["stage"]: (n["qor"]["wns"], n["qor"]["tns"], n["qor"]["power"], n["qor"]["area"]) for n in d["nodes"]}


def main():
    if subprocess.run(["sudo", "-n", "docker", "image", "inspect", "openroad/orfs:latest"], capture_output=True).returncode:
        print("SKIP: 没有 docker 或 openroad/orfs 镜像"); return
    tmp = tempfile.mkdtemp(prefix="orfs-test-")
    a = chain(tmp, "a", "6,5,0,3"); b = chain(tmp, "b", "6,5,0,3")
    assert a == b, ("不确定", a, b)
    c = chain(tmp, "c", "6,5,7,1", parent=f"{tmp}/a/PL.json", frm="CTS")
    full = chain(tmp, "d", "6,5,7,1")
    assert c == {s: full[s] for s in ("CTS", "GR")}, ("复用不等价", c, full)
    print("ORFS-OK default GR", a["GR"], "branch GR", c["GR"])


if __name__ == "__main__":
    main()
