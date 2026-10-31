#!/usr/bin/env python3
"""实验结束后收集真实 ORFS 结果（runs/ 不入库）：
复制账本 → results/orfs_ledgers_<tag>/，统计所有方法/种子见过的完整配置 → 合并进 results/orfs_meta_<tag>.json。
用法：python3 tools/orfs_collect.py --design aes-nangate45 --tag aes [--start "2026-10-31 10:20:00" --end ...]"""
import argparse, glob, json, os, shutil, statistics as st, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT)
ap = argparse.ArgumentParser(); ap.add_argument("--design", required=True); ap.add_argument("--tag", required=True)
ap.add_argument("--start"); ap.add_argument("--end"); a = ap.parse_args()
led = f"results/orfs_ledgers_{a.tag}"; os.makedirs(led, exist_ok=True)
for d in sorted(glob.glob(f"runs/pd/{a.design}/*/*/")):
    seed, method = d.rstrip("/").split("/")[-2:]
    with open(f"{led}/s{seed}_{method}.jsonl", "w") as f:
        for p in sorted(glob.glob(d + "b*.ledger.jsonl"), key=lambda p: int(os.path.basename(p)[1:].split(".")[0])):
            f.write(open(p).read())
        if os.path.exists(d + "dr.ledger.jsonl"): f.write(open(d + "dr.ledger.jsonl").read())
ok, bad = {}, set()
for p in glob.glob(f"runs/pd/{a.design}/*/*/b*.json"):
    b = json.load(open(p)); k = json.dumps(b["knobs"], sort_keys=True)
    if b.get("complete"): ok[k] = b["final"]["wns"]
    else: bad.add(k)
dflt = [json.load(open(p))["final"]["wns"] for p in glob.glob(f"runs/pd/{a.design}/*/default/b0.json")]
dw = dflt[0] if dflt else None
vals = sorted(ok.values())
best_k = max(ok, key=ok.get) if ok else None
mp = f"results/orfs_meta_{a.tag}.json"; meta = json.load(open(mp)) if os.path.exists(mp) else {}
meta.update({"unique_complete_configs": len(ok), "unique_failing_configs": len(bad - set(ok)),
             "default_gr_wns_ps": dw, "best_seen_gr_wns_ps": vals[-1] if vals else None, "best_seen_knobs": json.loads(best_k) if best_k else None,
             "configs_better_than_default": sum(v > dw for v in vals) if dw is not None else None,
             "median_seen_gr_wns_ps": st.median(vals) if vals else None, "worst_seen_gr_wns_ps": vals[0] if vals else None})
if a.start: meta["start"] = a.start
if a.end: meta["end"] = a.end
if meta.get("start") and meta.get("end"):
    f = "%Y-%m-%d %H:%M:%S"; meta["wall_minutes"] = round((dt.datetime.strptime(meta["end"], f) - dt.datetime.strptime(meta["start"], f)).total_seconds() / 60, 2)
json.dump(meta, open(mp, "w"), ensure_ascii=False, indent=1); print(json.dumps(meta, ensure_ascii=False, indent=1))
