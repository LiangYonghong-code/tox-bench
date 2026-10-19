# -*- coding: utf-8 -*-
"""agent 链路自检（零模型调用成本、不需要 Docker）：用 mini-swe-agent 的 DeterministicModel
把 eval/phase2_agent.py 的本机接线跑通一遍。

覆盖范围（这些是真跑出来的）：
  * 配置合并：mini 的 default.yaml + eval/mini_config.yaml
  * DefaultAgent / Environment 的构造与 run() 主循环
  * 观测消息渲染、COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT 提交检测
  * 轨迹落盘 trajectory.json、result.json 元数据（含 task_fingerprint）
  * 判分器在 agent 结束后被调用，reward 正确反映交付物状态

不覆盖（必须用 Docker + 真实模型才能验）：
  * DockerEnvironment 起容器、docker commit 快照、tests 事后注入
  * litellm 真实调用与成本统计

用法：
    python build-tools/test_agent_wiring.py [--task 1]
退出码 0 = 链路通；1 = 接线有问题。
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "eval"))

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")   # 关掉 mini 的启动横幅
import common                                        # noqa: E402
import phase2_agent                                  # noqa: E402


def deterministic(outputs):
    from minisweagent.models.test_models import DeterministicModel, make_output
    return DeterministicModel(outputs=[make_output(c, a, cost=0.0) for c, a in outputs]), make_output


def deliverable_paths(task, app_dir):
    """交付物路径从 task.toml 的 [task].deliverables 读，不硬编码 ——
    否则本脚本只能跑 task1，加了第 2 题就会误报"链路有问题"。"""
    names = common.read_task_spec(task).get("task", {}).get("deliverables") or []
    return [Path(app_dir) / Path(n).name for n in names]


def build_script(task, app_dir, correct=True):
    """造一个"确定性模型"：它只执行一条无害命令，然后提交。

    交付文件由本脚本直接写好/删掉 —— 这样测的是 harness 接线，
    而不是 Windows cmd.exe 支不支持 bash heredoc（那是环境差异，不是被测能力）。
    """
    paths = deliverable_paths(task, app_dir)
    assert paths, f"task.toml 未声明 [task].deliverables：{task.spec_file}"
    if correct:
        # 用参考解产出正确答案，作为"模型交上来的"交付物
        ok, output = common.run_oracle_local(task, app_dir)
        assert ok, f"参考解没有产出交付物:\n{output}"
        missing = [p.name for p in paths if not p.exists()]
        assert not missing, f"参考解没有产出交付物: {missing}"
    else:
        for p in paths:
            p.unlink(missing_ok=True)


def run_case(task, name, correct, expect):
    app = common.prepare_local_app(task)
    run_id = f"wiring-{name}"
    run_dir = common.RUNS / run_id
    shutil.rmtree(run_dir, ignore_errors=True)
    try:
        build_script(task, app, correct=correct)
        model, _ = deterministic([
            ("我先确认一下环境。", [{"command": "echo harness-wiring-check"}]),
            ("交付物已就位，提交。",
             [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ])
        record = phase2_agent.run_agent_local(
            task, run_dir, model_override=model, model_name="deterministic-wiring",
            app_dir=str(app), max_steps=5, cost_limit=0.0,
        )
    finally:
        shutil.rmtree(app, ignore_errors=True)

    traj = run_dir / f"task{task.id}" / "trajectory.json"
    reward = record.get("reward")
    checks = {
        "reward": reward == expect,
        "exit_status=Submitted": record.get("exit_status") == "Submitted",
        "steps=2": record.get("steps") == 2,
        "trajectory.json 已落盘": traj.exists() and traj.stat().st_size > 0,
        "result.json 含 task_fingerprint": bool(record.get("task_fingerprint")),
    }
    print(f"\n=== {name}（期望 reward={expect}，实得 {reward}）===")
    for key, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {key}")
    shutil.rmtree(run_dir, ignore_errors=True)
    return all(checks.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, default=None, help="task id，默认全部")
    args = ap.parse_args()

    _, tasks = common.load_tasks(only=[args.task] if args.task else None)
    if not tasks:
        raise SystemExit("没有选中任何题目")

    print(f"agent 链路自检：mini-swe-agent {__import__('minisweagent').__version__}，"
          f"不调用任何真实模型")
    ok = True
    for task in tasks:
        print(f"\n######## task{task.id} {task.slug} ########")
        ok = run_case(task, "correct", correct=True, expect=1) and ok
        ok = run_case(task, "missing", correct=False, expect=0) and ok
    print("\n结论：" + ("链路正常（正确交付物 → 1，缺交付物 → 0）" if ok
                        else "链路有问题，见上面的 FAIL 项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
