# OpenROAD 适配器（桩）

`backend.py` 目前**只是接口桩**：它记录真实后端必须实现的 CLI 契约（与 `tools/pdsim.py` / `tools/eco3d.py` 相同），以及接入 ORFS/OpenROAD 时要落实的事项，本身不运行任何 EDA 工具。

Aura 侧已经可替换后端：`aura/common.aura` 读取 `PD_TOOL` / `ECO_TOOL` 环境变量（默认指向合成环境）。实现契约后：

```bash
PD_TOOL=adapters/openroad/backend.py /path/to/run.sh 'scripts/aura.sh aura/pd-main.aura'
```

本仓库没有在任何真实设计上运行过 OpenROAD；所有已报告数字都来自合成环境。
