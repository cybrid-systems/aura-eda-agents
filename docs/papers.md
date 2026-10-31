# 两篇论文的可引用事实

本页只记录论文正文里写明的事实和数字，供 README 与设计文档引用。本仓库**没有**复现这些数字。仓库里的数字都来自合成环境，见 [`results/summary.md`](../results/summary.md)。

## AgenticPD（arXiv:2607.04758，ASPDAC'27）

**问题**：物理设计（PD）流程参数调优。每次评估都要从头跑完整流程，非常昂贵。已有的 AutoTuner、ORFS-Agent 等方法把整条流程当作黑盒，做"扁平"搜索。

**机制**
- 流程按阶段分为 FP（floorplan）/ PL（placement）/ CTS / GR（global route）。DR（detailed route）只对 incumbent 运行。
- 搜索空间是一棵**阶段感知的树**：从某个阶段 s 的检查点分支，复用 s 之前的上游结果，只重跑 s 及之后的阶段。
- 每个阶段有一个 **stage agent**。它的上下文包括上游 QoR、跨迭代经验摘要、检索到的知识，以及 Judge 给的提示（论文式 9）。
- **Judge** 是树上的导航策略，看的是自适应画像 A_k = ({E(n)}, {B(s)})：
  - E(n) 是探索平衡，衡量节点被重访的次数；
  - B(s) 是阶段瓶颈，衡量阶段 s 落后其已知最好结果多少。
  - Judge 输出 (分支节点, 分支阶段, 各下游阶段提示)。分支节点不必是 incumbent。
- 树搜索与 incumbent 选择解耦。incumbent 是 post-GR WNS 最高的候选（论文式 4）。
- 预算：20 次后端评估。所有方法共用同一后端、动作空间和阶段实现。
- 代码：https://github.com/Flians/AgenticPD

**报告的数字**
- 分支阶段分布：FP 28.1%，CTS 27.2%，GR 23.7%，PL 21.1%。约 72% 的决策选择 PL/CTS/GR 分支，复用缓存的上游阶段。
- Judge 消融（Table 5，ASAP7，3 次运行）：对照组换成确定性的 max-bottleneck 启发式，即从最好节点分支，在"上游到下游 WNS 退化最大"的阶段重启。AgenticPD Judge 的 WNS 高出 10.6 ps（AES）和 35.5 ps（JPEG）。在 JPEG 上，该启发式从未跳出默认配置。
- JPEG / ASAP7：AgenticPD +50.6 ps；AutoTuner −10.0 ps；ORFS-Agent +0.3 ps。
- 环内（post-GR）WNS 与 post-route WNS 的一致性（SKY130HD）：Spearman ρ = 0.86（AES）/ 0.83（ibex）/ 0.93（JPEG），Pearson r = 0.96 / 0.96 / 0.91。DR 每个配置的中位耗时为 38–75 分钟，所以只对 incumbent 跑 DR。
- 在所有基准上，至少 80% 的总改进在第 8–14 次迭代内达成。

## AgenticECO（arXiv:2608.03738）

**问题**：3D-IC（混合键合，HBT）上的 ECO 修复。目标是消除 DRC 违例，同时尽量少改动线网，不碰时钟网，并通过签核验证。

**设置**：TaiWei / OpenROAD / ASAP7；GCD / I2C / UART；9 个缺陷案例；预算 ≤40 次工具调用、≤3 次签核。

**机制**
- 多个只读专家先收集**带出处（provenance）的证据**，再由规划器选择修复程序。
- **EcoRoute**：局部重布线，失败时按 k≤3 逐级扩大范围。
- **时钟网永不纳入**修改集。
- **验证器**通过后才提交。
- **哈希链账本**，事件依次为 observe / fanout.diagnose / algorithm.exec / plan.action_sel / execute.completed / fanout.verify / decide / reflect / seal。
- 候选排序：用执行前的工具记录算出确定性的残余冲突分 c(a) 和占用分 o(a)，按 c(a) 字典序优先、o(a) 次之（论文式 9）。
- 验证器 𝒱 是冻结、确定性的，在模型之外给出裁决。未被纳入修改集的线网逐网按几何哈希检查。结构等价是结构性的，不是形式化 LEC。

**报告的数字（Table 1，Opus 4.8 作为模型）**

| 方法 | DRC 合计 | DRC 清零案例 | Δnets | 改动时钟网 |
|---|---|---|---|---|
| AgenticECO | 94→17（−81.9%） | 7/9 | 正文：已清零案例平均扰动 0.66%；表列 1.08 | 0 |
| 全量重布（full reroute） | 94→116（+23.4%） | 2/9 | 55.85% | 98 |
| 库存修复（stock repair） | 94→63（−33.0%） | 2/9 | 0.77% | 2 |
| 单 Agent（Opus 4.8） | 94→35（−62.8%） | 6/9 | 0.61（表列） | 0 |

- 换用 GPT-5.6，在同一封存契约下重跑：两个 agent 臂都 9/9 清零。AgenticECO 扰动 0.44%，单 agent 对照 1.00%，时钟网 0。
- 代码：anonymous.4open.science/r/AgenticECO（BSD-3）。本仓库**没有**复制其代码。
