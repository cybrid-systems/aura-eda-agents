# ECO：为什么没有真实 3D-IC 后端

AgenticECO（arXiv:2608.03738）修的是**混合键合 3D-IC** 布线后的 DRC 缺陷。本仓库的 ECO 实验仍然只在合成网格模型
`tools/eco3d.py` 上运行。原因如下（2026-10-31 核对）：

1. **开源流程里没有对应的 3D-IC 设计与缺陷。** 本仓库使用的官方镜像 `openroad/orfs:latest`
   （digest `sha256:cba40c1d…52b49fa`）里，平台只有 asap7、gf180、gt2n、ihp-sg13g2、nangate45、sky130 系列。
   在 `flow/` 的 Makefile、scripts、platforms、designs 中搜索 3dic / hybrid，只命中 `gf180/aes-hybrid`。
   它是混合**行高**（mixed-height sites）示例，不是混合**键合**。镜像里没有双 tier 折叠设计，没有键合端子（HBT），
   也没有带 3D 缺陷的布线后数据库。
2. **论文的引擎与案例无法直接取得。** 论文的引擎是 TaiWei + OpenROAD，在 ASAP7 上用 GCD/I2C/UART 构造了 9 个缺陷案例。
   其匿名仓库（anonymous.4open.science/r/AgenticECO）的 README 写明：
   - 引擎以 git submodule 固定在 `third_party/`，匿名镜像里该目录"by construction"为空；
   - "Experiment result data and session logs" 不随仓库发布；
   - 真实 episode 需要按 `docs/REPRODUCING.md` 自行构造封存检查点。
   
   本仓库没有复制其代码，也没有其封存的缺陷检查点。
3. **自己造缺陷不等于论文问题。** 在 2D ORFS 布线结果上人为注入 DRC，再做局部重布，可以测"证据门控 + 验证后提交"的编排，
   但缺陷分布与 3D 键合缺陷无关，结果不能与论文比较。这一步的成本也不小：需要可控的缺陷注入、逐网几何哈希、
   签核门禁。所以本轮**没有**做 2D ECO 桩，避免产出看似真实、其实不可比的数字。

## 如果要做真实 ECO，下一步

- 按 AgenticECO 的 `docs/REPRODUCING.md` 构建其 docker 镜像（含 TaiWei submodule，需能取得非匿名源），
  再把 `ECO_TOOL` 契约（`tools/eco3d.py` 的 CLI）对接到它的 adapter 层。Aura 侧不需要改：`aura/common.aura` 已经从环境变量 `ECO_TOOL` 读取后端。
- 或者退而求其次：用 ORFS 在 2D 设计上做布线后的时序/DRC ECO（例如对 `5_route.odb` 做 `repair_timing` 或局部 `detailed_route`）。
  这只能验证编排，并且必须在结果里明确标注为 2D、自造缺陷。
