# OpenROAD 适配器（真实 ORFS 后端）

`backend.py` 用官方镜像 `openroad/orfs` 中的 OpenROAD-flow-scripts 执行真实的布局布线。它实现与
`tools/pdsim.py` 相同的 PD_TOOL CLI（`space` / `chain` / `dr` / `ledger`），Aura 侧的 `aura/pd.aura` 不需要任何修改。

## 环境（本次实验）

| 项 | 值 |
|---|---|
| 镜像 | `openroad/orfs:latest`，`openroad/orfs@sha256:cba40c1de21f048b545c005210dabe728bf501f4bb7d5ae5f03b33feb52b49fa`，创建于 2026-10-30 22:09（UTC+8） |
| OpenROAD | 镜像内 `openroad -version` 输出 `unknown`，ORFS 目录不带 .git，所以以镜像 digest 作为版本标识 |
| Yosys | 0.68+post |
| 设计 / 平台 | ORFS 自带的 `gcd` / `nangate45`（`designs/nangate45/gcd/config.mk`）。SDC 未改，时钟周期 0.25 ns |
| 并行 | 一次只跑一个 flow（文件锁），`NUM_CORES=4`；宿主机 8 核 16 GB |

## 用法

```bash
sudo docker pull openroad/orfs:latest
python3 tests/test_orfs.py                       # 确定性 + 复用等价核对，约 2 分钟
# Aura 跑在没有 docker 的开发容器里时，宿主机先起一个小桥：
python3 adapters/openroad/bridge.py &
ORFS_EXEC=bridge PD_TOOL=adapters/openroad/backend.py PD_DESIGNS=gcd-nangate45 PD_SEEDS=1,2,3 \
  PD_OUT=results/pd_runs_orfs.tsv AURA_TIMEOUT=43200 scripts/aura.sh aura/pd-main.aura
```

环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `ORFS_WORK` | `/workspace/orfs-work` | 工作区，映射为容器内 `/work` |
| `ORFS_IMAGE` | `openroad/orfs:latest` | 镜像 |
| `ORFS_CONTAINER` | `orfs-eda` | 常驻容器名，用 `docker exec` 驱动 |
| `ORFS_CORES` | 4 | 线程数 |
| `ORFS_NOCACHE` | — | 设为 1 时不读、不写结果缓存 |
| `ORFS_EXEC` | — | 设为 `bridge` 时经 `bridge.py` 执行 |

## 阶段与旋钮

综合（`make synth`）只做一次，作为所有分支的根。四个可调阶段各对应一个 ORFS make 目标。`do-*` 目标不检查依赖，
直接读取 WORK_HOME 里上一阶段的 `.odb`。

| 阶段 | make 目标 | 旋钮（名字沿用 pd.aura） | ORFS 变量与取值，**粗体为默认** | 阶段末读取的指标 |
|---|---|---|---|---|
| FP | `do-floorplan` | util, aspect | `CORE_UTILIZATION` 35/45/**55**/65；`CORE_ASPECT_RATIO` 0.7/**1.0**/1.4 | `2_1_floorplan.json` |
| PL | `do-place` | density, td | `PLACE_DENSITY_LB_ADDON` 0/0.1/**0.2**/0.3；`GPL_TIMING_DRIVEN` 0/**1** | `3_5_place_dp.json` |
| CTS | `do-cts` | cluster, margin | `CTS_CLUSTER_SIZE` **不设**/10/20/40；`SETUP_SLACK_MARGIN` **0**/0.01/0.03 | `4_1_cts.json` |
| GR | `do-5_1_grt` | incr, opt | GR 后增量修复：**做**/不做（`SKIP_INCREMENTAL_REPAIR`）；`OPT_POST_GRT_WNS` 0/**1** | `5_1_grt.json` |
| DR | `do-5_2_route … do-finish` | — | 只对最终 incumbent 运行 | `6_report.json`、`5_2_route.json` |

- 默认组合等于 gcd/nangate45 的官方 ORFS 配置。复现值：GR 后 WNS −158.565 ps，finish 后 −155.709 ps，DRC 0。
- 空间共 12×8×12×4 = 4608 种组合。
- 没有调 GR 层容量：nangate45 的 `fastroute.tcl` 写死了层调整，而且它在 floorplan 阶段就被 source，所以层调整其实不是 GR 阶段旋钮。
- QoR 取 GR 后的 setup WNS/TNS（ps）、总功耗（mW）、实例面积（µm²）。
- `pen[s] = −WNS_s`（ps）是阶段 s 结束时的时序损失，对应论文 B(s)"阶段 s 比其已知最好结果落后多少"所需的逐阶段信号。
- `cong` 恒为 0：ORFS 的 GR 溢出会直接导致 make 失败，失败记为 `route_fail`。

## 检查点复用为何与全量重跑等价

- 运行阶段 s 时，只传归属于 ≤ s 阶段的旋钮，其余保持 ORFS 默认。因此"复用上游 .odb + 重跑下游"与
  "同配置从头全跑"喂给每个阶段的输入完全相同。
- `tests/test_orfs.py` 核对两件事：
  - 同一配置不走缓存跑两次，四个阶段的 WNS/TNS/功耗/面积逐位一致（确定性）；
  - 在 CTS 处分叉 == 同配置全跑。
- 每个阶段的检查点放在独立目录。分支从父检查点复制上游文件：用硬链接节省磁盘，文件设为只读；下游一旦改写上游文件会直接报错。
- **结果缓存**：键为（设计，阶段，≤ 该阶段的全部旋钮）。命中时直接取快照，不调用 ORFS。账本里记 `cached=true`：
  - `cost` 仍是第一次实测的秒数，即该方法"本应付出"的代价，方法间比较用它；
  - `wall` 是实际耗时。
  
  缓存依赖上面的确定性。

## 不是什么

- 只是 ORFS 默认脚本 + 少量变量，不是论文的 LLM stage agent。Judge 与各基线仍是 `aura/pd.aura` 中的确定性策略。
- 实验里的 "autotuner" 基线是 pd.aura 自带的"类 AutoTuner"随机局部搜索，**不是** ORFS 自带的 AutoTuner（Ray Tune）。
- 只有 1 个小设计、3 个种子，统计意义有限。
