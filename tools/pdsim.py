#!/usr/bin/env python3
"""pdsim.py — 确定性的合成物理设计（PD）流程模拟器。

!!! 这是合成环境（synthetic），不是真实 EDA 工具，数值不对应任何真实设计/工艺 !!!

它模仿 AgenticPD（arXiv:2607.04758）里的问题结构，而不是其数值：
  * 4 个阶段 FP -> PL -> CTS -> GR，每个阶段有离散旋钮（knob）；
  * 阶段有依赖：下游 QoR 取决于上游状态（例如高利用率利于时序但让 GR 拥塞）；
  * 候选在 GR 之后评估（"昂贵评估"），DR/签核只对最终 incumbent 运行；
  * 每个阶段输出检查点（JSON），从某阶段分支时可复用上游检查点（零成本）；
  * 账本（ledger）记录每次阶段执行及其成本单位，用来统计昂贵评估次数。

阶段成本（FP 1, PL 4, CTS 2, GR 3, DR 10 个成本单位）是本仓库的**假设**，不是论文数据。

子命令（所有输出写文件，便于 Aura 用 read-file + json-parse 读取）：
  space   --out F                          旋钮空间与默认值
  chain   --design D --parent CKPT|- --from STAGE --knobs JSON --outdir DIR --ledger L --out F
          从 STAGE 开始依次执行到 GR；STAGE 之前的状态取自 parent 检查点（复用，零成本）
  dr      --ckpt CKPT --ledger L --out F    对 GR 检查点做"详细布线+签核"（贵）
  ledger  --glob 'DIR/**/ledger*.jsonl' --out F   汇总账本
"""
import argparse, glob, hashlib, json, math, os, sys

STAGES = ["FP", "PL", "CTS", "GR"]
COST = {"FP": 1, "PL": 4, "CTS": 2, "GR": 3, "DR": 10}   # 假设的成本单位
SPACE = {
    "FP":  {"util": [0.40, 0.50, 0.60, 0.70], "aspect": [0.8, 1.0, 1.25]},
    "PL":  {"density": [0.55, 0.65, 0.75, 0.85], "td": [0, 1]},
    "CTS": {"cluster": [10, 20, 30], "margin": [0.0, 0.05, 0.10]},
    "GR":  {"ladj": [0.1, 0.3, 0.5], "gtd": [0, 1]},
}
DEFAULT = {"FP": {"util": 0.50, "aspect": 1.0}, "PL": {"density": 0.65, "td": 1},
           "CTS": {"cluster": 20, "margin": 0.0}, "GR": {"ladj": 0.3, "gtd": 1}}
# 三个合成设计（名字刻意不用 AES/ibex/JPEG，避免被误读成真实设计）
DESIGNS = {
    "syn-A": dict(cells=12000, cell_area=9000.0, period=1000.0, logic=760.0, wire_k=200.0, cong_k=1.00, p_base=40.0, skew0=28.0),
    "syn-B": dict(cells=20000, cell_area=15000.0, period=1400.0, logic=1120.0, wire_k=250.0, cong_k=1.12, p_base=65.0, skew0=34.0),
    "syn-C": dict(cells=50000, cell_area=40000.0, period=1600.0, logic=1200.0, wire_k=360.0, cong_k=1.25, p_base=120.0, skew0=45.0),
}
ROUTE_FAIL_CONG = 1.10   # GR 拥塞超过它视为布线失败（分支作废）


def noise(*parts):
    """确定性伪噪声 ∈ [-1, 1]，只依赖输入（同一配置永远得到同一结果）。"""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64 * 2 - 1


