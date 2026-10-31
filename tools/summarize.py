#!/usr/bin/env python3
"""把 results/*.tsv 汇总成 results/summary.md（合成数据）。"""
import csv, json, os, statistics as st, subprocess, sys
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT)
def rows(p): return list(csv.DictReader(open(p), delimiter="\t"))
def m(xs): return st.mean(xs) if xs else float("nan")
out = ["# 结果汇总（全部为合成环境数据，不代表真实 EDA 结果）", ""]

# ---------- PD ----------
pd = rows("results/pd_runs.tsv")
land = json.loads(subprocess.run([sys.executable, "tools/landscape.py"], capture_output=True, text=True, check=True).stdout)
methods = ["default", "random", "autotuner", "greedy", "judge", "judge-norerun"]
names = {"default": "默认配置（1 次评估）", "random": "扁平随机搜索", "autotuner": "AutoTuner 式扁平局部搜索", "greedy": "最大瓶颈贪心 Judge（复用）",
         "judge": "历史感知 Judge（复用）", "judge-norerun": "同一 Judge，不复用（全量重跑）"}
designs = sorted({r["design"] for r in pd}); seeds = sorted({r["seed"] for r in pd})
out += ["## PD：阶段感知树搜索（合成 pdsim）", "",
        f"设计 {', '.join(designs)} × 种子 {', '.join(seeds)}；每次运行预算 20 次昂贵评估（到 GR 的完整候选），DR 只对 incumbent 跑一次。",
        "成本单位按假设的阶段成本（FP 1, PL 4, CTS 2, GR 3, DR 10）从账本累加。穷举最优 = 对 5184 个配置穷举得到的最佳 GR WNS（只作参照，不计成本）。", ""]
out += ["| 方法 | " + " | ".join(f"{d} GR WNS" for d in designs) + " | 平均 GR WNS | 平均 DR WNS | 平均阶段执行数 | 平均成本单位 | 布线失败分支/运行 |",
        "|---|" + "---|" * (len(designs) + 5)]
for mth in methods:
    R = [r for r in pd if r["method"] == mth]
    per = [m([float(r["gr_wns"]) for r in R if r["design"] == d]) for d in designs]
    out.append(f"| {names[mth]} | " + " | ".join(f"{x:.2f}" for x in per) +
               f" | {m([float(r['gr_wns']) for r in R]):.2f} | {m([float(r['dr_wns']) for r in R]):.2f} | {m([float(r['stage_runs']) for r in R]):.1f} | {m([float(r['cost']) for r in R]):.1f} | {m([float(r['fails']) for r in R]):.2f} |")
out.append("| 穷举最优（参照） | " + " | ".join(f"{land[d]['best']:.2f}" for d in designs) + f" | {m([land[d]['best'] for d in designs]):.2f} | - | - | - | - |")
J = [r for r in pd if r["method"] == "judge"]
hist = [sum(int(r[f"branch_{s}"]) for r in J) for s in ("FP", "PL", "CTS", "GR")]; tot = sum(hist)
out += ["", f"历史感知 Judge 的分支阶段分布：FP {hist[0]/tot:.1%}，PL {hist[1]/tot:.1%}，CTS {hist[2]/tot:.1%}，GR {hist[3]/tot:.1%}；"
        f"复用上游检查点的分支占 {(tot-hist[0])/tot:.1%}（从 FP 分支无可复用）。",
        f"复用 vs 全量重跑：incumbent QoR 完全相同（{sum(1 for a in J for b in pd if b['method']=='judge-norerun' and a['design']==b['design'] and a['seed']==b['seed'] and a['gr_wns']==b['gr_wns'])}/{len(J)} 次运行），"
        f"平均成本 {m([float(r['cost']) for r in J]):.1f} vs {m([float(r['cost']) for r in pd if r['method']=='judge-norerun']):.1f} 单位，"
        f"阶段执行 {m([float(r['stage_runs']) for r in J]):.1f} vs {m([float(r['stage_runs']) for r in pd if r['method']=='judge-norerun']):.1f}。",
        f"overlay 一致性：全部 {len(pd)} 次运行里，全局 pd-incumbent（只能由胜出分支 overlay-commit 改写）都等于 incumbent 检查点：{all(r['overlay_incumbent_ok']=='#t' for r in pd)}。", ""]

