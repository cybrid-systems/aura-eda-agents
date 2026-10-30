#!/usr/bin/env python3
"""eco3d.py — 确定性的合成 3D-IC（两层混合键合）ECO 环境 + 独立验证器。

!!! 合成环境（synthetic）：几何单位是"格点"，不是 µm；不对应任何真实设计/工艺 !!!

问题结构模仿 AgenticECO（arXiv:2608.03738），数值不来自论文：
  * 两层 tier，跨层线网需要一个混合键合端子 HBT，HBT 落在键合格点上；
  * 规则：任意两个 HBT 的欧氏距离 < sB 即为 DRC 违例（cut-spacing 类缺陷）；
  * 初始"布线器"把 HBT 放到离引脚质心最近的空闲格点，忽略间距规则 -> 在热点区产生自然缺陷；
  * 时钟线网受保护：任何修复都不应改动；
  * Δnet = 指纹（HBT 位置）发生变化的线网数。

子命令（输出都写文件）：
  gen     --design D --seed S --out CKPT                    生成一个案例（已"布线"的检查点）
  query   --ckpt C --kind netlist|layout_vpix|hb_graph|drc [--nets a,b] --out F   只读查询（专家用）
  apply   --ckpt C --program G.json --out CKPT2 --report F  执行确定性修复程序 g（EcoRoute / move / tuple / spread）
  fullreroute --ckpt C --out CKPT2                          基线：全量拆线重布（顺序相关，包括时钟线网）
  stockrepair --ckpt C --out CKPT2                          基线：逐对把违例 HBT 挪到最近合法点（不看时钟/时序）
  verify  --before C --after C2 --allowed a,b|@file|* --target-file F --out V   独立验证器（只看产物）
  stats   --before C --after C2 --out F                     DRC/Δnet/时钟线网统计（报告用）
"""
import argparse, hashlib, json, math, random, sys

DESIGNS = {  # 合成设计：格点尺寸、单元数、线网数、热点
    "eco-S": dict(W=40, H=40, cells=260, nets=300, hot=0.15, clock=4, r=5),
    "eco-M": dict(W=52, H=52, cells=420, nets=520, hot=0.14, clock=6, r=5),
    "eco-L": dict(W=64, H=64, cells=640, nets=780, hot=0.13, clock=8, r=5),
}
SB = 1.5          # 间距规则（格点单位）：相邻(1)与对角(1.414)都违例