def run_stage(design, stage, st, kn):
    """st: 上游状态（dict，会被复制）；kn: 本阶段旋钮。返回新状态。"""
    D = DESIGNS[design]; s = dict(st); s["knobs"] = dict(st.get("knobs", {})); s["knobs"][stage] = kn
    pen = dict(st.get("pen", {}))
    key = json.dumps(s["knobs"], sort_keys=True)
    if stage == "FP":
        u, r = kn["util"], kn["aspect"]
        s["die_area"] = D["cell_area"] / u
        s["wl"] = (0.95 + 0.9 * (0.70 - u)) * (1 + 0.18 * abs(math.log(r)))
        s["cong"] = D["cong_k"] * u * (1 + 0.25 * abs(math.log(r))) * (1.0 + 0.10 * noise(design, key, "fpc"))
        pen["FP"] = D["wire_k"] * (s["wl"] - 0.77)
    elif stage == "PL":
        d, t = kn["density"], kn["td"]
        s["wl"] *= (1.18 - 0.32 * d)
        s["cong"] *= (0.55 + 0.75 * d) * (1.06 if t else 1.0)
        s["td"] = t
        s["pl_disp"] = 12.0 * max(0.0, s["cong"] - 0.75) * 10 + 6.0 * abs(noise(design, key, "pl"))
        pen["PL"] = s["pl_disp"] + (0 if t else 0.05 * D["wire_k"])
    elif stage == "CTS":
        k, m = kn["cluster"], kn["margin"]
        s["skew"] = D["skew0"] * math.sqrt(k / 20.0) * (1 + 0.15 * noise(design, key, "cts"))
        s["clk_pwr"] = 0.18 * D["p_base"] * math.sqrt(20.0 / k)
        s["margin"] = m
        s["cong"] *= (1 + 1.5 * m)
        s["die_area"] *= (1 + 0.6 * m)
        pen["CTS"] = s["skew"]
    elif stage == "GR":
        a, g = kn["ladj"], kn["gtd"]
        cap = 1.0 + 0.5 * (0.3 - a)          # layer adjustment 越大预留越多，可用容量越小
        c = s["cong"] / cap
        s["gr_cong"] = c
        s["detour"] = 1.0 + 6.0 * max(0.0, c - 0.65) ** 2
        s["route_fail"] = c > ROUTE_FAIL_CONG
        s["gtd"] = g
        s["dr_risk"] = max(0.0, c - 0.95) * (0.5 - a) * 2.0   # 低预留 + 高拥塞 -> DR 后时序变差
        pen["GR"] = D["wire_k"] * s["wl"] * (s["detour"] - 1.0) * (1 - 0.25 * g)
    s["pen"] = pen
    s["stage"] = stage
    s["qor"] = qor(design, s, key)
    return s


def qor(design, s, key):
    D = DESIGNS[design]
    wl = s.get("wl", 1.0)
    t = s.get("td", 1); g = s.get("gtd", 1); m = s.get("margin", 0.0)
    detour = s.get("detour", 1.0 + 6.0 * max(0.0, s.get("cong", 0.8) - 0.65) ** 2)
    wire = D["wire_k"] * wl * detour * (1 - 0.06 * t - 0.10 * g)
    logic = D["logic"] * (1 - 0.025 * t)
    skew = s.get("skew", D["skew0"])
    delay = logic + wire + skew - m * 0.9 * wire + 8.0 * noise(design, key, "wns")
    wns = D["period"] - delay
    power = D["p_base"] * (0.75 + 0.30 * wl * detour) * (1 + 0.02 * t + 0.01 * g + 0.10 * m) + s.get("clk_pwr", 0.18 * D["p_base"])
    return {"wns": round(wns, 2), "power": round(power, 3), "area": round(s.get("die_area", D["cell_area"] / 0.5), 1),
            "cong": round(s.get("gr_cong", s.get("cong", 0.0)), 4), "route_fail": bool(s.get("route_fail", False))}


