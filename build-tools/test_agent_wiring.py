# -*- coding: utf-8 -*-
"""agent 链路自检（不花钱、不需要 Docker）：用 mini-swe-agent 的 DeterministicModel
把 eval/agent.py 的接线跑通一遍。

覆盖范围（这些是真跑出来的）：
  * 配置合并：mini 的 default.yaml + eval/mini_config.yaml
  * DefaultAgent / Environment 的构造与 run() 主循环
  * 观测消息渲染、COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT 提交检测
  * 轨迹落盘 trajectory.json、run.json 元数据
  * 判分器在 agent 结束后被调用，reward 正确反映交付物状态

不覆盖（必须用 Docker + 真实模型才能验）：
  * DockerEnvironment 起容器、docker commit 快照、tests 事后注入
  * litellm 真实调用与成本统计

用法：
    python build-tools/test_agent_wiring.py
退出码 0 = 链路通；1 = 接线有问题。
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "eval"))

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")   # 关掉 mini 的启动横幅
import agent as agent_mod                            # noqa: E402
import run_task                                      # noqa: E402


def deterministic(outputs):
    from minisweagent.models.test_models import DeterministicModel, make_output
    return DeterministicModel(outputs=[make_output(c, a, cost=0.0) for c, a in outputs]), make_output


def build_script(app_dir, correct=True):
    """造一个"确定性模型"：它只执行一条无害命令，然后提交。

    交付文件由本脚本直接写好/删掉 —— 这样测的是 harness 接线，
    而不是 Windows cmd.exe 支不支持 bash heredoc（那是环境差异，不是被测能力）。
    """
    judgment = Path(app_dir) / "judgment.json"
    report = Path(app_dir) / "report.md"
    if correct:
        # 用参考解产出正确答案，作为"模型交上来的"交付物
        payload = run_task.extract_payload(run_task.SOLVE)
        script = Path(app_dir) / "_solve.py"
        script.write_text(payload, encoding="utf-8")
        import subprocess
        subprocess.run([sys.executable, str(script)], check=True,
                       capture_output=True, env=dict(os.environ, APP_DIR=str(app_dir)))
        script.unlink()
        assert judgment.exists() and report.exists(), "参考解没有产出交付物"
    else:
        judgment.unlink(missing_ok=True)
        report.unlink(missing_ok=True)


def run_case(name, correct, expect):
    app = Path(run_task.prepare_local_app())
    build_script(app, correct=correct)
    model, make_output = deterministic([
        ("我先确认一下环境。", [{"command": "echo harness-wiring-check"}]),
        ("交付物已就位，提交。",
         [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
    ])
    try:
        reward, record = agent_mod.run_agent(
            runner="local", model_name="deterministic-wiring", run_id=f"wiring-{name}",
            max_steps=5, cost_limit=0.0, app_dir=str(app), model_override=model,
        )
    finally:
        shutil.rmtree(app, ignore_errors=True)

    run_dir = agent_mod.RUNS_DIR / f"wiring-{name}"
    traj = run_dir / "trajectory.json"
    checks = {
        "reward": reward == expect,
        "exit_status=Submitted": record["exit_status"] == "Submitted",
        "steps=2": record["steps"] == 2,
        "trajectory.json 已落盘": traj.exists() and traj.stat().st_size > 0,
        "run.json 含 task_fingerprint": bool(record.get("task_fingerprint")),
    }
    print(f"\n=== {name}（期望 reward={expect}，实得 {reward}）===")
    for key, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {key}")
    shutil.rmtree(run_dir, ignore_errors=True)
    return all(checks.values())


def main():
    print(f"agent 链路自检：mini-swe-agent {__import__('minisweagent').__version__}，"
          f"不调用任何真实模型")
    ok_correct = run_case("correct", correct=True, expect=1)
    ok_missing = run_case("missing", correct=False, expect=0)
    ok = ok_correct and ok_missing
    print("\n结论：" + ("链路正常（正确交付物 → 1，缺交付物 → 0）" if ok
                        else "链路有问题，见上面的 FAIL 项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
