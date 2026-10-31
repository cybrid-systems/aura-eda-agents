#!/usr/bin/env python3
"""汇总真实 ORFS 实验。
无参数：Phase 2 gcd（v1）：results/pd_runs_orfs.tsv + orfs_ledgers/ + orfs_meta.json → summary_orfs.md
--tag aes：results/pd_runs_orfs_aes.tsv + orfs_ledgers_aes/ + orfs_meta_aes.json → summary_orfs_aes.md"""
import argparse, csv, glob, json, os, statistics as st
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT)
ap = argparse.ArgumentParser(); ap.add_argument("--tag", default=""); T = ap.parse_args().tag; sfx = f"_{T}" if T else ""
R = list(csv.DictReader(open(f"results/pd_runs_orfs{sfx}.tsv"), delimiter="\t"))
meta = json.load(open(f"results/orfs_meta{sfx}.json"))
m = lambda xs: st.mean(xs) if xs else float("nan")
methods = ["default", "random", "autotuner", "greedy", "judge", "judge-norerun", "llm-judge"]
names = {"default": "ORFS 默认配置（1 次评估）", "random": "扁平随机搜索", "autotuner": "类 AutoTuner 扁平局部搜索",
         "greedy": "最大瓶颈贪心 Judge（复用）", "judge": "历史感知 Judge（复用）", "judge-norerun": "同一 Judge，不复用（全量重跑）", "llm-judge": "LLM Judge（复用）"}
seeds = sorted({r["seed"] for r in R})
out = [f"# 真实 ORFS 结果汇总（{meta['design']}）", "",
       f"**真实 OpenROAD-flow-scripts 运行**：设计 `{meta['design']}`，平台 `{meta['platform']}`，镜像 `{meta['image_digest']}`"
       f"（{meta['image_created']}），Yosys {meta['yosys']}，NUM_CORES={meta['num_cores']}。SDC 时钟周期 {meta['clock_period_ns']} ns，未修改。"
       + (f" 旋钮空间 {meta['space']}。" if meta.get("space") else ""), "",
       f"种子 {', '.join(seeds)}；每次运行预算 {meta['budget']} 次到 GR 的候选评估，DR+finish 只对 incumbent 跑一次。"
       "WNS 单位 ps，越大越好。成本 = 账本中各阶段实测秒数之和（缓存命中时记原始实测秒数），含 DR。", "",
       "| 方法 | " + " | ".join(f"种子 {s} GR WNS" for s in seeds) + " | 平均 GR WNS | 平均 finish WNS | 平均功耗 mW | 平均面积 µm² | 平均阶段执行数 | 平均成本 s | 失败分支 |",
       "|---|" + "---|" * (len(seeds) + 7)]
for mt in methods:
    X = [r for r in R if r["method"] == mt]
    if not X: continue
    per = [next((float(r["gr_wns"]) for r in X if r["seed"] == s), float("nan")) for s in seeds]
    out.append(f"| {names[mt]} | " + " | ".join(f"{v:.3f}" for v in per) +
               f" | {m([float(r['gr_wns']) for r in X]):.3f} | {m([float(r['dr_wns']) for r in X]):.3f} | {m([float(r['power']) for r in X]):.4f}"
               f" | {m([float(r['area']) for r in X]):.1f} | {m([float(r['stage_runs']) for r in X]):.1f} | {m([float(r['cost']) for r in X]):.0f} | {m([float(r['fails']) for r in X]):.2f} |")
J = [r for r in R if r["method"] == "judge"]
if J:
    hist = [sum(int(r[f"branch_{s}"]) for r in J) for s in ("FP", "PL", "CTS", "GR")]; tot = sum(hist) or 1
    out += ["", f"历史感知 Judge 的分支阶段分布：FP {hist[0]/tot:.0%}，PL {hist[1]/tot:.0%}，CTS {hist[2]/tot:.0%}，GR {hist[3]/tot:.0%}。"]
L = [json.loads(l) for p in sorted(glob.glob(f"results/orfs_ledgers{sfx}/*.jsonl")) for l in open(p)]
real = [x for x in L if not x.get("cached")]
out += ["", "## 计算量（整个实验）", "",
        f"- 逻辑阶段执行（各方法账本之和，含 DR）：{len(L)} 次，合计记账成本 {sum(x['cost'] for x in L)/60:.1f} 分钟。",
        f"- 其中真正调用 ORFS 的：{len(real)} 次，实测 {sum(x['cost'] for x in real)/60:.1f} 分钟；"
        f"其余 {len(L)-len(real)} 次命中结果缓存（同一阶段同一上游旋钮，已由 tests/test_orfs.py 验证确定性）。",
        f"- 实验总墙钟：{meta['wall_minutes']:.1f} 分钟（{meta['start']} → {meta['end']}，UTC+8）。", ""]
dw, bw = meta.get("default_gr_wns_ps"), meta["best_seen_gr_wns_ps"]
nb = meta["configs_better_than_default"]
out += ["## 搜索到的配置", "",
        f"- 所有方法、所有种子合计评估了 {meta['unique_complete_configs']} 个不同的完整配置（到 GR 成功），另有 {meta['unique_failing_configs']} 个配置失败。",
        (f"- 最好的 GR WNS 是 {bw} ps，就是 ORFS 默认值；**没有任何配置优于默认**。" if not nb else
         f"- ORFS 默认 GR WNS {dw} ps；见过的最好 GR WNS {bw} ps，**{nb} 个不同配置优于默认**。")
        + f"中位数 {meta['median_seen_gr_wns_ps']} ps，最差 {meta['worst_seen_gr_wns_ps']} ps。"]
if meta.get("best_seen_knobs") and nb: out.append(f"- 最好配置的旋钮：`{json.dumps(meta['best_seen_knobs'], ensure_ascii=False, sort_keys=True)}`")
out += [f"- {x}" for x in meta.get("notes", [])] + [""]
open(f"results/summary_orfs{sfx}.md", "w").write("\n".join(out) + "\n")
print("\n".join(out))
