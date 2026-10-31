#!/usr/bin/env python3
"""宿主机侧小桥：Aura 跑在没有 docker 的开发容器里时，backend.py（ORFS_EXEC=bridge）把 ORFS 命令写进
ORFS_WORK/queue/*.job，本进程逐个用 docker exec 在 ORFS 容器里执行，并写回 *.done（退出码）。

  python3 adapters/openroad/bridge.py &      # 在宿主机上
"""
import glob, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backend

q = os.path.join(backend.WORK, "queue"); os.makedirs(q, exist_ok=True)
while True:
    jobs = sorted(glob.glob(os.path.join(q, "*.job")))
    if not jobs:
        time.sleep(0.3); continue
    j = jobs[0]; d = json.load(open(j)); os.remove(j)
    rc = backend.run_in_container(d["cmd"], d["log"])
    tmp = j[:-4] + ".dtmp"; open(tmp, "w").write(str(rc)); os.rename(tmp, j[:-4] + ".done")
