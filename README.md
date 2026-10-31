# aura-eda-agents

探索用 [Aura](https://github.com/cybrid-systems/aura) 承载两类 EDA Agent 的编排与状态管理：

- **AgenticPD**（arXiv:2607.04758，ASPDAC'27）：阶段感知的物理设计树搜索。
- **AgenticECO**（arXiv:2608.03738）：证据门控的 3D-IC ECO 修复。

> ⚠️ 本仓库有两类数字，表格里都会分别标注：
> - **真实 ORFS**：PD 树搜索接入真实 OpenROAD-flow-scripts，设计为 ORFS 自带的公开设计 gcd/nangate45，见下文“结果（真实 ORFS）”。
> - **合成**：确定性合成环境（`tools/pdsim.py`、`tools/eco3d.py`）。ECO 部分全部是合成数据。
>
> 两类都没有调用任何 LLM，也不含任何客户设计数据。数字只能在仓库内部横向比较，**不能**与论文数字对比。论文里的数字见 [docs/papers.md](docs/papers.md)，已对照原文核对。

## 动机

两篇论文都在 EDA 工具外面加了一层 **Agent 编排**。这层编排真正难的地方是状态管理，不是"调用模型"：

| 论文 | 核心诉求 |
|---|---|
| AgenticPD | 从树上任意保留节点分支，复用上游阶段的中间状态，只重跑下游。多个分支并行试探，只有胜者能改写 incumbent。 |
| AgenticECO | 专家只读取证。证据必须带出处，可追溯到检查点。修改必须先过外部验证器才能提交。时钟网永不纳入。全过程写进防篡改账本。 |

这些正好是 Aura 运行时原生提供的能力：
- fiber 并发
- 写时复制（COW）的 overlay 事务：`fiber:overlay-begin / commit / rollback`
- live AST 的快照与回滚：`ast:snapshot / ast:restore`
- 热替换：`mutate:rebind` + `eval-current`
- 运行时变更日志：`(query :mutation-log)`

本仓库的问题是：**把论文里的机制一一落到 Aura 原语上，能不能得到一个可运行、可审计、可自我演化的原型？**

## 论文机制 → Aura 原语

| 论文机制 | Aura 原语 / 本仓库实现 | 位置 |
|---|---|---|
| PD：从保留节点 n̂ 在阶段 b 分支，复用 Bef(b) | 子分支只引用父检查点（COW 式复用）；`chain --from b --parent ckpt` 只重跑 b 及其后 | `aura/pd.aura` `run-branches!` |
| PD：并行试探多个分支，incumbent 独立更新 | 每个分支一个 fiber，在 `fiber:overlay-begin` 里写全局 `pd-incumbent`；主 fiber 裁决后，只对胜出分支 `overlay-commit`，其余 `rollback` | `aura/pd.aura` |
| PD：Judge 看 E(n)/B(s) 选 (节点, 阶段, 提示) | `judge-select`：探索项（访问次数）+ 瓶颈 B(s) + 阶段历史增益；策略本身是 live AST 中的 `judge-policy` | `aura/pd.aura` |
| PD：Stage agent 使用上游 QoR、经验与 Judge 提示 | `stage-agent`：一步邻域 + 跨迭代经验 + 提示加分（确定性，无 LLM） | `aura/pd.aura` |
| PD：只对 incumbent 跑 DR | `pd-finish` | `aura/pd.aura` |
| （扩展）Judge 策略自我演化 | `ast:snapshot` → `mutate:rebind` → `eval-current` → 重放 → **演化门禁** → 保留或 `ast:restore` | `aura/evolve.aura` |
| ECO：只读专家并行取证 | 每个专家一个 fiber，在 overlay 里运行，结束时**一律 rollback**（写入不可能泄漏） | `aura/eco.aura` `run-experts!` |
| ECO：证据带出处 | 证据 = 数据 + {tool, ckpt_sha256, query}；`admit-evidence` 会拒收出处不全或哈希不符的证据 | `aura/eco.aura` |
| ECO：EcoRoute 有界扩大（k≤3），时钟网永不纳入 | 候选程序 `reroute k=0..3`，由环境强制不碰时钟网 | `aura/eco.aura` + `tools/eco3d.py` |
| ECO：外部验证器通过后才提交 | 候选在各自 fiber/overlay 中执行并验证；只有验证通过且最优的候选 `overlay-commit` 到 `eco-current` | `aura/eco.aura` `run-candidates!` |
| ECO：哈希链账本（observe … seal） | sha256 链式账本，事件名沿用论文；`tools/ledger_check.py` 独立复核 | `aura/eco.aura` `ledger!` |
| ECO：Revise / 反思 | 未清零时用新证据再规划，直到预算用完 | `aura/eco.aura` `eco-repair` |

## 架构

```mermaid
flowchart TB
  subgraph Aura["Aura 运行时（单进程）"]
    direction TB
    J["Judge<br/>judge-policy ∈ live AST"]
    EVO["演化门禁<br/>snapshot → rebind → 重放 → 保留 / restore"]
    subgraph PD["PD 树搜索"]
      B1["分支 fiber 1<br/>overlay: pd-incumbent"]
      B2["分支 fiber 2<br/>overlay: pd-incumbent"]
    end
    subgraph ECO["证据门控 ECO"]
      EX["只读专家 fiber ×4<br/>overlay 恒 rollback"]
      EV["证据 + 出处<br/>tool / ckpt_sha256 / query"]
      CA["候选程序 fiber<br/>reroute k≤3 / move / tuple / spread"]
    end
    LED[("哈希链账本")]
    ML[("(query :mutation-log)")]
  end
  subgraph Env["后端（可替换：PD_TOOL / ECO_TOOL）"]
    PDS["tools/pdsim.py<br/>合成 PD 流程 FP→PL→CTS→GR(→DR)"]
    E3["tools/eco3d.py<br/>合成 2 层 HBT + 验证器"]
    OR["adapters/openroad<br/>（桩）"]
  end
  EVO -- mutate:rebind / ast:restore --> J
  J -- (节点, 阶段, 提示) --> B1 & B2
  B1 & B2 -- chain --from b --parent ckpt --> PDS
  B1 & B2 -- 胜者 overlay-commit / 其余 rollback --> J
  EX -- query --> E3
  EX --> EV --> CA
  CA -- apply + verify --> E3
  CA -- 验证通过且最优才 commit --> LED
  EX --> LED
  B1 & B2 -.-> ML
  EVO -.-> ML
  PDS -.-> OR
  E3 -.-> OR
```

## 真实 vs 模拟

| 部分 | 状态 |
|---|---|
| Aura 运行时原语（fiber、overlay commit/rollback、ast:snapshot/restore、mutate:rebind、mutation-log） | **真实**：在 Aura main（5af7fd023）上实际运行，`tests/aura/core.aura` 断言了这些性质 |
| 编排逻辑（树搜索、分支复用、证据出处、验证门控提交、哈希链账本、演化门禁） | **真实**：Aura 代码，可运行 |
| Judge / stage agent / 规划器的"智能" | **模拟**：确定性启发式策略，**没有 LLM**。论文中这些都是 LLM |
| PD 流程（FP/PL/CTS/GR/DR）与 QoR | **两套后端**：<br>- **真实**：`adapters/openroad/backend.py` 逐阶段调用 ORFS（官方镜像 openroad/orfs，设计 gcd/nangate45）。每阶段 .odb 作为检查点供分支复用；WNS/TNS/功耗/面积取自 ORFS 指标 JSON；成本为实测秒数。<br>- **模拟**：`tools/pdsim.py` 的解析式模型，阶段成本为假设值 |
| 3D-IC HBT 布局、DRC、时序、结构等价 | **模拟**：`tools/eco3d.py` 的网格模型 |
| OpenROAD/ORFS 接入 | **PD 已实现**：`tests/test_orfs.py` 验证 ORFS 的确定性，以及“检查点复用 = 全量重跑”。<br>**ECO 未接入**：开源流程里没有混合键合 3D-IC 设计与缺陷，论文引擎 TaiWei 与缺陷案例也取不到，原因见 [docs/eco-real.md](docs/eco-real.md) |
| 论文数字 | 只在 `docs/papers.md` 中引用，本仓库**没有**复现 |

## 结果（真实 ORFS）

完整表格见 [results/summary_orfs.md](results/summary_orfs.md)，可用 `python3 tools/summarize_orfs.py` 重新生成。

**设置**：
- 设计与平台：ORFS 自带的 gcd / nangate45，官方 SDC，时钟周期 0.25 ns。
- 镜像：`openroad/orfs@sha256:cba40c1d…52b49fa`，Yosys 0.68+post；OpenROAD 版本串为 unknown，以镜像 digest 作为版本标识。
- 搜索空间：4 个阶段各 2 个 ORFS 变量，共 4608 种组合，见 [adapters/openroad/README.md](adapters/openroad/README.md)。
- 运行规模：3 个种子 × 6 种方法，每次预算 20 次到 GR 的评估；`NUM_CORES=4`，一次只跑一个 flow。

| 方法 | 平均 GR WNS (ps) | 平均 finish WNS (ps) | 平均阶段执行数 | 平均成本（实测秒） |
|---|---|---|---|---|
| ORFS 默认配置 | −158.565 | −155.709 | 4.0 | 63 |
| 扁平随机 | −159.737 | −156.974 | 79.3 | 620 |
| 类 AutoTuner 扁平局部搜索 | −158.565 | −155.709 | 80.0 | 846 |
| 最大瓶颈贪心（复用） | −158.565 | −155.709 | 67.0 | 804 |
| 历史感知 Judge（复用） | −158.565 | −155.709 | **46.7** | 669 |
| 同一 Judge，不复用 | −158.565 | −155.709 | 80.0 | 811 |

- **没有任何搜索方法超过 ORFS 默认配置。**
  - 6 种方法合计评估了 110 个不同的完整配置，最好的就是默认值（另有 4 个配置与它并列）。中位数 −165.4 ps。
  - 默认配置 finish 后 DRC 为 0。
  - 在这个小设计和这组旋钮上，ORFS 默认值已经是我们能找到的最好结果。论文报告的 WNS 提升在这里**没有复现**。
  - 可能的原因：
    - gcd 太小；
    - 0.25 ns 时钟下 WNS 主要由 ORFS 自身的 repair_timing 决定；
    - 所选旋钮对时序的杠杆不够。
- **复用的收益是真实的**：
  - Judge 复用与不复用得到相同的 incumbent（3/3）；
  - 阶段执行数 46.7 vs 80.0，实测成本 669 s vs 811 s（−17%）。
  - 节省比例小于合成环境（−27%），因为真实 ORFS 中 GR（含修复）一个阶段就约 33 s，占一条完整链约 45 s 的大头，复用上游省下的只是 FP/PL/CTS 各约 4 s。
- 失败配置全部是 `CORE_UTILIZATION=65`：时序修复增大面积后合法化失败（DPL-0038 / DPL-0033）。
- 计算量：
  - 逻辑阶段执行 1089 次（记账 190.6 分钟），真正调用 ORFS 294 次（实测 56.0 分钟），其余命中结果缓存。
  - 缓存键为（阶段，上游全部旋钮）。ORFS 的确定性已由测试验证。
  - 总墙钟 56.4 分钟。
- 统计意义有限：只有 1 个设计、3 个种子。Judge 是确定性启发式，不是 LLM。

## 结果（合成）

完整表格见 [results/summary.md](results/summary.md)，可用 `python3 tools/summarize.py` 重新生成。WNS 是合成单位，越大越好；成本单位来自假设的阶段成本。

### PD：3 设计 × 5 种子，每次预算 20 次 GR 评估

| 方法 | 平均 GR WNS | 平均 DR WNS | 平均阶段执行数 | 平均成本单位 |
|---|---|---|---|---|
| 默认配置 | 44.46 | 48.85 | 4.0 | 20.0 |
| 扁平随机 | 55.56 | 55.49 | 80.0 | 210.0 |
| AutoTuner 式扁平局部搜索 | 71.68 | 72.68 | 80.0 | 210.0 |
| 最大瓶颈贪心（复用） | **80.24** | **80.32** | 53.8 | 156.5 |
| 历史感知 Judge（复用） | 75.89 | 75.66 | 53.9 | **154.2** |
| 同一 Judge，不复用 | 75.89 | 75.66 | 80.0 | 210.0 |
| 穷举最优（参照） | 91.55 | – | – | – |

- **复用的收益是确定的**：Judge 复用与不复用得到完全相同的 incumbent（15/15），阶段执行 53.9 vs 80.0，成本 154.2 vs 210.0（−27%）。
- Judge 的分支阶段分布：FP 30.2%、PL 25.3%、CTS 21.8%、GR 22.8%；69.8% 的分支复用了上游。
- **与论文相反的发现（如实报告）**：在本合成环境里，最大瓶颈贪心的 WNS 高于历史感知 Judge（syn-A、syn-C 上更好，syn-B 上更差）。论文 Table 5 是 LLM Judge 胜过该启发式。可能的原因是我们的 Judge 是手写打分而不是 LLM，合成地形也偏平滑。这正是"策略需要演化"的动机。
- 90 次运行中，全局 `pd-incumbent` 都只由胜出分支的 overlay-commit 改写，并与 incumbent 检查点一致。

### Judge 自演化（演化门禁）

- 9 个候选改动中，门禁保留 1 个（w_incumbent +0.5，训练集 77.17→78.49），回滚 8 个。其中包括一个故意放进去的坏候选（纯探索，WNS 59.9，成本 +38%）。
- **保留集**（种子 201–205）：演化后策略 78.60 vs 初始 78.85，成本 165.67 vs 157.07，**轻微过拟合**。门禁只看训练集还不够，见路线图。

### ECO：9 个合成案例，174 个 DRC，每案例 ≤40 次执行器调用

| 方法 | DRC 前→后 | 清零案例 | 平均 Δnet% | 改动时钟网 | 时序门通过 |
|---|---|---|---|---|---|
| 全量拆线重布 | 174→186（+6.9%） | 0/9 | 72.33% | 26 | 7/9 |
| 库存式逐对修复 | 174→170 | 0/9 | 0.20% | 0 | 9/9 |
| **证据门控 Agent（Aura）** | **174→38（−78.2%）** | **4/9** | 4.53% | **0** | 9/9 |
| 消融：无 EcoRoute | 174→46 | 2/9 | 2.12% | 0 | 9/9 |
| 消融：无 hb_graph 专家 | 174→33 | 4/9 | 4.11% | 0 | 9/9 |
| 消融：无 Revise | 174→50 | 1/9 | 2.08% | 0 | 9/9 |

- Agent 的定性趋势与论文一致：降幅远大于两个基线，扰动远小于全量重布，时钟网为 0。但清零率（4/9）低于论文的 7/9，**数字不可比**。
- 去掉 EcoRoute 或 Revise 都明显变差。去掉 hb_graph 专家在本合成环境里**反而略好**（33 vs 38）：该专家提供的信息在这里与 drc/layout 专家冗余。如实报告。
- 所有 Agent 运行的账本都通过 `tools/ledger_check.py` 独立复核。`eco-current` 只经由 overlay-commit 改写。

## 路线图

1. **轻集成（当前）**：Aura 作为编排层，EDA 工具作为子进程。状态的载体是检查点文件 + overlay 里的全局绑定。
   - 已完成：`adapters/openroad` 接入 ORFS（gcd/nangate45）。
   - 下一步：
     - 换更大的设计（如 aes、ibex，sky130hd / asap7）；
     - 加入对时序更有杠杆的旋钮，例如时钟周期余量、repair_timing 参数、布局密度；
     - 跨设计复用缓存。
   - 接入 LLM 作为 Judge / stage agent / 规划器，与确定性策略对照。
2. **状态集成**：把检查点元数据、证据、账本作为 Aura 一等值放进 overlay / worldline，fork 分支时零拷贝共享上游。
   - 让 `ast:snapshot` 同时覆盖策略和搜索树。
   - 让账本复用 Aura 自己的 mutation 审计 WAL。
3. **深集成**：EDA 工具以 Aura 原生能力（capability + sandbox）暴露。
   - 验证器作为提交钩子：overlay-commit 前必须过 verify。
   - "时钟网永不纳入"之类的约束作为类型/能力约束，在运行时强制，而不是由环境自觉遵守。
4. **Judge 自演化**：
   - 演化门禁加入**保留集检验**和多种子置信区间，修正本次观察到的过拟合。
   - 候选从参数扰动扩展到让 LLM 改写 `judge-policy` 的代码本身。每次 rebind 都进 mutation-log，可审计、可回滚。
   - ECO 的修复策略同理演化，门禁加上"零时钟网、零新 DRC"的硬约束。

## 目录

```
aura/                 Aura 原型（pd / evolve / eco / *-main 入口 / common）
tools/                合成环境与工具：pdsim.py  eco3d.py  landscape.py  ledger_check.py  summarize.py
adapters/openroad/    真实 ORFS 后端（backend.py）与宿主机桥（bridge.py）
tests/                test_envs.py（环境/验证器）  aura/core.aura（Aura 性质测试）  run_tests.sh  test_orfs.py（真实 ORFS 确定性/复用等价）
results/              合成：pd_runs.tsv 等与 summary.md；真实 ORFS：pd_runs_orfs.tsv、orfs_ledgers/、orfs_meta.json、summary_orfs.md
docs/                 papers.md（论文事实）  design.md（设计与假设）  aura-issues.md（构建中发现的 Aura 问题）
scripts/aura.sh       拼接模块并用 aura --load 运行
```

## 运行

需要一个 Aura 二进制（本仓库在 cybrid-systems/aura main 5af7fd023 上验证），以及 Python 3（只用标准库）。

```bash
export AURA_BIN=/path/to/aura
tests/run_tests.sh                       # 单测 + Aura 性质测试，输出 TESTS-OK
scripts/aura.sh aura/pd-main.aura        # PD：→ results/pd_runs.tsv
scripts/aura.sh aura/evolve-main.aura    # Judge 演化：→ results/evolve_*.tsv
scripts/aura.sh aura/eco-main.aura       # ECO：→ results/eco_runs.tsv
python3 tools/summarize.py               # → results/summary.md

# 真实 ORFS（需要 docker 与 openroad/orfs 镜像，见 adapters/openroad/README.md）
python3 tests/test_orfs.py
PD_TOOL=adapters/openroad/backend.py PD_DESIGNS=gcd-nangate45 PD_SEEDS=1,2,3 \
  PD_OUT=results/pd_runs_orfs.tsv AURA_TIMEOUT=43200 scripts/aura.sh aura/pd-main.aura
python3 tools/summarize_orfs.py          # → results/summary_orfs.md
```

所有环境和策略都是确定性的，同样的种子会得到同样的结果。

## Aura 问题

构建过程中发现的 Aura 问题都已查重，并以中文提交到 cybrid-systems/aura，见 [docs/aura-issues.md](docs/aura-issues.md)。

## 许可

Apache-2.0。论文内容版权归原作者；本仓库没有复制 AgenticPD 或 AgenticECO 的代码。
