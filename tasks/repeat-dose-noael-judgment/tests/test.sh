#!/bin/bash
# 判分入口：从快照镜像起的干净容器里运行。
# 判分器（test_outputs.py）在 agent 结束之后才被注入，agent 看不到任何断言。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${LOG_DIR:-/logs/verifier}"
mkdir -p "$LOG_DIR"

python -m pytest "$SCRIPT_DIR/test_outputs.py" -rA -p no:cacheprovider \
    --ctrf "$LOG_DIR/ctrf.json" 2>&1 | tee "$LOG_DIR/pytest.log"
rc=${PIPESTATUS[0]}

if [ "$rc" -eq 0 ]; then
    echo 1 > "$LOG_DIR/reward.txt"
else
    echo 0 > "$LOG_DIR/reward.txt"
fi

echo "reward=$(cat "$LOG_DIR/reward.txt")"
exit 0
