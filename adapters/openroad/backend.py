#!/usr/bin/env python3
"""OpenROAD-flow-scripts（ORFS）真实后端：实现与 tools/pdsim.py 相同的 PD_TOOL CLI 契约。

  PD_TOOL=adapters/openroad/backend.py scripts/aura.sh aura/pd-main.aura

  space  --out F
  chain  --design D --parent CKPT|- --from FP|PL|CTS|GR --knobs idx:a,b,c,d --outdir DIR --ledger L --out F
  dr     --ckpt GR_CKPT --ledger L --out F          （详细布线 + finish 报告，只对 incumbent 调用）
  ledger --glob G --out F

真实执行：
  * 官方镜像 openroad/orfs（ORFS_IMAGE），常驻容器 ORFS_CONTAINER，宿主机 ORFS_WORK 映射到容器 /work。
  * 每个阶段是一次独立的 ORFS make：FP=do-floorplan，PL=do-place，CTS=do-cts，GR=do-5_1_grt；
    DR=do-5_2_route do-5_3_fillcell do-5_route do-5_route.sdc do-finish。每阶段 .odb 留在分支自己的 WORK_HOME。
  * 分支复用：从 parent 的 WORK_HOME 复制 --from 之前各阶段的 results/logs/reports（.odb 即检查点），
    只重跑 --from 及之后的阶段。
  * 旋钮"归属阶段"：运行阶段 s 时只传归属于 <= s 的阶段的旋钮，其余保持 ORFS 默认。
    因此"复用上游 + 重跑下游"与"同配置从头全跑"得到同一个输入，二者应给出同样结果（tests 会核对）。
  * 结果缓存：键 = (阶段, 所有 <= 该阶段的旋钮)。命中时复制已完成的 WORK_HOME 快照，不再调用 ORFS；
    账本里记 cached=true，cost 仍记原始测得的耗时（即该方法本应付出的代价），wall 记实际耗时。
    这依赖 ORFS 对同一输入是确定性的（tests/test_orfs.py 会核对）。
  * 一次只跑一个 flow（文件锁），线程数 NUM_CORES（默认 4）。
"""
import argparse, fcntl, glob, hashlib, itertools, json, os, shutil, subprocess, sys, time

WORK = os.environ.get("ORFS_WORK", "/workspace/orfs-work")
IMAGE = os.environ.get("ORFS_IMAGE", "openroad/orfs:latest")
CONTAINER = os.environ.get("ORFS_CONTAINER", "orfs-eda")
CORES = os.environ.get("ORFS_CORES", "4")
DESIGNS = {"gcd-nangate45": "designs/nangate45/gcd/config.mk"}
PLAT_DESIGN = {"gcd-nangate45": ("nangate45", "gcd")}

STAGES = ["FP", "PL", "CTS", "GR"]
STAGE_NUM = {"FP": 2, "PL": 3, "CTS": 4, "GR": 5}
# 每个旋钮 -> (ORFS 变量, 取值)；0 / None 表示"不传，用 ORFS 默认"
SPACE = {
    "FP":  {"util": [35, 45, 55, 65], "aspect": [0.7, 1.0, 1.4]},
    "PL":  {"density": [0.0, 0.1, 0.2, 0.3], "td": [0, 1]},
    "CTS": {"cluster": [0, 10, 20, 40], "margin": [0.0, 0.01, 0.03]},
    "GR":  {"incr": [0, 1], "opt": [0, 1]},
}
# gcd/nangate45 的 ORFS 官方默认（config.mk：CORE_UTILIZATION 55，PLACE_DENSITY_LB_ADDON 0.20；其余为 ORFS 默认）
DEFAULT = {"FP": {"util": 55, "aspect": 1.0}, "PL": {"density": 0.2, "td": 1},
           "CTS": {"cluster": 0, "margin": 0.0}, "GR": {"incr": 1, "opt": 1}}
VARS = {
    "FP":  lambda k: {"CORE_UTILIZATION": k["util"], "CORE_ASPECT_RATIO": k["aspect"]},
    "PL":  lambda k: {"PLACE_DENSITY_LB_ADDON": k["density"], "GPL_TIMING_DRIVEN": k["td"]},
    "CTS": lambda k: dict(({"CTS_CLUSTER_SIZE": k["cluster"]} if k["cluster"] else {}),
                          **({"SETUP_SLACK_MARGIN": k["margin"]} if k["margin"] else {})),
    # incr=1 表示做 GR 后增量修复（ORFS 默认 SKIP_INCREMENTAL_REPAIR=0）；opt=1 表示 OPT_POST_GRT_WNS=1（默认）
    "GR":  lambda k: {"SKIP_INCREMENTAL_REPAIR": 0 if k["incr"] else 1, "OPT_POST_GRT_WNS": k["opt"]},
}
TARGETS = {"FP": ["do-floorplan"], "PL": ["do-place"], "CTS": ["do-cts"], "GR": ["do-5_1_grt"],
           "DR": ["do-5_2_route", "do-5_3_fillcell", "do-5_route", "do-5_route.sdc", "do-finish"]}
