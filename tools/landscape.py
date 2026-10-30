#!/usr/bin/env python3
"""穷举合成 PD 空间（5184 个配置/设计），给出最优、默认配置排名等——仅用于校验模拟器与结果解读。"""
import itertools, json, sys, os
sys.path.insert(0, os.path.dirname(__file__))
import pdsim

def all_cfgs():
    per = []
    for s in pdsim.STAGES:
        ks = sorted(pdsim.SPACE[s]); per.append([dict(zip(ks, v)) for v in itertools.product(*[pdsim.SPACE[s][k] for k in ks])])
    for combo in itertools.product(*per):
        yield dict(zip(pdsim.STAGES, combo))

def evaluate(design, cfg):
    st = {}
    for s in pdsim.STAGES:
        st = pdsim.run_stage(design, s, st, cfg[s])
        if st["qor"]["route_fail"]: return None
    return st["qor"]["wns"]

if __name__ == "__main__":
    out = {}
    for d in sorted(pdsim.DESIGNS):
        vals = [(evaluate(d, c), c) for c in all_cfgs()]
        ok = sorted([v for v, c in vals if v is not None], reverse=True)
        dv = evaluate(d, pdsim.DEFAULT)
        best = max((v, json.dumps(c, sort_keys=True)) for v, c in vals if v is not None)
        out[d] = {"n": len(vals), "fail": len(vals) - len(ok), "best": ok[0], "p90": ok[len(ok)//10], "median": ok[len(ok)//2],
                  "default": dv, "default_rank": sum(1 for v in ok if v > dv) + 1, "best_cfg": json.loads(best[1])}
    json.dump(out, open(sys.argv[1] if len(sys.argv) > 1 else "/dev/stdout", "w"), indent=1)
