#!/bin/bash
# 判分入口：从快照镜像起的干净容器里运行。
# 判分器（test_outputs.py）在 agent 结束后才被注入，agent 看不到任何断言。
#
# 约定（与 eval/phase3_verify.py 严格对应）：
#   - 结果只写 $LOG_DIR/reward.txt，内容必须是 0 或 1
#   - 本脚本**永远 exit 0**，所以退出码不能当通过信号，唯一判据是 reward.txt
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${LOG_DIR:-/logs/verifier}"
TASK_APP_DIR="${TASK_APP_DIR:-/app}"
export TASK_APP_DIR
mkdir -p "$LOG_DIR"

if command -v pytest &> /dev/null; then
    python -m pytest "$SCRIPT_DIR/test_outputs.py" -rA -p no:cacheprovider \
        --ctrf "$LOG_DIR/ctrf.json" 2>&1 | tee "$LOG_DIR/pytest.log"
    rc=${PIPESTATUS[0]}
else
    echo "pytest 未安装，使用内置 runner" | tee "$LOG_DIR/pytest.log"
    python - "$SCRIPT_DIR" "$LOG_DIR" <<'PY'
import json, os, sys
script_dir, log_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, script_dir)
import test_outputs
results = []
failed = 0
for name in sorted(n for n in dir(test_outputs) if n.startswith("test_")):
    try:
        getattr(test_outputs, name)()
        results.append({"name": name, "status": "passed"})
        print(f"PASS {name}")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}".splitlines()[0]
        results.append({"name": name, "status": "failed", "message": msg})
        print(f"FAIL {name} - {msg}")
        failed += 1
summary = {
    "tests": len(results), "passed": len(results) - failed,
    "failed": failed, "skipped": 0,
}
with open(os.path.join(log_dir, "ctrf.json"), "w", encoding="utf-8") as f:
    json.dump({"results": {"summary": summary, "tests": results}}, f, ensure_ascii=False, indent=2)
print(f"\n{summary['passed']} passed, {failed} failed (built-in runner)")
sys.exit(0 if failed == 0 else 1)
PY
    rc=$?
fi

if [ "$rc" -eq 0 ]; then
    echo 1 > "$LOG_DIR/reward.txt"
else
    echo 0 > "$LOG_DIR/reward.txt"
fi

echo "reward=$(cat "$LOG_DIR/reward.txt")"
exit 0