METRIC = {"FP": ("2_1_floorplan", "floorplan"), "PL": ("3_5_place_dp", "detailedplace"),
          "CTS": ("4_1_cts", "cts"), "GR": ("5_1_grt", "globalroute"), "DR": ("6_report", "finish")}


def combos(stage):
    ks = sorted(SPACE[stage])
    return [dict(zip(ks, v)) for v in itertools.product(*[SPACE[stage][k] for k in ks])]


def neighbors(stage):
    cs = combos(stage); ks = sorted(SPACE[stage]); out = []
    for c in cs:
        nb = []
        for j, d in enumerate(cs):
            diff = [k for k in ks if c[k] != d[k]]
            if len(diff) == 1 and abs(SPACE[stage][diff[0]].index(c[diff[0]]) - SPACE[stage][diff[0]].index(d[diff[0]])) == 1:
                nb.append(j)
        out.append(nb)
    return out


def env_for(knobs, upto):
    """运行阶段 upto 时传给 ORFS 的变量：只含归属阶段 <= upto 的旋钮。upto='DR' 表示全部。"""
    n = len(STAGES) if upto == "DR" else STAGES.index(upto) + 1
    env = {}
    for s in STAGES[:n]:
        env.update(VARS[s](knobs[s]))
    return env


def cache_key(design, knobs, upto):
    n = len(STAGES) if upto == "DR" else STAGES.index(upto) + 1
    return hashlib.sha256(json.dumps([design, upto, {s: knobs[s] for s in STAGES[:n]}], sort_keys=True).encode()).hexdigest()[:20]


def cpath(host):   # 宿主机路径 -> 容器路径
    host = os.path.abspath(host)
    assert host.startswith(WORK + "/"), host
    return "/work" + host[len(WORK):]


