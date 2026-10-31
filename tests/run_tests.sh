#!/usr/bin/env bash
# 运行全部测试：Python 合成环境/验证器单测 + Aura 核心性质测试。
#   AURA_BIN=/path/to/aura tests/run_tests.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 tests/test_envs.py
out=$(scripts/aura.sh tests/aura/core.aura 2>&1) || { echo "$out"; exit 1; }
echo "$out"
grep -q '^ALL-OK$' <<<"$out"
echo "TESTS-OK"
