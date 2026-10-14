# -*- coding: utf-8 -*-
"""用 mini-swe-agent 在任务环境里跑一个模型，再把交付物交给判分器打分。

与 run_task.py 的分工
---------------------
* `run_task.py --mode oracle|base`：证明**判分器**可信（标准解能过、空基线不过）。
* 本文件 `--mode agent`：测量**模型**。用 mini-swe-agent 当 agent 框架，
  让模型在一个 Linux 容器里自己读数据、写代码、产出交付物，然后判分。

容器归属
--------
`minisweagent.environments.docker.DockerEnvironment` 自己 `docker run` 一个容器
（名字形如 `minisweagent-xxxxxxxx`），容器 ID 挂在 `env.container_id` 上。
因此本模块不接管容器生命周期，只在 agent 跑完后用这个 ID 做事后判分：

    agent 跑完 → docker commit 快照 → 从快照起干净验证容器
              → 注入 tests/ → 跑 test.sh → 读 reward.txt → 清理

`--verify-mode snapshot`（默认）就是"判分器与解题环境隔离"的严格版；
`--verify-mode inplace` 直接在 agent 容器里判分，快但不体现隔离。

用法
----
    # 真实模型（默认读全局配置里的 MSWEA_MODEL_NAME，兜底 deepseek/deepseek-chat）
    python eval/agent.py --model deepseek/deepseek-chat --run-id flash-1

    # 不花钱的链路自检：DeterministicModel + 本机环境（见 build-tools/test_agent_wiring.py）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TASK_DIR = REPO / "tasks" / "repeat-dose-noael-judgment"
ENV_DIR = TASK_DIR / "environment"
TESTS_DIR = TASK_DIR / "tests"
INSTRUCTION = TASK_DIR / "instruction.md"
RUNS_DIR = HERE / "runs"
CONFIG_FILE = HERE / "mini_config.yaml"

IMAGE = os.environ.get("TOX_IMAGE", "tox/noael-judgment:base")
VERIFY_CONTAINER = "tox-noael-verify"
SNAPSHOT_IMAGE = IMAGE + "-snapshot"
DEFAULT_MODEL = os.getenv("MSWEA_MODEL_NAME") or "deepseek/deepseek-chat"

# agent 侧要收集回 run 目录的产物
ARTIFACTS = ["/app/judgment.json", "/app/report.md",
             "/logs/verifier/ctrf.json", "/logs/verifier/pytest.log"]


# ------------------------------------------------------------------ docker 封装
def docker(*args, check=False):
    r = subprocess.run(["docker", *[str(a) for a in args]], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(str(a) for a in args)} 失败：\n{r.stdout}{r.stderr}")
    return r


# ------------------------------------------------------------------ 配置 / 组件
def load_config():
    """mini 内置 default.yaml + 本仓库 eval/mini_config.yaml 的递归合并。"""
    import yaml
    from minisweagent import package_dir
    from minisweagent.utils.serialize import recursive_merge

    base = yaml.safe_load((Path(package_dir) / "config" / "default.yaml").read_text(encoding="utf-8"))
    over = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    note = over.pop("harness_note", "")      # 不传给 mini 的构造函数
    return recursive_merge(base, over), note


def build_image(image=IMAGE):
    r = docker("build", "-t", image, ENV_DIR)
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        raise SystemExit("docker build 失败")
    return image


def make_model(model_name, config, override=None):
    if override is not None:                 # 测试注入（DeterministicModel 等）
        return override
    from minisweagent.models.litellm_model import LitellmModel
    return LitellmModel(model_name=model_name, **config.get("model", {}))


def make_env(runner, image, config, app_dir=None):
    env_cfg = dict(config.get("environment", {}))
    if runner == "docker":
        from minisweagent.environments.docker import DockerEnvironment
        return DockerEnvironment(image=image, cwd="/app", **env_cfg)
    from minisweagent.environments.local import LocalEnvironment
    for key in ("container_timeout", "run_args", "interpreter", "pull_timeout"):
        env_cfg.pop(key, None)               # LocalEnvironmentConfig 没有这些字段
    return LocalEnvironment(cwd=str(app_dir), **env_cfg)


# ------------------------------------------------------------------ 判分
def collect(container, run_dir):
    """把交付物与判分产物拷回 run 目录（缺失不致命）。"""
    for src in ARTIFACTS:
        docker("cp", f"{container}:{src}", str(run_dir / Path(src).name))


def judge_docker(agent_container, run_dir, verify_mode="snapshot"):
    """在（快照起的）验证容器里注入 tests 并判分，返回 (reward, log, 验证容器名)。"""
    target = agent_container
    if verify_mode == "snapshot":
        docker("commit", agent_container, SNAPSHOT_IMAGE, check=True)
        docker("rm", "-f", VERIFY_CONTAINER)
        docker("run", "-d", "--name", VERIFY_CONTAINER, SNAPSHOT_IMAGE, "sleep", "600", check=True)
        target = VERIFY_CONTAINER
    # 判分器在 agent 结束之后才注入 —— agent 全程看不到断言
    docker("cp", TESTS_DIR, f"{target}:/app/", check=True)
    verify = docker("exec", target, "bash", "/app/tests/test.sh")
    raw = docker("exec", target, "cat", "/logs/verifier/reward.txt").stdout.strip()
    collect(target, run_dir)
    return int(raw or 0), (verify.stdout or "") + (verify.stderr or ""), target


def judge_local(app_dir, run_dir):
    """本机链路自检用：直接调用 run_task 的判分器。"""
    sys.path.insert(0, str(HERE))
    import run_task
    ok, output = run_task.run_tests(str(app_dir))
    for name in ("judgment.json", "report.md"):
        src = Path(app_dir) / name
        if src.exists():
            (run_dir / name).write_bytes(src.read_bytes())
    return (1 if ok else 0), output


# ------------------------------------------------------------------ 主流程
def run_agent(runner="docker", model_name=DEFAULT_MODEL, run_id=None, max_steps=40,
              cost_limit=2.0, wall_time=1800, cmd_timeout=None, verify_mode="snapshot",
              keep_container=False, model_override=None, app_dir=None, build=True,
              extra_note="", allow_local_model=False):
    from minisweagent.agents.default import DefaultAgent

    # 安全闸：local runner 用 LocalEnvironment(shell=True)，模型输出的命令会直接在你本机执行。
    # 只有注入确定性测试模型（接线自检）才允许；真实模型必须走 Docker。
    if runner == "local" and model_override is None and not allow_local_model:
        raise SystemExit(
            "拒绝执行：--runner local 会让模型生成的 bash 命令直接在你本机运行。\n"
            "请改用 --runner docker；若确实要这么做，显式加 --i-know-local-runs-on-host。"
        )

    config, note = load_config()
    if cmd_timeout:
        config.setdefault("environment", {})["timeout"] = cmd_timeout

    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 题面 + harness 约定（事实性约定，不含任何判定规则）
    task = INSTRUCTION.read_text(encoding="utf-8") + "\n\n" + (note or "") + extra_note

    local_app = None
    if runner == "local":
        sys.path.insert(0, str(HERE))
        import run_task
        local_app = Path(app_dir) if app_dir else Path(run_task.prepare_local_app())

    image = build_image() if (runner == "docker" and build) else IMAGE
    env = make_env(runner, image, config, app_dir=local_app)
    model = make_model(model_name, config, override=model_override)

    agent = DefaultAgent(
        model, env,
        system_template=config["agent"]["system_template"],
        instance_template=config["agent"]["instance_template"],
        step_limit=max_steps,
        cost_limit=cost_limit,
        wall_time_limit_seconds=wall_time,
        output_path=run_dir / "trajectory.json",
    )
    container = getattr(env, "container_id", None)
    print(f"[agent] runner={runner} model={model_name} "
          f"max_steps={max_steps} cost_limit=${cost_limit} container={container}")

    started = time.time()
    try:
        result = agent.run(task)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[agent] agent.run 抛出异常：{type(exc).__name__}: {exc}")
        result = {"exit_status": type(exc).__name__, "submission": ""}
    elapsed = time.time() - started

    # ---- 判分
    judge_log = ""
    if runner == "docker":
        reward, judge_log, target = judge_docker(container, run_dir, verify_mode)
    else:
        reward, judge_log = judge_local(local_app, run_dir)
        target = None

    # ---- 存档
    record = {
        "run_id": run_id,
        "runner": runner,
        "model": model_name,
        "image": image,
        "verify_mode": verify_mode if runner == "docker" else "n/a",
        "exit_status": result.get("exit_status", ""),
        "reward": reward,
        "steps": agent.n_calls,
        "cost_usd": round(agent.cost, 6),
        "elapsed_sec": round(elapsed, 1),
        "max_steps": max_steps,
        "cost_limit_usd": cost_limit,
        "task_fingerprint": task_fingerprint(),
        "container_id": container,
    }
    (run_dir / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    if judge_log:
        (run_dir / "verify.log").write_text(judge_log, encoding="utf-8")

    # ---- 清理
    if runner == "docker":
        docker("rm", "-f", VERIFY_CONTAINER)
        env.cleanup()
        if not keep_container:
            docker("rm", "-f", container)
            docker("rmi", SNAPSHOT_IMAGE)
    else:
        print(f"[agent] 本机环境下的工作目录保留在 {local_app}")

    print(f"\n[agent] reward = {reward} | 步数 {agent.n_calls} | "
          f"成本 ${agent.cost:.4f} | 耗时 {elapsed:.0f}s | exit={record['exit_status']}")
    print(f"[agent] 轨迹与产物：{run_dir}")
    return reward, record


def task_fingerprint():
    """题目指纹：题面 + SOP + 数据。用来判断两次 run 是否可比。"""
    import hashlib
    h = hashlib.sha256()
    for path in (INSTRUCTION, ENV_DIR / "SOP_toxicity_criteria.md", ENV_DIR / "study.db"):
        h.update(Path(path).read_bytes())
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="用 mini-swe-agent 跑模型并判分")
    ap.add_argument("--runner", choices=["docker", "local"], default="docker")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"默认 {DEFAULT_MODEL}")
    ap.add_argument("--run-id", default=None, help="轨迹与产物的存档目录名")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--cost-limit", type=float, default=2.0, help="美元上限")
    ap.add_argument("--wall-time", type=int, default=1800, help="墙钟上限（秒）")
    ap.add_argument("--cmd-timeout", type=int, default=None, help="单条容器命令超时（秒）")
    ap.add_argument("--verify-mode", choices=["snapshot", "inplace"], default="snapshot")
    ap.add_argument("--no-build", action="store_true", help="复用已存在的镜像")
    ap.add_argument("--keep-container", action="store_true", help="保留容器与快照镜像以便排查")
    ap.add_argument("--i-know-local-runs-on-host", action="store_true",
                    help="允许真实模型在 --runner local 下直接在你本机执行命令（危险）")
    args = ap.parse_args()

    reward, _ = run_agent(
        runner=args.runner, model_name=args.model, run_id=args.run_id,
        max_steps=args.max_steps, cost_limit=args.cost_limit, wall_time=args.wall_time,
        cmd_timeout=args.cmd_timeout, verify_mode=args.verify_mode,
        keep_container=args.keep_container, build=not args.no_build,
        allow_local_model=args.i_know_local_runs_on_host,
    )
    return 0 if reward in (0, 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