def append_ledger(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def cmd_space(a):
    json.dump({"stages": STAGES, "space": SPACE, "default": DEFAULT, "cost": COST, "designs": sorted(DESIGNS)}, open(a.out, "w"))


def cmd_chain(a):
    knobs = json.loads(a.knobs) if not a.knobs.startswith("@") else json.load(open(a.knobs[1:]))
    st = {} if a.parent in ("-", "") else json.load(open(a.parent))
    i0 = STAGES.index(a.frm)
    if i0 > 0 and st.get("stage") is None:
        sys.exit("chain: --from %s 需要 parent 检查点" % a.frm)
    if i0 > 0 and STAGES.index(st["stage"]) < i0 - 1:
        sys.exit("chain: parent 检查点阶段太早")
    if i0 > 0:   # 只保留 STAGES[:i0] 的上游状态：把 parent 截断到 i0-1 阶段的检查点
        anc = st.get("ancestors", {})
        want = STAGES[i0 - 1]
        if st["stage"] != want:
            st = json.load(open(anc[want]))
    os.makedirs(a.outdir, exist_ok=True)
    nodes = []
    for sname in STAGES[i0:]:
        kn = knobs.get(sname, DEFAULT[sname])
        anc = dict(st.get("ancestors", {}))
        st = run_stage(a.design, sname, st, kn)
        path = os.path.join(a.outdir, "%s.json" % sname)
        st["ancestors"] = dict(anc, **{sname: path})
        st["design"] = a.design
        json.dump(st, open(path, "w"), sort_keys=True)
        append_ledger(a.ledger, {"design": a.design, "stage": sname, "cost": COST[sname], "ckpt": path})
        nodes.append({"stage": sname, "ckpt": path, "knobs": kn, "qor": st["qor"], "pen": st["pen"]})
        if st["qor"]["route_fail"]:
            break
    out = {"design": a.design, "from": a.frm, "nodes": nodes, "reused": STAGES[:i0],
           "final": nodes[-1]["qor"], "complete": nodes[-1]["stage"] == "GR" and not nodes[-1]["qor"]["route_fail"],
           "knobs": st["knobs"]}
    json.dump(out, open(a.out, "w"), sort_keys=True)


def cmd_dr(a):
    st = json.load(open(a.ckpt))
    assert st["stage"] == "GR", "DR 只能在 GR 检查点上运行"
    key = json.dumps(st["knobs"], sort_keys=True)
    q = dict(st["qor"])
    q["wns"] = round(q["wns"] - 120.0 * st["dr_risk"] + 5.0 * noise(key, "dr"), 2)
    q["signoff"] = True
    append_ledger(a.ledger, {"stage": "DR", "cost": COST["DR"], "ckpt": a.ckpt})
    json.dump(q, open(a.out, "w"), sort_keys=True)


def cmd_ledger(a):
    tot = {"runs": 0, "cost": 0, "by_stage": {s: 0 for s in STAGES + ["DR"]}}
    for p in sorted(glob.glob(a.glob, recursive=True)):
        for line in open(p):
            r = json.loads(line); tot["runs"] += 1; tot["cost"] += r["cost"]; tot["by_stage"][r["stage"]] += 1
    tot["gr_evals"] = tot["by_stage"]["GR"]
    json.dump(tot, open(a.out, "w"), sort_keys=True)


def main():
    p = argparse.ArgumentParser(); sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("space"); s.add_argument("--out", required=True)
    s = sp.add_parser("chain")
    for k in ("design", "parent", "knobs", "outdir", "ledger", "out"): s.add_argument("--" + k, required=True)
    s.add_argument("--from", dest="frm", required=True, choices=STAGES)
    s = sp.add_parser("dr"); [s.add_argument("--" + k, required=True) for k in ("ckpt", "ledger", "out")]
    s = sp.add_parser("ledger"); [s.add_argument("--" + k, required=True) for k in ("glob", "out")]
    a = p.parse_args()
    {"space": cmd_space, "chain": cmd_chain, "dr": cmd_dr, "ledger": cmd_ledger}[a.cmd](a)


if __name__ == "__main__":
    main()
