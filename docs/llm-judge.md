# 可插拔 LLM Judge（接口已就绪，尚未调用任何真实 LLM）

AgenticPD 中由 LLM 担任的 Judge 在本仓库里是一个外部进程。Aura 的 `pd.aura` 新增方法 `llm-judge`，每轮调用：

```
tools/llm_judge.py select --dir RUN_DIR --space SPACE_JSON --nodes "id:parent:b:i0,i1,i2,i3:fail;..." \
                          --width W --budget-left K --out judgeN.json
```

- **输入**：搜索树（每个节点的完整设置、父节点、分支阶段、是否失败）。工具从 `RUN_DIR/b<id>.json` 读取真实 QoR（WNS/TNS/功耗/面积）和逐阶段损失 `pen`，再算出每阶段与全树最好值之差 `gap`（论文的阶段瓶颈 B(s)）。
- **输出**：`{"picks":[{"node","b","idx"}], "source", "fallback"}`。
  - LLM 必须给出"节点 + 阶段名 + 该阶段全部旋钮取值"，工具把它映射回设置下标。
  - 非法节点、失败节点、不存在的设置、重复评估都会被拒绝，并记录原因。
- **回退**：没有 key、网络失败、回复无法解析、picks 全部非法时，返回空 picks。Aura 改用确定性的历史感知 Judge，并在结果里记 `llm_fallbacks`，搜索不会中断。
- **审计**：每次调用追加一行到 `RUN_DIR/llm_calls.jsonl`，内容为提供方、模型、提示词 SHA-256、原始回复、被拒绝的 picks、耗时、token 用量；设置 `LLM_JUDGE_SAVE_PROMPT=1` 时连提示词一起保存。**不含 key。**
- **复用**：`llm-judge` 与 `judge` 一样从分支阶段复用上游检查点。预算、账本、DR 和 summary 与其它方法完全相同，可以直接对照。

## 提供方

| `LLM_JUDGE_PROVIDER` | 说明 |
|---|---|
| `mock`（默认） | 确定性、离线：选 incumbent 的最大 gap 阶段，取一步邻居。只用来打通管线，**不是 LLM** |
| `minimax` | MiniMax。key `MINIMAX_API_KEY`；模型 `MINIMAX_MODEL`，默认 `MiniMax-M3.1-Flash`；地址 `MINIMAX_BASE_URL`，默认 `https://api.minimaxi.com/v1` |
| `glm` | 智谱 GLM Coding Plan。key `GLM_API_KEY`；模型 `GLM_MODEL`，默认 `glm-5.3-flash`；地址 `GLM_BASE_URL`，默认 `https://open.bigmodel.cn/api/coding/paas/v4` |
| `deepseek` | DeepSeek。key `DEEPSEEK_API_KEY`；模型 `DEEPSEEK_MODEL`，默认 `deepseek-flash`；地址 `DEEPSEEK_BASE_URL`，默认 `https://api.deepseek.com` |

新增提供方：在 `tools/llm_judge.py` 的 `PROVIDERS` 中加一项。OpenAI 兼容接口只需要地址、key 环境变量名和默认模型名。

## 安全约定

- key 只从环境变量读取。不写入仓库、日志、输出或报错信息。
- 真实网络调用必须显式设置 `LLM_JUDGE_ALLOW_NETWORK=1`；否则即使有 key，也只记一条"未调用"并回退。
- 其它可调项：
  - `LLM_JUDGE_MAX_TOKENS`：默认 4000
  - `LLM_JUDGE_EFFORT`：对应 `reasoning_effort`
  - `LLM_JUDGE_TIMEOUT`：默认 120 s

## 运行（第 3 步再做）

```bash
LLM_JUDGE_PROVIDER=minimax LLM_JUDGE_ALLOW_NETWORK=1 PD_METHODS=default,judge,llm-judge \
  PD_TOOL=adapters/openroad/backend.py ORFS_SPACE=v2 PD_DESIGNS=aes-nangate45 ... scripts/aura.sh aura/pd-main.aura
python3 tools/llm_judge.py prompt --dir RUN_DIR --space SPACE_JSON --nodes ...   # 只看提示词，不调用
```

目前只用 `mock` 在合成环境里验证过管线：syn-A 种子 1 上，`llm-judge`（mock）跑完 20 次评估，10 次调用全部有效、零回退。本仓库还**没有**任何真实 LLM 的结果。

## 提供方配置说明（2026-10-31）

- 三个提供方都走 OpenAI 兼容的 `POST <base>/chat/completions`。地址和模型名由仓库所有者提供，并说明三个 key 都已验证可用。本仓库的实验还没有通过本接口调用过任何一个。
- 用 `LLM_JUDGE_PROVIDER=minimax|glm|deepseek` 选择提供方。key 只从环境变量读取，不写入代码、日志或 `llm_calls.jsonl`。
- Aura 跑在开发容器里时，key 要作为环境变量传进容器。用 `docker run -e MINIMAX_API_KEY` 这种只写变量名的形式，不要把值写在命令行上。
