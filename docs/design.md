# 设计说明

## 1. 合成环境（全部是假设，不对应任何真实工艺或设计）

### tools/pdsim.py：PD 流程模拟器
- 3 个合成设计 syn-A / syn-B / syn-C。4 个可调阶段，每个阶段 2 个旋钮：
  - FP：util、aspect
  - PL：density、td
  - CTS：cluster、margin
  - GR：ladj、gtd
  - 共 5184 种组合，默认配置索引为 [5,3,3,4]。
- QoR 是确定性函数，同一配置总得到同一结果。
  - 拥塞 c 带来的绕线因子为 `1 + 6·max(0, c−0.65)²`。
  - c ≥ 1.10 时布线失败。
  - margin 会抬高拥塞，阶段之间因此真实耦合：上游选择会限制下游空间。
  - DR 相对 GR 的扣减为 `120·dr_risk`。
- **阶段成本是假设值**：FP 1、PL 4、CTS 2、GR 3、DR 10。只用来比较"复用 vs 全量重跑"的相对成本。
- `chain --from S --parent CKPT` 复用父检查点中 S 之前的阶段，只重跑 S 及其后。每个分支写自己的 jsonl 账本。
- `tools/landscape.py` 穷举全部配置，给出每个设计的最优值，用作参照：

  | 设计 | 最优 GR WNS | 默认 GR WNS（排名） | 布线失败配置数 |
  |---|---|---|---|
  | syn-A | 92.32 | 36.32（第 1521） | 36 |
  | syn-B | 83.25 | 50.92（第 231） | 186 |
  | syn-C | 99.09 | 46.14（第 251） | 768 |

### tools/eco3d.py：两层混合键合（HBT）ECO 环境
- 网格上有两层，HBT 之间要求最小间距 SB=1.5（网格单位）。
- 3 个规模：

  | 规模 | 网格 | 线网数 | 时钟网 |
  |---|---|---|---|
  | eco-S | 40×40 | 300 | 前 4 条 |
  | eco-M | 52×52 | 520 | 前 6 条 |
  | eco-L | 64×64 | 780 | 前 8 条 |

- 生成器在半径 r=5 内按间距合法放置 HBT，放不下就放到最近的空位。热点区域会制造间距违例（DRC）。
- 只读查询：`drc`、`netlist`、`hb_graph`、`layout_vpix`。`layout_vpix` 返回 ±8 窗口和前 6 个合法位置。
- 修复程序族：
  - `reroute`：EcoRoute 风格。逐级 k 扩大半径（3+2k），k>0 时纳入非时钟邻网。**时钟网永不纳入**。
  - `move`、`tuple`（耦合冲突成组移动）、`spread`。
- 基线：
  - `fullreroute`：按字典序、用设计半径全部重放，包括时钟网。
  - `stockrepair`：半径 3，单遍逐对修复。
- 验证器 `verify` 的门：routing、drc_no_new、drc_target_cleared、timing_max、timing_min、structural_eq、preservation（未声明的线网指纹不得变化）。
- **案例选取规则（事先固定）**：每个规模取种子 1..15 中前 3 个满足 5≤DRC≤30 的种子。
  - 得到 S:2,3,5 / M:1,6,7 / L:1,5,11，共 9 个案例，174 个 DRC。
  - 规则跟数字形态上模仿论文的 9 案例设置，但数字本身与论文不可比。

## 2. Aura 侧实现

| 文件 | 内容 |
|---|---|
| `aura/common.aura` | 工具函数、确定性 LCG 随机数、子进程、`spawn-reporting` |
| `aura/pd.aura` | Judge + stage agent 树搜索、各基线、overlay 维护 incumbent |
| `aura/evolve.aura` | Judge 策略自演化与演化门禁 |
| `aura/eco.aura` | 只读专家、证据与出处、候选程序、验证门控提交、哈希链账本 |
| `*-main.aura` | 批量实验入口，输出 `results/*.tsv` |

### PD
- 每轮 Judge 选 (节点, 阶段)：
  - 可调策略的 UCB 式打分：score(n,s) = 归一化 WNS(n) + c_explore·√(ln(T+1)/(1+visits(n))) + w_bottleneck·B(n,s)/max B + w_hist·(阶段 s 历史平均增益) + w_incumbent·[n 是 incumbent]。
  - B(n,s) 是 pen-gap：阶段 s 相对上游的 WNS 退化。
  - 策略本身是 live AST 里的一个定义 `judge-policy`。
- 每轮并发 PD_WIDTH=2 个分支 fiber：
  - 每个 fiber 进入 `fiber:overlay-begin`，把自己的候选写进 overlay 里的全局 `pd-incumbent`；
  - 主 fiber 比较结果后，只对胜出且超过 incumbent 的分支发 commit，其余 rollback。
  - 检查点按 COW 方式复用：子分支只引用父检查点路径，不复制上游。
- 预算 20 次 GR 评估。DR 只对最终 incumbent 跑一次。
- 方法：
  - `default`
  - `random`：扁平随机
  - `autotuner`：扁平邻域局部搜索，每次全量重跑
  - `greedy`：确定性最大瓶颈，从 incumbent 在 pen-gap 最大的阶段分支，与论文 Table 5 的 heuristic 对照组同构
  - `judge`
  - `judge-norerun`：同一 Judge，但每次从 FP 全量重跑，用来量化复用

### Judge 自演化（演化门禁）
1. `ast:snapshot`
2. `mutate:rebind "judge-policy" <新 lambda>`
3. `eval-current`
4. 在训练种子上重放
5. 门禁：平均 GR WNS 提升 ≥0.5，失败分支不增，成本升幅 ≤5%。通过就保留；否则 `ast:restore` + `eval-current` 回滚。

每步写审计行。最后在保留种子上，对比快照里的初始策略和演化后策略。

### ECO
1. 4 个只读专家 fiber：drc / netlist / hb_graph / layout_vpix。每个都在 overlay 里运行，结束时一律 rollback，保证只读，测试会验证这一点。
2. 证据必须带出处：工具名、检查点 sha256、查询参数。出处不全或哈希不符的证据会被拒绝。
3. 由证据生成候选程序（reroute k=0..3 / move / tuple / spread），并发在各自 overlay 里执行。
4. 验证器通过且改善最多的候选才 commit：排序先按剩余 DRC，再按 Δnet。
5. 没有完全清零时进入 Revise：用新证据再来一轮，直到预算 ECO_BUDGET=40 次执行器调用用完。
6. 每步写进 sha256 哈希链账本。事件名沿用论文：observe / fanout.diagnose / algorithm.exec / plan.action_sel / execute.completed / fanout.verify / decide / reflect / seal。`tools/ledger_check.py` 会独立复核。

## 3. 与论文的差异（务必注意）
- 论文里的 Judge / stage agent / 规划器都是 LLM。这里全部是确定性策略，没有调用任何模型。本仓库验证的是**编排与状态管理的机制**，不是 LLM 的决策质量。
- 环境、成本和缺陷都是合成的。数字只能在本仓库内部横向比较，不能与论文数字比较。
- ECO 的签核次数（论文 ≤3）这里没有单独建模；验证器每次候选都跑，计入执行器调用。