def sha(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def load(p): return json.load(open(p))
def dump(o, p): json.dump(o, open(p, "w"), sort_keys=True)


# ---------- 几何与指标 ----------
def dist(a, b): return math.hypot(a[0] - b[0], a[1] - b[1])


def violations(ck):
    """返回违例对列表 [netA, netB]（排序、去重）。"""
    items = sorted(ck["hbt"].items())
    grid = {}
    for n, p in items: grid.setdefault((p[0], p[1]), []).append(n)
    out = set()
    for n, p in items:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m in grid.get((p[0] + dx, p[1] + dy), []):
                    if m != n and dist(p, ck["hbt"][m]) < SB:
                        out.add(tuple(sorted((n, m))))
    return sorted([list(v) for v in out])


def net_wl(ck, n):
    pins = [ck["cells"][c] for c in ck["nets"][n]["pins"]]
    if n in ck["hbt"]:
        h = ck["hbt"][n]; return sum(abs(p[1] - h[0]) + abs(p[2] - h[1]) for p in pins)
    xs = [p[1] for p in pins]; ys = [p[2] for p in pins]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def net_delay(ck, n): return 1.0 + 0.08 * net_wl(ck, n) + (0.5 if n in ck["hbt"] else 0.0)


def timing(ck):
    ds = [sum(net_delay(ck, n) for n in path) for path in ck["paths"]]
    return {"max_delay": round(max(ds), 4), "min_delay": round(min(ds), 4),
            "wns": round(ck["period"] - max(ds), 4), "hold_slack": round(min(ds) - ck["hold"], 4)}


def path_slack(ck):
    """每个线网所在最差路径的 slack（给 netlist 专家用）。"""
    s = {}
    for path in ck["paths"]:
        sl = ck["period"] - sum(net_delay(ck, n) for n in path)
        for n in path: s[n] = min(s.get(n, 1e9), sl)
    return s


def structure(ck):  # 结构等价：单元与连接关系（不含 HBT 位置）
    return sha({"cells": ck["cells"], "nets": ck["nets"]})


def fingerprints(ck):
    return {n: sha([n, ck["hbt"].get(n), ck["nets"][n]["pins"]])[:16] for n in ck["nets"]}


def changed_nets(a, b):
    fa, fb = fingerprints(a), fingerprints(b)
    return sorted(n for n in fa if fa[n] != fb.get(n))


# ---------- 生成 ----------
def nearest_free(ck, target, occupied, ok=lambda p: True, rmax=None):
    W, H = ck["W"], ck["H"]; tx, ty = target
    best = None
    R = rmax if rmax is not None else max(W, H)
    for r in range(0, R + 1):
        for x in range(tx - r, tx + r + 1):
            for y in range(ty - r, ty + r + 1):
                if max(abs(x - tx), abs(y - ty)) != r or not (0 <= x < W and 0 <= y < H): continue
                p = (x, y)
                if p in occupied or p in ck["_blocked"] or not ok(p): continue
                d = abs(x - tx) + abs(y - ty)
                if best is None or (d, p) < best[0]: best = ((d, p), p)
        if best is not None: return list(best[1])
    return None


def centroid(ck, n):
    pins = [ck["cells"][c] for c in ck["nets"][n]["pins"]]
    return (round(sum(p[1] for p in pins) / len(pins)), round(sum(p[2] for p in pins) / len(pins)))


def prep(ck):
    ck["_blocked"] = set(map(tuple, ck["blocked"])); return ck


def strip(ck):
    ck = dict(ck); ck.pop("_blocked", None); return ck


def cmd_gen(a):
    D = DESIGNS[a.design]; rng = random.Random("%s/%s" % (a.design, a.seed)); W, H = D["W"], D["H"]
    hx, hy = rng.randint(W // 4, 3 * W // 4), rng.randint(H // 4, 3 * H // 4)
    cells = {}
    for i in range(D["cells"]):
        if rng.random() < D["hot"]:
            x = min(W - 1, max(0, int(rng.gauss(hx, W / 7)))); y = min(H - 1, max(0, int(rng.gauss(hy, H / 7))))
        else:
            x, y = rng.randrange(W), rng.randrange(H)
        cells["c%d" % i] = [rng.randrange(2), x, y]
    names = sorted(cells, key=lambda c: int(c[1:]))
    nets = {}
    for i in range(D["nets"]):
        k = rng.choice([2, 2, 3, 3, 4]); c0 = rng.choice(names); x0, y0 = cells[c0][1], cells[c0][2]
        near = sorted(names, key=lambda c: (abs(cells[c][1] - x0) + abs(cells[c][2] - y0) + rng.random() * 6, c))[:12]
        pins = sorted(set([c0] + rng.sample(near, k - 1)), key=lambda c: int(c[1:]))
        nets["n%d" % i] = {"pins": pins, "clock": i < D["clock"]}
        if len({cells[c][0] for c in pins}) < 2:  # 一半概率强制跨层
            if rng.random() < 0.6:
                c = pins[-1]; cells[c] = [1 - cells[c][0]] + cells[c][1:]
    for n in nets:
        nets[n]["pins"] = nets[n]["pins"]
    blocked = sorted({(rng.randrange(W), rng.randrange(H)) for _ in range(W * H // 25)})
    ck = {"design": a.design, "seed": a.seed, "W": W, "H": H, "sB": SB, "cells": cells, "nets": nets,
          "blocked": [list(b) for b in blocked], "hbt": {}}
    prep(ck)
    occ = set()
    for n in sorted(nets, key=lambda s: int(s[1:])):   # 间距感知，但只在小半径内找；找不到就近放 -> 热点区残留自然缺陷
        if len({cells[c][0] for c in nets[n]["pins"]}) == 2:
            ok = lambda p: all(dist(p, q) >= SB for q in occ)
            p = nearest_free(ck, centroid(ck, n), occ, ok, rmax=D["r"]) or nearest_free(ck, centroid(ck, n), occ)
            ck["hbt"][n] = p; occ.add(tuple(p))
    # 时序路径：随机串联的线网链
    nl = sorted(nets, key=lambda s: int(s[1:])); ck["paths"] = [sorted(rng.sample(nl, 6), key=lambda s: int(s[1:])) for _ in range(len(nl) // 6)]
    ck["period"] = 0; ck["hold"] = 0
    t = timing(ck); ck["period"] = round(t["max_delay"] * 1.02, 4); ck["hold"] = round(t["min_delay"] * 0.85, 4)
    dump(strip(ck), a.out)


# ---------- 只读查询（专家） ----------
def cmd_query(a):
    ck = prep(load(a.ckpt)); viol = violations(ck); nets = a.nets.split(",") if a.nets else sorted({n for v in viol for n in v})
    if a.kind == "drc":
        data = {"violations": viol, "count": len(viol)}
    elif a.kind == "netlist":
        sl = path_slack(ck)
        data = {n: {"clock": ck["nets"][n]["clock"], "pins": ck["nets"][n]["pins"], "fanout": len(ck["nets"][n]["pins"]) - 1,
                    "slack": round(sl.get(n, 1e9), 4) if n in sl else None, "wl": net_wl(ck, n)} for n in nets}
    elif a.kind == "hb_graph":
        adj = {}
        for x, y in viol: adj.setdefault(x, set()).add(y); adj.setdefault(y, set()).add(x)
        comps, seen = [], set()
        for n in sorted(adj):
            if n in seen: continue
            st, comp = [n], []
            while st:
                m = st.pop()
                if m in seen: continue
                seen.add(m); comp.append(m); st.extend(adj[m] - seen)
            comps.append(sorted(comp))
        data = {"degree": {n: len(adj[n]) for n in sorted(adj)}, "components": comps}
    elif a.kind == "layout_vpix":
        occ = {tuple(p) for p in ck["hbt"].values()}
        data = {}
        for n in nets:
            if n not in ck["hbt"]: continue
            others = [p for m, p in ck["hbt"].items() if m != n]
            cand = []
            h = ck["hbt"][n]
            for x in range(h[0] - 8, h[0] + 9):
                for y in range(h[1] - 8, h[1] + 9):
                    p = (x, y)
                    if not (0 <= x < ck["W"] and 0 <= y < ck["H"]) or p in ck["_blocked"] or (p in occ and list(p) != h): continue
                    if any(dist(p, q) < SB for q in others): continue
                    old = ck["hbt"][n]; ck["hbt"][n] = list(p); dwl = net_wl(ck, n); ck["hbt"][n] = old
                    cand.append({"site": [x, y], "wl": dwl})
            cand.sort(key=lambda c: (c["wl"], c["site"]))
            data[n] = {"hbt": h, "legal_sites": cand[:6], "wl_now": net_wl(ck, n)}
    else:
        sys.exit("unknown kind")
    dump({"kind": a.kind, "ckpt_sha256": hashlib.sha256(open(a.ckpt, "rb").read()).hexdigest(), "nets": nets, "data": data}, a.out)


# ---------- 修复程序执行器 ----------
def place_free(ck, n, others_fn, rmax):
    """把 n 的 HBT 放到 rmax 内：不与其它 HBT 违例、线长最小的格点。找不到返回 None。"""
    h = ck["hbt"][n]; occ = {tuple(p) for m, p in ck["hbt"].items() if m != n}
    others = others_fn(n); best = None
    for x in range(h[0] - rmax, h[0] + rmax + 1):
        for y in range(h[1] - rmax, h[1] + rmax + 1):
            p = (x, y)
            if not (0 <= x < ck["W"] and 0 <= y < ck["H"]) or p in occ or p in ck["_blocked"]: continue
            if any(dist(p, q) < SB for q in others): continue
            old = ck["hbt"][n]; ck["hbt"][n] = list(p); key = (net_wl(ck, n), abs(x - h[0]) + abs(y - h[1]), p); ck["hbt"][n] = old
            if best is None or key < best: best = key
    return list(best[2]) if best else None


def ecoroute(ck, nets, k):
    """EcoRoute：只重布 nets（时钟永不纳入），其余冻结；k 为升级级数（扩大搜索半径并纳入邻居）。"""
    R = sorted({n for n in nets if n in ck["hbt"] and not ck["nets"][n]["clock"]})
    if k > 0:  # 升级：纳入距离 R 中 HBT ≤ (SB + k) 的非时钟邻居
        ext = set(R)
        for n in R:
            for m, p in ck["hbt"].items():
                if not ck["nets"][m]["clock"] and dist(p, ck["hbt"][n]) <= SB + k: ext.add(m)
        R = sorted(ext)
    Rs = set(R); placed_set = set()
    frozen = lambda n: [p for m, p in ck["hbt"].items() if m != n and (m not in Rs or m in placed_set)]
    sl = path_slack(ck)
    for n in sorted(R, key=lambda n: (sl.get(n, 1e9), n)):   # 时序关键的先放
        p = place_free(ck, n, frozen, 3 + 2 * k)
        if p: ck["hbt"][n] = p
        placed_set.add(n)
    return R


def cmd_apply(a):
    ck = prep(load(a.ckpt)); g = json.load(open(a.program)); fam = g["family"]
    if fam == "reroute":
        R = ecoroute(ck, g["nets"], int(g.get("k", 0)))
    elif fam in ("move", "tuple"):
        moves = g["moves"]; R = sorted({m[0] for m in moves})
        for n, site in moves: ck["hbt"][n] = list(site)
    elif fam == "spread":
        x0, y0, x1, y1 = g["window"]
        R = sorted(n for n, p in ck["hbt"].items() if x0 <= p[0] <= x1 and y0 <= p[1] <= y1 and not ck["nets"][n]["clock"])
        for n in R: ck["hbt"][n] = [-99, -99]
        for n in R:
            ck["hbt"][n] = centroid(ck, n) and list(centroid(ck, n))
            p = place_free(ck, n, lambda m: [q for o, q in ck["hbt"].items() if o != m and q[0] >= 0], 5)
            ck["hbt"][n] = p if p else nearest_free(ck, centroid(ck, n), {tuple(q) for o, q in ck["hbt"].items() if o != n})
    else:
        sys.exit("unknown family")
    out = strip(ck); dump(out, a.out)
    dump({"family": fam, "declared": R, "changed": changed_nets(load(a.ckpt), out)}, a.report)


def cmd_fullreroute(a):
    """基线：全部 HBT 拆掉，按线网名字典序（顺序相关）重布，与生成器同样的间距感知半径，找不到就近放。包括时钟。"""
    ck = prep(load(a.ckpt)); order = sorted(ck["hbt"])
    ck["hbt"] = {}
    for n in order:
        ck["hbt"][n] = list(centroid(ck, n))
        occ = {tuple(p) for m, p in ck["hbt"].items() if m != n}
        p = place_free(ck, n, lambda m: [q for o, q in ck["hbt"].items() if o != m], DESIGNS[ck["design"]]["r"])
        ck["hbt"][n] = p if p else nearest_free(ck, centroid(ck, n), occ)
    dump(strip(ck), a.out)


def cmd_stockrepair(a):
    """基线：逐个违例对，把字典序较大的那个 HBT（不管是不是时钟）挪到半径 3 内线长最小的合法点。单遍。"""
    ck = prep(load(a.ckpt))
    for x, y in violations(ck):
        if dist(ck["hbt"][x], ck["hbt"][y]) >= SB: continue
        p = place_free(ck, y, lambda m: [q for o, q in ck["hbt"].items() if o != m], 3)
        if p: ck["hbt"][y] = p
    dump(strip(ck), a.out)


# ---------- 独立验证器 ----------
def cmd_verify(a):
    b, c = prep(load(a.before)), prep(load(a.after))
    vb, vc = violations(b), violations(c)
    tb, tc = timing(b), timing(c)
    chg = changed_nets(b, c)
    if a.allowed == "*": allowed = None
    elif a.allowed.startswith("@"): allowed = set(json.load(open(a.allowed[1:])))
    else: allowed = set(filter(None, a.allowed.split(",")))
    target = [tuple(v) for v in json.load(open(a.target_file))] if a.target_file else [tuple(v) for v in vb]
    occ = [tuple(p) for p in c["hbt"].values()]
    gates = {
        "routing": all(c["hbt"].get(n) is not None and 0 <= c["hbt"][n][0] < c["W"] and 0 <= c["hbt"][n][1] < c["H"] and tuple(c["hbt"][n]) not in c["_blocked"]
                       for n in b["hbt"]) and len(occ) == len(set(occ)) and set(c["hbt"]) == set(b["hbt"]),
        "drc_no_new": set(map(tuple, vc)) <= set(map(tuple, vb)),
        "drc_target_cleared": not (set(target) & set(map(tuple, vc))),
        "timing_max": tc["wns"] >= min(tb["wns"], 0.0) - 1e-9,
        "timing_min": tc["hold_slack"] >= min(tb["hold_slack"], 0.0) - 1e-9,
        "structural_eq": structure(b) == structure(c),
        "preservation": (allowed is None or set(chg) <= allowed) and not any(b["nets"][n]["clock"] for n in chg),
    }
    res = {"pass": all(gates.values()), "gates": gates, "drc_before": len(vb), "drc_after": len(vc), "delta_nets": len(chg),
           "changed": chg, "clock_changed": [n for n in chg if b["nets"][n]["clock"]], "timing_before": tb, "timing_after": tc,
           "before_sha256": hashlib.sha256(open(a.before, "rb").read()).hexdigest(),
           "after_sha256": hashlib.sha256(open(a.after, "rb").read()).hexdigest()}
    dump(res, a.out)


def cmd_stats(a):
    b, c = prep(load(a.before)), prep(load(a.after)); chg = changed_nets(b, c)
    dump({"drc_before": len(violations(b)), "drc_after": len(violations(c)), "delta_nets": len(chg), "nets": len(b["nets"]),
          "routed_nets": len(b["hbt"]), "clock_changed": sum(1 for n in chg if b["nets"][n]["clock"]),
          "timing_before": timing(b), "timing_after": timing(c), "structural_eq": structure(b) == structure(c)}, a.out)


def main():
    p = argparse.ArgumentParser(); sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("gen"); s.add_argument("--design", required=True, choices=sorted(DESIGNS)); s.add_argument("--seed", type=int, required=True); s.add_argument("--out", required=True)
    s = sp.add_parser("query"); s.add_argument("--ckpt", required=True); s.add_argument("--kind", required=True); s.add_argument("--nets", default=""); s.add_argument("--out", required=True)
    s = sp.add_parser("apply"); [s.add_argument("--" + k, required=True) for k in ("ckpt", "program", "out", "report")]
    for name in ("fullreroute", "stockrepair"):
        s = sp.add_parser(name); s.add_argument("--ckpt", required=True); s.add_argument("--out", required=True)
    s = sp.add_parser("verify"); [s.add_argument("--" + k, required=True) for k in ("before", "after", "out")]
    s.add_argument("--allowed", default="*"); s.add_argument("--target-file", dest="target_file", default="")
    s = sp.add_parser("stats"); [s.add_argument("--" + k, required=True) for k in ("before", "after", "out")]
    a = p.parse_args()
    {"gen": cmd_gen, "query": cmd_query, "apply": cmd_apply, "fullreroute": cmd_fullreroute, "stockrepair": cmd_stockrepair,
     "verify": cmd_verify, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
