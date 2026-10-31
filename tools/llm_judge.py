#!/usr/bin/env python3
"""可插拔的 LLM Judge（AgenticPD 中"选哪个节点、从哪个阶段分支、改成什么设置"的那一步）。

Aura 侧（aura/pd.aura 的 llm-judge 方法）每轮调用一次：

  llm_judge.py select --dir RUN_DIR --space SPACE_JSON --nodes "id:parent:b:i0,i1,i2,i3:fail;..." \
                      --width W --budget-left K --out OUT_JSON

本工具从 RUN_DIR/b<id>.json 读取各节点的真实 QoR 与逐阶段损失 pen，组织成提示词交给 LLM，
校验返回的 JSON，并写出 {"picks": [{"node": id, "b": stage_idx, "idx": [..4..]}], "source": ..., "fallback": bool}。
任何错误（无 key、网络、解析、非法选择）都不会让搜索中断：返回空 picks，由 Aura 回退到确定性 Judge，并记 fallback。
每次调用的提示词哈希、原始回复、耗时、token 用量追加到 RUN_DIR/llm_calls.jsonl（不含 key）。

提供方（LLM_JUDGE_PROVIDER）：
  mock     确定性、离线：选 incumbent 的最大瓶颈阶段，取一步邻居。只用于打通管线，不代表 LLM。
  minimax  MiniMax，OpenAI 兼容接口。key 从环境变量 MINIMAX_API_KEY 读取；可选 MINIMAX_BASE_URL
           （默认 https://api.minimax.io/v1，中国区 https://api.minimax.cn/v1）、MINIMAX_MODEL（默认 MiniMax-M3.1-Flash）。
新增提供方：在 PROVIDERS 里加一项（OpenAI 兼容的只需 base_url / key 环境变量名 / 模型名）。

安全：真实网络调用必须显式设置 LLM_JUDGE_ALLOW_NETWORK=1；key 只从环境变量读取，永不写入日志、输出或报错信息。
"""
import argparse, hashlib, json, os, re, sys, time, urllib.request

STAGES = ["FP", "PL", "CTS", "GR"]
PROVIDERS = {
    "minimax": {"kind": "openai", "base_url_env": "MINIMAX_BASE_URL", "base_url": "https://api.minimax.io/v1",
                "key_env": "MINIMAX_API_KEY", "model_env": "MINIMAX_MODEL", "model": "MiniMax-M3.1-Flash"},
    "mock": {"kind": "mock"},
}

SYSTEM = """你是物理设计（OpenROAD 布局布线）流程的 Judge。流程分 FP→PL→CTS→GR 四个阶段，每个阶段有一组可选设置。
搜索树中每个节点是一套完整设置及其真实 QoR。你要选出若干 (节点, 分支阶段, 新设置)：
从该节点在"分支阶段"处改设置并重跑下游，上游阶段的检查点直接复用（分支阶段越靠后越便宜）。
目标：在评估预算内最大化 GR 后的 setup WNS（ps，越大越好）。pen[s] 是该节点在阶段 s 结束时的时序损失（ps，越小越好），
gap[s] 是它与全树该阶段最好值之差，可用来定位瓶颈阶段。不要重复已评估过的完整设置。
只输出 JSON：{"picks":[{"node":<id>,"stage":"FP|PL|CTS|GR","settings":{...该阶段全部旋钮...},"reason":"<一句话>"}]}"""


def load_nodes(a):
    nodes = []
    for part in filter(None, a.nodes.split(";")):
        i, p, b, idx, fail = part.split(":")
        n = {"id": int(i), "parent": int(p), "b": int(b), "idx": [int(x) for x in idx.split(",")], "fail": fail == "1"}
        f = os.path.join(a.dir, "b%s.json" % i)
        if os.path.exists(f):
            r = json.load(open(f))
            pen = (r.get("nodes") or [{}])[-1].get("pen", {})
            n["pen"] = [pen.get(s) for s in STAGES]
            n["qor"] = r.get("final") or {}
        nodes.append(n)
    return nodes


def build_prompt(nodes, space, width, left):
    ok = [n for n in nodes if not n["fail"] and n.get("pen") and None not in n["pen"]]
    best = [min(n["pen"][s] for n in ok) for s in range(4)] if ok else [0] * 4
    combos = space["combos"]
    lines = []
    for n in nodes:
        st = {s: combos[s][n["idx"][k]] for k, s in enumerate(STAGES)}
        if n["fail"] or not n.get("pen") or None in n["pen"]:
            lines.append({"node": n["id"], "parent": n["parent"], "settings": st, "status": "流程失败" if n["fail"] else "无数据"})
            continue
        q = n.get("qor", {})
        lines.append({"node": n["id"], "parent": n["parent"], "branched_at": STAGES[n["b"]], "settings": st,
                      "wns_ps": q.get("wns"), "tns_ps": q.get("tns"), "power_mw": q.get("power"), "area_um2": q.get("area"),
                      "pen": dict(zip(STAGES, n["pen"])),
                      "gap": {s: round(n["pen"][k] - best[k], 3) for k, s in enumerate(STAGES)}})
    user = {"可选设置（每阶段的旋钮与取值）": space["space"], "默认设置": space["default"],
            "已评估节点": lines, "本轮最多选": width, "剩余评估预算": left}
    return json.dumps(user, ensure_ascii=False, indent=1)