# ---------- 演化 ----------
if os.path.exists("results/evolve_audit.tsv"):
    A = rows("results/evolve_audit.tsv"); H = rows("results/evolve_holdout.tsv")
    out += ["## Judge 策略自演化（演化门禁）", "", "训练种子 101–103、3 个设计；保留种子 201–205。门禁：训练集平均 GR WNS 提升 ≥0.5 ps，且失败分支不增、平均成本升幅 ≤5%；否则 `ast:restore`。", "",
            "| 步 | 候选 | 训练 WNS 前→后 | 成本 前→后 | 决定 | 原因 |", "|---|---|---|---|---|---|"]
    for r in A:
        out.append(f"| {r['step']} | {r['change']} | {r['wns_before']}→{r['wns_after']} | {r['cost_before']}→{r['cost_after']} | {r['decision']} | {r['reason']} |")
    out += ["", "| 保留集 | 策略 | 平均 GR WNS | 平均成本 |", "|---|---|---|---|"]
    for r in H: out.append(f"| {r['policy']} | `{r['src']}` | {r['mean_gr_wns']} | {r['mean_cost']} |")
    out.append("")

# ---------- ECO ----------
if os.path.exists("results/eco_runs.tsv"):
    E = rows("results/eco_runs.tsv"); ms = []
    for r in E:
        if r["method"] not in ms: ms.append(r["method"])
    en = {"none": "不修", "fullreroute": "全量拆线重布", "stockrepair": "库存式逐对修复", "agent": "证据门控 Agent（Aura）",
          "agent-no-ecoroute": "消融：无 EcoRoute", "agent-no-hbgraph": "消融：无 hb_graph 专家", "agent-no-revise": "消融：无 Revise"}
    ncase = len({r["case"] for r in E})
    out += ["## ECO：证据门控修复（合成 eco3d）", "", f"{ncase} 个合成案例；Δnet% = 指纹变化的线网数 / 有 HBT 的线网数；‘清零’= 该案例 DRC 变为 0。", "",
            "| 方法 | DRC 总数 前→后 | 降幅 | 清零案例 | 平均 Δnet%（全部） | 平均 Δnet%（清零案例） | 改动时钟线网 | 时序门通过 | 结构等价 | 平均执行器调用 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for mth in ms:
        R = [r for r in E if r["method"] == mth]
        b = sum(int(r["drc_before"]) for r in R); a = sum(int(r["drc_after"]) for r in R)
        pct = [100.0 * int(r["delta_nets"]) / int(r["routed_nets"]) for r in R]
        pc = [p for p, r in zip(pct, R) if r["cleared"] == "1"]
        out.append(f"| {en.get(mth, mth)} | {b}→{a} | {100.0*(b-a)/b:.1f}% | {sum(int(r['cleared']) for r in R)}/{len(R)} | {m(pct):.2f}% | "
                   f"{(f'{m(pc):.2f}%' if pc else '-')} | {sum(int(r['clock_changed']) for r in R)} | {sum(int(r['timing_ok']) for r in R)}/{len(R)} | "
                   f"{sum(int(r['structural_eq']) for r in R)}/{len(R)} | {m([float(r['exec_calls']) for r in R]):.1f} |")
    ag = [r for r in E if r["method"].startswith("agent")]
    out += ["", f"Agent 运行的哈希链账本全部通过独立复核：{all(r['ledger_ok']=='#t' for r in ag)}；共享状态 eco-current 只经由 overlay-commit 改写且与最终检查点一致：{all(r['overlay_current_ok']=='#t' for r in ag)}。", ""]
    out += ["### 逐案例（证据门控 Agent vs 全量重布）", "", "| 案例 | DRC 前 | Agent 后 | Agent Δnet | 全量重布 后 | 全量重布 Δnet | 全量重布 改时钟 |", "|---|---|---|---|---|---|---|"]
    for c in sorted({r["case"] for r in E}, key=lambda s: (s[:5], int(s.split("-")[-1]))):
        g = {r["method"]: r for r in E if r["case"] == c}
        if "agent" in g and "fullreroute" in g:
            out.append(f"| {c} | {g['agent']['drc_before']} | {g['agent']['drc_after']} | {g['agent']['delta_nets']}/{g['agent']['routed_nets']} | "
                       f"{g['fullreroute']['drc_after']} | {g['fullreroute']['delta_nets']}/{g['fullreroute']['routed_nets']} | {g['fullreroute']['clock_changed']} |")
open("results/summary.md", "w").write("\n".join(out) + "\n")
print("\n".join(out))