def ensure_container():
    r = subprocess.run(["sudo", "-n", "docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True, text=True)
    if r.stdout.strip() == "true":
        return
    subprocess.run(["sudo", "-n", "docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(["sudo", "-n", "docker", "run", "-d", "--name", CONTAINER, "-u", "%d:%d" % (os.getuid(), os.getgid()),
                    "-e", "HOME=/tmp", "-v", "%s:/work" % WORK, "-w", "/OpenROAD-flow-scripts/flow", IMAGE,
                    "sleep", "infinity"], check=True, capture_output=True)


def make(design, work_home, targets, env, log):
    """在容器里跑一次 ORFS make（持锁：一次只跑一个 flow）。返回 (ok, 秒)。"""
    ensure_container()
    vs = " ".join("%s=%s" % (k, v) for k, v in sorted(env.items()))
    cmd = ("source ../env.sh >/dev/null 2>&1; make DESIGN_CONFIG=%s WORK_HOME=%s NUM_CORES=%s %s %s"
           % (DESIGNS[design], cpath(work_home), CORES, vs, " ".join(targets)))
    os.makedirs(WORK, exist_ok=True)
    with open(os.path.join(WORK, ".flow.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        t0 = time.time()
        with open(log, "a") as f:
            f.write("$ %s\n" % cmd); f.flush()
            r = subprocess.run(["sudo", "-n", "docker", "exec", CONTAINER, "bash", "-c", cmd], stdout=f, stderr=subprocess.STDOUT)
        return r.returncode == 0, time.time() - t0


def sub(work_home, design, kind):
    p, d = PLAT_DESIGN[design]
    return os.path.join(work_home, kind, p, d, "base")


def metrics(work_home, design, stage):
    f, pre = METRIC[stage]
    p = os.path.join(sub(work_home, design, "logs"), f + ".json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    g = lambda k, dflt=None: d.get("%s__%s" % (pre, k), dflt)
    q = {"ws_ns": g("timing__setup__ws"), "tns_ns": g("timing__setup__tns"),
         "power_w": g("power__total"), "area_um2": g("design__instance__area")}
    if stage == "GR":
        q["overflow"] = d.get("globalroute__global_route__fastroute__overflow_iterations_s")
        q["gr_wirelength"] = d.get("globalroute__global_route__wirelength")
    return q


def ensure_synth(design):
    """综合只做一次（不调旋钮），作为所有分支的根。"""
    base = os.path.join(WORK, "base", design)
    if os.path.exists(os.path.join(sub(base, design, "results"), "1_synth.odb")):
        return base
    os.makedirs(base, exist_ok=True)
    ok, sec = make(design, base, ["synth"], {}, os.path.join(base, "synth.log"))
    if not ok:
        sys.exit("synth 失败，见 %s/synth.log" % base)
    json.dump({"seconds": sec}, open(os.path.join(base, "synth.time.json"), "w"))
    return base


NONDIGIT = {"mem.json": 1, "synth_check.txt": 1, "synth_stat.txt": 1, "route.guide": 5, "grt_antennas.log": 5,
            "drt_antennas.log": 6, "cts_default_core_clock.webp": 4, "cts_default_core_clock_layout.webp": 4}


def stage_of(fn):
    if fn[:1].isdigit():
        return int(fn[0])
    if fn in NONDIGIT:
        return NONDIGIT[fn]
    return 6 if fn.startswith("final") else 0


def copy_upto(src, dst, design, keep_upto_num):
    """把 src WORK_HOME 中阶段号 <= keep_upto_num 的文件放到 dst（.odb 即检查点）。

    用硬链接节省磁盘，并把文件设为只读：下游阶段只会新建更高阶段号的文件，
    万一 ORFS 试图改写上游文件会直接报错而不是悄悄污染其它分支。"""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    for kind in ("results", "logs", "reports", "objects"):
        s, d = sub(src, design, kind), sub(dst, design, kind)
        os.makedirs(d, exist_ok=True)
        if not os.path.isdir(s):
            continue
        for fn in os.listdir(s):
            sp, dp = os.path.join(s, fn), os.path.join(d, fn)
            if kind != "objects" and stage_of(fn) > keep_upto_num:
                continue
            if os.path.isdir(sp) or kind == "objects":
                (shutil.copytree if os.path.isdir(sp) else shutil.copy2)(sp, dp)
                continue
            os.chmod(sp, 0o444)
            try:
                os.link(sp, dp)
            except OSError:
                shutil.copy2(sp, dp)


def run_or_cache(design, knobs, stage, work_home, log):
    """在 work_home（已含上游）上执行 stage；命中缓存则直接取快照。返回 (ok, cost_s, wall_s, cached)。"""
    key = cache_key(design, knobs, stage)
    cdir = os.path.join(WORK, "cache", key)
    meta = os.path.join(cdir, "meta.json")
    t0 = time.time()
    if os.path.exists(meta) and os.environ.get("ORFS_NOCACHE") != "1":
        m = json.load(open(meta))
        copy_upto(os.path.join(cdir, "wh"), work_home, design, 9)
        return m["ok"], m["seconds"], time.time() - t0, True
    ok, sec = make(design, work_home, TARGETS[stage], env_for(knobs, stage), log)
    if os.environ.get("ORFS_NOCACHE") == "1":
        return ok, sec, time.time() - t0, False
    os.makedirs(cdir, exist_ok=True)
    copy_upto(work_home, os.path.join(cdir, "wh"), design, 9)
    json.dump({"ok": ok, "seconds": sec, "stage": stage, "knobs": knobs, "design": design}, open(meta, "w"), sort_keys=True)
    return ok, sec, time.time() - t0, False


def append_ledger(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def cmd_space(a):
    json.dump({"stages": STAGES, "space": SPACE, "default": DEFAULT, "designs": sorted(DESIGNS),
               "cost": "measured-seconds", "backend": "orfs", "image": IMAGE,
               "combos": {s: combos(s) for s in STAGES}, "neighbors": {s: neighbors(s) for s in STAGES},
               "default_idx": [combos(s).index(DEFAULT[s]) for s in STAGES]}, open(a.out, "w"), sort_keys=True)


def cmd_chain(a):
    idx = [int(x) for x in a.knobs[4:].split(",")]
    knobs = {s: combos(s)[i] for s, i in zip(STAGES, idx)}
    i0 = STAGES.index(a.frm)
    outdir = os.path.abspath(a.outdir); os.makedirs(outdir, exist_ok=True)
    wh = os.path.join(WORK, "w", hashlib.sha256(outdir.encode()).hexdigest()[:16])
    log = os.path.join(outdir, "orfs.log")
    if i0 == 0:
        copy_upto(ensure_synth(a.design), wh, a.design, 1)
        anc = {}
    else:
        par = json.load(open(a.parent))
        anc = dict(par["ancestors"]); want = STAGES[i0 - 1]
        src = json.load(open(anc[want]))["work"]
        copy_upto(src, wh, a.design, STAGE_NUM[want])
        anc = {s: anc[s] for s in STAGES[:i0]}
    nodes = []; pen = {}
    if i0 > 0:   # 上游阶段的瓶颈值从父检查点继承
        pen = dict(json.load(open(anc[STAGES[i0 - 1]])).get("pen", {}))
    complete = False
    for s in STAGES[i0:]:
        ok, cost, wall, cached = run_or_cache(a.design, knobs, s, wh, log)
        m = metrics(wh, a.design, s) if ok else None
        path = os.path.join(outdir, "%s.json" % s)
        anc[s] = path
        if m and m["ws_ns"] is not None:
            pen[s] = round(-1000.0 * m["ws_ns"], 3)   # 阶段 s 结束时的 setup WNS 损失（ps，越小越好）
        q = {"wns": round(1000.0 * m["ws_ns"], 3) if m and m["ws_ns"] is not None else -1e9,
             "tns": round(1000.0 * m["tns_ns"], 3) if m and m["tns_ns"] is not None else None,
             "power": round(1000.0 * m["power_w"], 5) if m and m["power_w"] is not None else None,
             "area": m["area_um2"] if m else None, "cong": 0.0, "route_fail": not ok}
        st = {"stage": s, "design": a.design, "knobs": knobs, "work": wh, "ancestors": dict(anc), "pen": dict(pen),
              "qor": q, "metrics": m}
        snap = os.path.join(WORK, "ck", hashlib.sha256((outdir + s).encode()).hexdigest()[:16])
        copy_upto(wh, snap, a.design, STAGE_NUM[s])   # 每阶段检查点各自一份：后代从这里复用
        st["work"] = snap
        json.dump(st, open(path, "w"), sort_keys=True)
        append_ledger(a.ledger, {"design": a.design, "stage": s, "cost": round(cost, 3), "wall": round(wall, 3),
                                 "cached": cached, "ok": ok, "ckpt": path, "reused": STAGES[:i0]})
        nodes.append({"stage": s, "ckpt": path, "knobs": knobs[s], "qor": q, "pen": dict(pen)})
        if not ok:
            break
        complete = s == "GR"
    shutil.rmtree(wh, ignore_errors=True)
    json.dump({"design": a.design, "from": a.frm, "nodes": nodes, "reused": STAGES[:i0], "final": nodes[-1]["qor"],
               "complete": complete, "knobs": knobs}, open(a.out, "w"), sort_keys=True)


def cmd_dr(a):
    st = json.load(open(a.ckpt)); assert st["stage"] == "GR"
    design = st["design"]
    wh = os.path.join(WORK, "w", "dr-" + hashlib.sha256(os.path.abspath(a.ckpt).encode()).hexdigest()[:16])
    copy_upto(st["work"], wh, design, 5)
    ok, cost, wall, cached = run_or_cache(design, st["knobs"], "DR", wh, os.path.abspath(a.out) + ".orfs.log")
    m = metrics(wh, design, "DR") if ok else None
    drj = os.path.join(sub(wh, design, "logs"), "5_2_route.json")
    drc = json.load(open(drj)).get("detailedroute__route__drc_errors") if os.path.exists(drj) else None
    q = {"wns": round(1000.0 * m["ws_ns"], 3) if m else -1e9, "tns": round(1000.0 * m["tns_ns"], 3) if m else None,
         "power": round(1000.0 * m["power_w"], 5) if m else None, "area": m["area_um2"] if m else None,
         "drc": drc, "signoff": ok, "die_area": None}
    append_ledger(a.ledger, {"stage": "DR", "cost": round(cost, 3), "wall": round(wall, 3), "cached": cached, "ok": ok, "ckpt": a.ckpt})
    shutil.rmtree(wh, ignore_errors=True)
    json.dump(q, open(a.out, "w"), sort_keys=True)


def cmd_ledger(a):
    tot = {"runs": 0, "cost": 0.0, "wall": 0.0, "cached": 0, "by_stage": {s: 0 for s in STAGES + ["DR"]}}
    for p in sorted(glob.glob(a.glob, recursive=True)):
        for line in open(p):
            r = json.loads(line)
            tot["runs"] += 1; tot["cost"] += r["cost"]; tot["wall"] += r.get("wall", 0); tot["cached"] += int(r.get("cached", False))
            tot["by_stage"][r["stage"]] += 1
    tot["cost"] = round(tot["cost"], 2); tot["wall"] = round(tot["wall"], 2)
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