def call_openai(cfg, system, user):
    key = os.environ.get(cfg["key_env"], "")
    if not key:
        raise RuntimeError("缺少环境变量 %s" % cfg["key_env"])
    if os.environ.get("LLM_JUDGE_ALLOW_NETWORK") != "1":
        raise RuntimeError("未设置 LLM_JUDGE_ALLOW_NETWORK=1，不发起网络调用")
    url = os.environ.get(cfg["base_url_env"], cfg["base_url"]).rstrip("/") + "/chat/completions"
    body = {"model": os.environ.get(cfg["model_env"], cfg["model"]),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": int(os.environ.get("LLM_JUDGE_MAX_TOKENS", "4000"))}
    if os.environ.get("LLM_JUDGE_EFFORT"):
        body["reasoning_effort"] = os.environ["LLM_JUDGE_EFFORT"]
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("LLM_JUDGE_TIMEOUT", "120"))) as r:
            resp = json.loads(r.read().decode())
    except Exception as e:     # 报错信息里不带请求头
        raise RuntimeError("请求失败：%s" % type(e).__name__ + (" %s" % getattr(e, "code", "")))
    msg = resp["choices"][0]["message"]
    return msg.get("content") or "", {"model": body["model"], "usage": resp.get("usage")}


def mock_picks(nodes, space, width):
    ok = [n for n in nodes if not n["fail"] and n.get("pen") and None not in n["pen"]]
    if not ok:
        return []
    inc = max(ok, key=lambda n: (n["qor"].get("wns", -1e9), -n["id"]))
    best = [min(n["pen"][s] for n in ok) for s in range(4)]
    order = sorted(range(4), key=lambda s: -(inc["pen"][s] - best[s]))
    seen = {tuple(n["idx"]) for n in nodes}; out = []
    for s in order:
        for c in space["neighbors"][STAGES[s]][inc["idx"][s]]:
            idx = list(inc["idx"]); idx[s] = c
            if tuple(idx) not in seen:
                seen.add(tuple(idx)); out.append({"node": inc["id"], "stage": STAGES[s],
                                                  "settings": space["combos"][STAGES[s]][c], "reason": "mock"})
                break
        if len(out) >= width:
            break
    return out


def validate(raw_picks, nodes, space, width):
    byid = {n["id"]: n for n in nodes}; seen = {tuple(n["idx"]) for n in nodes}; out = []; rejected = []
    for p in raw_picks:
        try:
            n = byid[int(p["node"])]; s = STAGES.index(p["stage"])
            assert not n["fail"], "节点失败"
            c = space["combos"][STAGES[s]].index(p["settings"])
            idx = list(n["idx"]); idx[s] = c
            assert tuple(idx) not in seen, "重复设置"
            seen.add(tuple(idx)); out.append({"node": n["id"], "b": s, "idx": idx, "reason": str(p.get("reason", ""))[:300]})
        except Exception as e:
            rejected.append({"pick": p, "why": "%s: %s" % (type(e).__name__, e)})
        if len(out) >= width:
            break
    return out, rejected


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    return json.loads(m.group(0))["picks"]


def cmd_select(a):
    space = json.load(open(a.space)); nodes = load_nodes(a)
    prov = os.environ.get("LLM_JUDGE_PROVIDER", "mock"); cfg = PROVIDERS[prov]
    user = build_prompt(nodes, space, a.width, a.budget_left)
    rec = {"provider": prov, "prompt_sha256": hashlib.sha256((SYSTEM + user).encode()).hexdigest(), "t": time.time()}
    t0 = time.time(); picks = []; rejected = []
    try:
        if cfg["kind"] == "mock":
            raw = mock_picks(nodes, space, a.width); rec["raw"] = raw
        else:
            text, meta = call_openai(cfg, SYSTEM, user); rec.update(meta); rec["raw"] = text[:20000]
            raw = parse_json(text)
        picks, rejected = validate(raw, nodes, space, a.width)
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, e)
    rec.update({"latency_s": round(time.time() - t0, 3), "picks": picks, "rejected": rejected})
    if os.environ.get("LLM_JUDGE_SAVE_PROMPT") == "1":
        rec["prompt"] = user
    with open(os.path.join(a.dir, "llm_calls.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    json.dump({"picks": picks, "source": prov, "fallback": not picks}, open(a.out, "w"))


def main():
    p = argparse.ArgumentParser(); sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("select")
    for k in ("dir", "space", "nodes", "out"): s.add_argument("--" + k, required=True)
    s.add_argument("--width", type=int, default=2); s.add_argument("--budget-left", type=int, default=0)
    s = sp.add_parser("prompt", help="只打印给定状态下的提示词（不调用任何 API），便于审阅")
    for k in ("dir", "space", "nodes"): s.add_argument("--" + k, required=True)
    s.add_argument("--width", type=int, default=2); s.add_argument("--budget-left", type=int, default=0)
    a = p.parse_args()
    if a.cmd == "select":
        try:
            cmd_select(a)
        except Exception as e:     # 永不让搜索中断：写空 picks，Aura 回退
            json.dump({"picks": [], "source": "error", "fallback": True, "error": type(e).__name__}, open(a.out, "w"))
    else:
        print(SYSTEM); print(build_prompt(load_nodes(a), json.load(open(a.space)), a.width, a.budget_left))


if __name__ == "__main__":
    main()
