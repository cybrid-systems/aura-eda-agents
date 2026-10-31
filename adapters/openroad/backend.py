#!/usr/bin/env python3
"""OpenROAD / ORFS 适配器桩（未实现）。

目标：与 tools/pdsim.py、tools/eco3d.py 保持**相同的 CLI 契约**，从而 aura/*.aura
无需修改即可把合成环境换成真实后端（设置 PD_TOOL / ECO_TOOL 环境变量指向本脚本）。

契约（与合成环境完全相同；所有结果写入 --out 指定的 JSON 文件）：
  PD（替换 tools/pdsim.py，设置 PD_TOOL=adapters/openroad/backend.py）：
    space  --out F
    chain  --design D --parent CKPT|root --knobs idx:a,b,c,d --from FP|PL|CTS|GR
           --outdir DIR --ledger L.jsonl --out F    # 复用 parent 中 --from 之前的阶段，只重跑其后
    dr     --ckpt CKPT --ledger L.jsonl --out F    # 只对 incumbent 调用
    ledger --glob 'G' --out F
  ECO（替换 tools/eco3d.py，设置 ECO_TOOL=...）：
    query  --ckpt C --kind drc|netlist|hb_graph|layout_vpix [--nets N] --out F   # 只读
    apply  --ckpt C --program P.json --out C2 --report R.json
    verify --before C --after C2 --out F [--allowed @R.json|'*'] [--target-file T]
    stats  --before C --after C2 --out F
    fullreroute|stockrepair --ckpt C --out C2   # 基线
  JSON 字段以合成环境的输出为准（见 tools/pdsim.py、tools/eco3d.py）。

真实后端时需要落实的事项（均为 TODO，尚未在任何真实设计上验证）：
  * PD 阶段检查点：ORFS 每阶段产出 .odb，可作为 COW 上游复用的物理载体；
    具体文件名/目标名随 ORFS 版本而异，接入时以所用版本为准。
  * 阶段旋钮到 ORFS 变量的映射（如 CORE_UTILIZATION、PLACE_DENSITY、CTS_CLUSTER_SIZE 等），
    需对照所用 ORFS 版本文档逐一确认，不在此硬编码。
  * WNS/TNS 读取：从 OpenSTA 报告解析；GR 后作为环内信号，DR 只对 incumbent 运行
    （AgenticPD 的做法，见 docs/papers.md）。
  * ECO：需要 3D-IC/HBT 的 DRC 规则、线网几何哈希（用于保留性检查）与签核脚本；
    AgenticECO 的公开实现（BSD-3）可作为参照，本仓库未复制其代码。
"""
import sys

def main():
    sys.stderr.write("adapters/openroad/backend.py: 尚未实现，见文件头注释。\n")
    return 2

if __name__ == "__main__":
    sys.exit(main())
