# -*- coding: utf-8 -*-
"""Phase 2：让 mini-swe-agent 在容器里解题，跑完把容器状态 commit 成快照镜像。

用法：
    # 批量（每题一个子进程，带外层超时兜底）
    python eval/phase2_agent.py --run-id main
    python eval/phase2_agent.py --run-id main --only 1 --model your-provider/your-model

    # 单题（驱动脚本内部用，也可手动调试）
    python eval/phase2_agent.py --run-id main --single 1

    # 本机链路自检（零模型调用成本、不需要 Docker）：由 build-tools/test_agent_wiring.py 驱动
    python eval/phase2_agent.py --run-id wiring --single 1 --local \
        --model your-provider/your-model --i-know-local-runs-on-host

架构
----
agent 进程跑在宿主机，通过 mini-swe-agent 的 DockerEnvironment 用 `docker exec`
把每条 bash 命令送进任务容器执行。容器的 cleanup 会销毁容器，所以**必须在
env 析构之前 `docker commit`**，否则 agent 的成果会连同容器一起消失。
`finally` 里的顺序是硬约束：docker diff → commit → stop → cleanup。

为什么快照在 Phase 2 而不是 Phase 3
-----------------------------------
Phase 3 是**另一个进程**，启动时 agent 容器早已不存在。快照是"run-id → 可判分状态"
唯一能跨进程传递的持久载体。因此快照镜像**不在 Phase 2 结束时删除** —— 删了 Phase 3
就没得判、Phase 4 也无从回查。清理是离线运维的事（按 `solved-` 前缀挑旧的删）。

产物（每题一个目录）：
    eval/runs/<run-id>/taskN/
        prompt.md          本次实际喂给模型的完整题面（题面 + harness 约定）
        trajectory.json    agent 的完整线性轨迹
        agent.log          mini-swe-agent 的运行日志
        container.id       容器 ID（审计用）
        docker-diff.txt    容器内改动文件清单
        snapshot.tag       commit 出来的快照镜像名
        result.json        步数/成本/结束原因/指纹/是否成功快照
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from common import (
    REPO,
    Task,
    die,
    docker,
    docker_ready,
    judge_local,
    load_tasks,
    log,
    make_run_id,
    new_run_dir,
    parse_only,
    prepare_local_app,
    read_json,
    resolve_workdir,
    spec_run_args,
    spec_timeout,
    task_fingerprint,
    task_out,
    write_json,
)

DEFAULT_MAX_STEPS = 40        # 有意的测量口径：不改成"不限步"
DEFAULT_COST_LIMIT = 2.0
DEFAULT_WALL_TIME = 1800      # 仅当 task.toml 未声明 agent_timeout_sec 时的兜底
CONFIG_FILE = Path(__file__).resolve().parent / "mini_config.yaml"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

def default_model() -> str:
    """读取默认模型名。

    注意顺序：mini 的全局配置（`<user_config_dir>/mini-swe-agent/.env`，里面有
    MSWEA_MODEL_NAME 与各家 API key）是在 `import minisweagent` 时用 dotenv 加载的，
    所以必须**先 import 再读环境变量** —— 在模块顶层读会读不到。

    为了不把任何模型硬编码进 harness，这里不再提供 fallback 默认值。
    必须通过 `--model` 参数或 `MSWEA_MODEL_NAME` 环境变量显式指定。
    """
    try:
        import minisweagent  # noqa: F401  触发全局 .env 加载
    except ImportError:
        pass
    model = os.getenv("MSWEA_MODEL_NAME")
    if not model:
        raise SystemExit(
            "必须指定模型：通过 --model 参数或环境变量 MSWEA_MODEL_NAME 设置。\n"
            "例如：--model your-provider/your-model"
        )
    return model


def load_config() -> tuple[dict, str]:
    """mini 内置 default.yaml + 本仓库 eval/mini_config.yaml 的递归合并。

    刻意**不用** `get_config_from_spec("mini.yaml")`：`mini.yaml` 与 `default.yaml`
    的动作格式不同（前者是 tool-calling，后者是 ```mswea_bash_command 代码块），
    而 harness_note 给出的结束命令是代码块形式。换模板会让测出来的分数变成
    "模型熟不熟这套 prompt"，而不是"模型的毒理判定能力"。
    """
    import yaml
    from minisweagent import package_dir
    from minisweagent.utils.serialize import recursive_merge

    base = yaml.safe_load(
        (Path(package_dir) / "config" / "default.yaml").read_text(encoding="utf-8"))
    over = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    note = over.pop("harness_note", "")      # 不传给 mini 的构造函数
    return recursive_merge(base, over), note


def resolve_wall_time(task: Task, requested: int | None) -> int:
    """墙钟上限：CLI 显式给了就用，否则取 task.toml，最后兜底 DEFAULT_WALL_TIME。

    这样 task.toml 成为超时的单一来源（此前它声明 7200，CLI 却默认 1800，两者打架）。
    """
    return requested if requested else spec_timeout(task, "agent", DEFAULT_WALL_TIME)


def build_prompt(task: Task, note: str) -> str:
    """题面 + harness 约定（事实性约定，不含任何判定规则）。"""
    note = (note or "").replace("{workdir}", task.workdir)
    return task.instruction_file.read_text(encoding="utf-8") + "\n\n" + note


def build_docker_config(task: Task, workdir: str, out: Path, cfg: dict,
                        model_name: str, args, wall_time: int) -> dict:
    """把合并好的配置改写成 DockerEnvironment 可用的形态。

    get_environment 会把 config 里的键原样当构造参数传给环境类，所以这里
    只能出现 DockerEnvironmentConfig 认识的字段。
    """
    env_cfg = dict(cfg.get("environment", {}))
    env_cfg |= {
        "environment_class": "docker",
        "image": task.base_image,
        "cwd": workdir,                # 必须显式给，否则 mini 默认 "/" 会盖掉镜像 WORKDIR
        "run_args": ["--rm", *spec_run_args(task)],
    }

    agent_cfg = dict(cfg.get("agent", {}))
    agent_cfg |= {
        "step_limit": args.max_steps,
        "cost_limit": args.cost_limit,
        "wall_time_limit_seconds": wall_time,
        "output_path": str(out / "trajectory.json"),
    }

    model_cfg = dict(cfg.get("model", {}))
    # 必须用文本协议版（litellm_textbased），不能是工具调用版（litellm）：
    # 模板取自 default.yaml，它用的是 mswea_bash_command 代码块协议；而 litellm 版
    # 会给 litellm 传 tools=[BASH_TOOL]、并且只解析原生 tool_calls。两者错配时，
    # 模型照题面写出代码块反而被解析器判为"没有动作"，回喂的 format_error_template
    # 又是文本版的、把它继续往同一个方向推 —— 实测一次运行 40 次 API 调用里有 23 次
    # 是这种格式错误，白吃掉一半以上步数预算（步数是有意的测量口径，不能靠调松掩盖）。
    model_cfg |= {"model_class": "litellm_textbased", "model_name": model_name}

    return {"agent": agent_cfg, "environment": env_cfg, "model": model_cfg}


def build_local_config(app: Path, out: Path, cfg: dict, model_name: str,
                       args, wall_time: int) -> dict:
    """本机环境版：LocalEnvironmentConfig 只有 cwd/env/timeout，其余字段要剔除。"""
    env_cfg = {k: v for k, v in cfg.get("environment", {}).items()
               if k in ("cwd", "env", "timeout")}
    env_cfg |= {"environment_class": "local", "cwd": str(app)}

    agent_cfg = dict(cfg.get("agent", {}))
    agent_cfg |= {
        "step_limit": args.max_steps,
        "cost_limit": args.cost_limit,
        "wall_time_limit_seconds": wall_time,
        "output_path": str(out / "trajectory.json"),
    }

    model_cfg = dict(cfg.get("model", {}))
    # 必须用文本协议版（litellm_textbased），不能是工具调用版（litellm）：
    # 模板取自 default.yaml，它用的是 mswea_bash_command 代码块协议；而 litellm 版
    # 会给 litellm 传 tools=[BASH_TOOL]、并且只解析原生 tool_calls。两者错配时，
    # 模型照题面写出代码块反而被解析器判为"没有动作"，回喂的 format_error_template
    # 又是文本版的、把它继续往同一个方向推 —— 实测一次运行 40 次 API 调用里有 23 次
    # 是这种格式错误，白吃掉一半以上步数预算（步数是有意的测量口径，不能靠调松掩盖）。
    model_cfg |= {"model_class": "litellm_textbased", "model_name": model_name}

    return {"agent": agent_cfg, "environment": env_cfg, "model": model_cfg}


def attach_file_logger(path: Path) -> None:
    """把 mini-swe-agent 的 logger 输出写进文件。

    注意：DefaultAgent 用的是 logging.getLogger("agent")，不在 "minisweagent" 命名空间下，
    两个都要挂。agent 那条会把每步的完整 messages 以 DEBUG 级别 dump 出来，所以 agent.log
    会比 trajectory.json 更啰嗦 —— 但有纯文本副本，JSON 解析失败时能兜底。
    """
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name in ("minisweagent", "agent"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.addHandler(fh)


def make_model(model_cfg: dict, override=None):
    if override is not None:                 # 测试注入（DeterministicModel 等）
        return override
    from minisweagent.models import get_model
    return get_model(config=model_cfg)


def usage_of(agent, traj_file: Path) -> dict:
    """步数与成本：优先取 agent 对象上的实时值，失败时回退到轨迹里的 model_stats。"""
    out: dict = {}
    for attr, key in (("n_calls", "steps"), ("cost", "cost")):
        if hasattr(agent, attr):
            out[key] = getattr(agent, attr)
    if out.get("steps") is None or out.get("cost") is None:
        try:
            stats = read_json(traj_file)["info"]["model_stats"]
            out["steps"] = out.get("steps") or stats.get("api_calls")
            out["cost"] = out.get("cost") or stats.get("instance_cost")
        except Exception:  # noqa: BLE001
            pass
    return out


# --------------------------------------------------------------------------- #
# 单题：Docker
# --------------------------------------------------------------------------- #

def run_single_docker(task: Task, run_dir: Path, args) -> dict:
    from minisweagent.agents import get_agent
    from minisweagent.environments import get_environment

    out = task_out(run_dir, task)
    workdir = resolve_workdir(task, run_dir)
    wall_time = resolve_wall_time(task, args.wall_time)
    cfg, note = load_config()
    prompt = build_prompt(task, note)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    attach_file_logger(out / "agent.log")

    model_name = args.model or default_model()
    result: dict = {
        "task": task.id,
        "slug": task.slug,
        "runner": "docker",
        "image": task.base_image,
        "workdir": workdir,
        "model": model_name,
        "max_steps": args.max_steps,
        "cost_limit_usd": args.cost_limit,
        "wall_time_sec": wall_time,
        "task_fingerprint": task_fingerprint(task),
    }

    conf = build_docker_config(task, workdir, out, cfg, model_name, args, wall_time)
    log(f"task{task.id}: 启动容器 {task.base_image} "
        f"(cwd={workdir}, run_args={conf['environment']['run_args']})")

    env = get_environment(conf["environment"])
    agent = get_agent(make_model(conf["model"]), env, conf["agent"], default_type="default")
    cid = env.container_id
    (out / "container.id").write_text(cid, encoding="utf-8")
    result["container_id"] = cid[:12]

    t0 = time.time()
    run_result: dict = {}
    try:
        run_result = agent.run(prompt) or {}
    except SystemExit as exc:                      # agent 主动退出
        result["error"] = f"SystemExit({exc.code})"
    except BaseException as exc:                   # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"task{task.id}: agent 运行中断 -> {result['error']}")
    finally:
        result["agent_seconds"] = round(time.time() - t0, 1)
        result["exit_status"] = run_result.get("exit_status", "")

        # ---- 关键顺序：先快照，后 cleanup（cleanup 会销毁容器）----
        try:
            diff = docker("diff", cid, timeout=120)
            (out / "docker-diff.txt").write_text(
                diff.stdout or diff.stderr or "", encoding="utf-8")
            result["changed_files"] = len(
                [ln for ln in (diff.stdout or "").splitlines() if ln.strip()])
        except Exception as exc:  # noqa: BLE001
            log(f"task{task.id}: docker diff 失败 {exc!r}")

        snap = task.solved_image(args.run_id)
        try:
            p = docker("commit", cid, snap, timeout=600)
            if p.returncode == 0:
                (out / "snapshot.tag").write_text(snap, encoding="utf-8")
                result["snapshot"] = snap
                result["snapshot_ok"] = True
                log(f"task{task.id}: 已快照 -> {snap}")
            else:
                result["snapshot_ok"] = False
                result["snapshot_error"] = (p.stderr or p.stdout or "")[-800:]
                log(f"task{task.id}: 快照失败 {(p.stderr or '')[:200]}")
        except Exception as exc:  # noqa: BLE001
            result["snapshot_ok"] = False
            result["snapshot_error"] = repr(exc)
            log(f"task{task.id}: 快照异常 {exc!r}")

        # 容器回收必须由我们自己保证。mini 的 cleanup() 用 shell=True 执行
        # `(timeout 60 docker stop X || docker rm -f X) >/dev/null 2>&1 &`，这在 Windows
        # 的 cmd.exe 下无法解析 —— 只会打印一行 "The system cannot find the path
        # specified." 然后什么都不做；而且 DockerEnvironment.__del__ 会在 GC 时**再**
        # 触发一次 cleanup()，绕开任何调用侧的分支判断。
        # 所以：自己 stop + rm（幂等，比 --rm 更硬），然后把 container_id 置空 ——
        # cleanup() 的入口就是 `if container_id is not None`，置空后 cleanup() 与
        # __del__ 都变成静默的空操作。
        for sub in (("stop", cid), ("rm", "-f", cid)):
            try:
                docker(*sub, timeout=120)
            except Exception:  # noqa: BLE001
                pass
        try:
            env.container_id = None
        except Exception:  # noqa: BLE001
            pass

    # 轨迹与用量
    traj_file = out / "trajectory.json"
    try:
        agent.save(traj_file)
    except Exception as exc:  # noqa: BLE001
        log(f"task{task.id}: 保存轨迹失败 {exc!r}")
    result["trajectory_exists"] = traj_file.exists()
    result |= usage_of(agent, traj_file)

    write_json(out / "result.json", result)
    return result


# --------------------------------------------------------------------------- #
# 单题：本机（链路自检）
# --------------------------------------------------------------------------- #

def run_agent_local(task: Task, run_dir: Path, *, model_override=None, model_name: str = "",
                    app_dir=None, max_steps: int = DEFAULT_MAX_STEPS,
                    cost_limit: float = DEFAULT_COST_LIMIT, wall_time: int | None = None,
                    allow_local_model: bool = False, out: Path | None = None) -> dict:
    """在本机目录里跑 agent 并判分。供 build-tools/test_agent_wiring.py 注入
    DeterministicModel 使用（真实模型走 Docker，见模块 docstring）。"""
    from minisweagent.agents import get_agent
    from minisweagent.environments import get_environment

    # 安全闸：LocalEnvironment 会把模型输出的命令直接在你本机执行。
    # 只有注入确定性测试模型（接线自检）才允许；真实模型必须走 Docker。
    if model_override is None and not allow_local_model:
        raise SystemExit(
            "拒绝执行：本机 runner 会让模型生成的 bash 命令直接在你本机运行。\n"
            "请改用 Docker（去掉 --local）；若确实要这么做，显式加 --i-know-local-runs-on-host。"
        )

    out = out or task_out(run_dir, task)
    out.mkdir(parents=True, exist_ok=True)
    created_app = app_dir is None
    app = Path(app_dir) if app_dir else prepare_local_app(task)
    wall_time = resolve_wall_time(task, wall_time)
    cfg, note = load_config()
    prompt = build_prompt(task, note)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    model_name = model_name or "deterministic-wiring"

    result: dict = {
        "task": task.id,
        "slug": task.slug,
        "runner": "local",
        "image": None,
        "workdir": str(app),
        "model": model_name,
        "max_steps": max_steps,
        "cost_limit_usd": cost_limit,
        "wall_time_sec": wall_time,
        "task_fingerprint": task_fingerprint(task),
    }

    class _Args:
        pass

    a = _Args()
    a.max_steps, a.cost_limit = max_steps, cost_limit
    conf = build_local_config(app, out, cfg, model_name, a, wall_time)

    env = get_environment(conf["environment"])
    agent = get_agent(make_model(conf["model"], override=model_override), env,
                      conf["agent"], default_type="default")

    t0 = time.time()
    run_result: dict = {}
    try:
        run_result = agent.run(prompt) or {}
    except BaseException as exc:                   # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["agent_seconds"] = round(time.time() - t0, 1)
        result["exit_status"] = run_result.get("exit_status", "")
        try:
            env.cleanup()
        except Exception:  # noqa: BLE001
            pass

    reward, judge_log, _ctrf = judge_local(task, app)
    result["reward"] = reward
    result["passed"] = (reward == 1)
    if judge_log:
        (out / "verify.log").write_text(judge_log, encoding="utf-8")

    traj_file = out / "trajectory.json"
    try:
        agent.save(traj_file)
    except Exception as exc:  # noqa: BLE001
        log(f"task{task.id}: 保存轨迹失败 {exc!r}")
    result["trajectory_exists"] = traj_file.exists()
    result |= usage_of(agent, traj_file)

    if created_app:
        import shutil
        shutil.rmtree(app, ignore_errors=True)

    write_json(out / "result.json", result)
    return result


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #

def driver(run_dir: Path, tasks: list[Task], args) -> int:
    """批量模式：每题一个子进程，外层超时 = agent 墙钟上限 + 缓冲。"""
    results = []
    for task in tasks:
        log(f"===== task{task.id} ({task.slug}) =====")
        wall = resolve_wall_time(task, args.wall_time)
        cmd = [
            sys.executable, str(Path(__file__).resolve()),
            "--run-id", args.run_id, "--single", str(task.id),
            "--model", args.model or default_model(),
            "--max-steps", str(args.max_steps),
            "--cost-limit", str(args.cost_limit),
            "--wall-time", str(wall),
        ]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=str(REPO), timeout=wall + 600,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            for ln in (p.stdout or "").strip().splitlines()[-3:]:
                print(f"    {ln}", flush=True)
            if p.returncode != 0:
                log(f"task{task.id}: 子进程退出码 {p.returncode}")
                if p.stderr:
                    print(f"    [stderr] {p.stderr.strip().splitlines()[-1][:300]}", flush=True)
        except subprocess.TimeoutExpired:
            log(f"task{task.id}: 外层超时 {wall + 600}s，已强杀")
        log(f"task{task.id}: 子进程耗时 {time.time() - t0:.0f}s")

        rf = run_dir / f"task{task.id}" / "result.json"
        if rf.exists():
            results.append(read_json(rf))
    write_json(run_dir / "_agent_summary.json", results)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--only", default=None, help="逗号分隔的 task id")
    ap.add_argument("--single", type=int, default=None, help="内部用：只跑这一题")
    ap.add_argument("--model", default=None,
                    help="模型名；未指定时读取 MSWEA_MODEL_NAME 环境变量，两者都未设置则报错")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    ap.add_argument("--cost-limit", type=float, default=DEFAULT_COST_LIMIT, help="美元上限")
    ap.add_argument("--wall-time", type=int, default=None,
                    help="墙钟上限（秒）；不给则取 task.toml 的 agent_timeout_sec")
    ap.add_argument("--local", action="store_true",
                    help="不走 Docker：在本机目录里跑（仅供链路自检）")
    ap.add_argument("--i-know-local-runs-on-host", action="store_true",
                    help="允许真实模型在本机直接执行命令（危险）")
    args = ap.parse_args()

    args.run_id = args.run_id or make_run_id()
    run_dir = new_run_dir(args.run_id)

    _, tasks = load_tasks(only=parse_only(args.only))
    if args.single is not None:
        tasks = [t for t in tasks if t.id == args.single]
        if not tasks:
            die(f"清单里没有 task{args.single}")

    if args.local:
        if len(tasks) != 1:
            die("--local 只能配合 --single/--only 单题使用")
        res = run_agent_local(tasks[0], run_dir,
                              model_name=args.model or default_model(),
                              max_steps=args.max_steps, cost_limit=args.cost_limit,
                              wall_time=args.wall_time,
                              allow_local_model=args.i_know_local_runs_on_host)
        log(f"task{tasks[0].id}: reward={res.get('reward')} steps={res.get('steps')} "
            f"cost={res.get('cost')} exit={res.get('exit_status')}")
        return 0

    ok, info = docker_ready()
    if not ok:
        die(f"Docker 引擎未就绪：{info}\n请先启动 Docker Desktop 并等它完成初始化。")

    if args.single is not None:
        res = run_single_docker(tasks[0], run_dir, args)
        log(f"task{tasks[0].id}: steps={res.get('steps')} cost={res.get('cost')} "
            f"snapshot_ok={res.get('snapshot_ok')} error={res.get('error')}")
        return 0

    for t in tasks:
        if not t.instruction_file.exists():
            die(f"task{t.id} 缺少 instruction.md: {t.instruction_file}")
    return driver(run_dir, tasks, args)


if __name__ == "__main__":
    raise SystemExit(main())
