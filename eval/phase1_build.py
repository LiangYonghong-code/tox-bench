# -*- coding: utf-8 -*-
"""Phase 1：把每道题打成可复现的基础镜像，回填镜像真实的 WORKDIR，并与 task.toml 对拍。

用法：
    python eval/phase1_build.py [--run-id ID] [--only 1,2] [--force] [--timeout 900]

产物：
    eval/runs/<run-id>/_build/build_report.json   每题构建结果 + WORKDIR 校验 + spec_check
    eval/runs/<run-id>/_build/taskN.build.log     单题完整构建日志
    eval/runs/<run-id>/_build/workdirs.json       镜像真实 WORKDIR（供 Phase 2/3 使用）

关于 spec_check
---------------
`task.toml`（提交元数据）与 `tasks.yaml`（驱动清单）各自声明了一部分相同的事实：
镜像名、workdir、参考解路径、用例数。两者由不同的人在不同的时候维护，是最容易悄悄
分叉的地方。这里把重叠字段全部对拍一遍 —— **只告警，不失败**：因为 task.toml 是给平台
看的，它的字段缺失不代表任务包无效；但分叉了必须让人看见。
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from common import (
    Task,
    die,
    docker,
    docker_output,
    docker_ready,
    image_exists,
    image_workdir,
    load_tasks,
    log,
    make_run_id,
    new_run_dir,
    parse_only,
    read_task_spec,
    task_fingerprint,
    write_json,
)

DEFAULT_BUILD_TIMEOUT = 900


def count_cases(task: Task) -> int | None:
    """数判分器里的 test_ 函数个数。"""
    f = task.tests_dir / (task.tests_entry or "test_outputs.py")
    if not f.exists():
        return None
    return len(re.findall(r"^def (test_\w+)", f.read_text(encoding="utf-8"), re.M))


def spec_check(task: Task, real_workdir: str | None) -> dict:
    """task.toml ↔ tasks.yaml ↔ 实际文件 三方对拍。返回 {字段: {...}}。"""
    spec = read_task_spec(task)
    env = spec.get("environment", {})
    tests = spec.get("tests", {})
    checks: dict = {}

    def cmp(field: str, declared, actual, note: str = "") -> None:
        checks[field] = {
            "declared": declared,
            "actual": actual,
            "match": declared == actual,
        }
        if note:
            checks[field]["note"] = note

    if not spec:
        checks["_spec_file"] = {
            "declared": str(task.spec_file), "actual": None, "match": False,
            "note": "task.toml 缺失或无法解析，跳过对拍",
        }
        return checks

    task_section = spec.get("task", {})
    cmp("task.name", task_section.get("name"), f"{task.image_prefix}/{task.slug}",
        "task.toml [task].name vs tasks.yaml 推导")
    cmp("environment.image", env.get("image"), task.base_image,
        "task.toml 声明 vs tasks.yaml 推导")
    cmp("environment.workdir", env.get("workdir"), task.workdir,
        "task.toml 声明 vs tasks.yaml 声明")
    if real_workdir is not None:
        cmp("environment.workdir_realdocker", env.get("workdir"), real_workdir,
            "task.toml 声明 vs 镜像真实 WORKDIR")
        cmp("tasks_yaml.workdir_realdocker", task.workdir, real_workdir,
            "tasks.yaml 声明 vs 镜像真实 WORKDIR")
    cmp("environment.build_context", env.get("build_context"), "environment")

    oracle_rel = str(task.solution_file.relative_to(task.dir)).replace("\\", "/")
    cmp("tests.oracle", tests.get("oracle"), oracle_rel,
        f"声明 vs 实际存在的参考解 {task.solution_file.name}")

    runner_rel = str(task.tests_dir.relative_to(task.dir)).replace("\\", "/") + "/test.sh"
    cmp("tests.runner", tests.get("runner"), runner_rel)

    cases = count_cases(task)
    if cases is not None:
        cmp("tests.case_count", tests.get("case_count"), cases, "声明 vs 判分器里实际的用例数")

    # 超时与资源：声明了才有效。缺了不报错（各阶段有 CLI 兜底），但记下来。
    for key, fallback in (("build_timeout_sec", DEFAULT_BUILD_TIMEOUT),
                          ("agent_timeout_sec", None),
                          ("verify_timeout_sec", None)):
        declared = spec.get("phases", {}).get(key)
        checks[f"phases.{key}"] = {
            "declared": declared, "actual": fallback if declared is None else declared,
            "match": declared is not None,
            "note": "未声明，该阶段将回退到 CLI 默认值" if declared is None else "",
        }

    res = spec.get("resources", {})
    checks["resources"] = {
        "declared": res or None, "actual": None, "match": bool(res),
        "note": "" if res else "未声明 [resources]，容器将不受 cpus/memory 限制",
    }
    return checks


def build_one(task: Task, run_dir: Path, force: bool, timeout: int) -> dict:
    build_dir = run_dir / "_build"
    build_dir.mkdir(parents=True, exist_ok=True)
    build_log = build_dir / f"task{task.id}.build.log"

    result: dict = {
        "task": task.id,
        "slug": task.slug,
        "image": task.base_image,
        "expected_workdir": task.workdir,
        "task_fingerprint": task_fingerprint(task),
    }

    if not task.environment_dir.is_dir():
        result |= {"ok": False, "stage": "context",
                   "error": f"缺少 environment 目录: {task.environment_dir}"}
        return result

    if image_exists(task.base_image) and not force:
        log(f"task{task.id}: 镜像已存在，跳过构建（--force 可强制重建）")
        result["build"] = "skipped"
    else:
        log(f"task{task.id} ({task.slug}): 开始构建 ...")
        t0 = time.time()
        p = docker("build", "-t", task.base_image, str(task.environment_dir),
                   timeout=timeout, log=build_log)
        result["build_seconds"] = round(time.time() - t0, 1)
        if p.returncode != 0:
            result |= {"ok": False, "stage": "build",
                       "error": (p.stderr or p.stdout or "")[-1500:]}
            log(f"task{task.id}: 构建失败（{result['build_seconds']}s），详见 {build_log.name}")
            return result
        result["build"] = "built"
        log(f"task{task.id}: 构建成功（{result['build_seconds']}s）")

    # 冒烟：镜像能否起容器
    smoke = docker("run", "--rm", "--entrypoint", "", task.base_image,
                   "sh", "-c", "echo SMOKE_OK", timeout=120, log=build_log)
    if smoke.returncode != 0 or "SMOKE_OK" not in smoke.stdout:
        result |= {"ok": False, "stage": "smoke",
                   "error": (smoke.stderr or smoke.stdout or "")[-1500:]}
        log(f"task{task.id}: 冒烟失败")
        return result

    # 读取镜像真实的 WORKDIR，和清单里的预期值对拍
    try:
        real_workdir = image_workdir(task.base_image)
    except Exception as exc:  # noqa: BLE001
        result |= {"ok": False, "stage": "inspect", "error": str(exc)}
        return result

    result["real_workdir"] = real_workdir
    result["workdir_match"] = (real_workdir == task.workdir)
    if not result["workdir_match"]:
        log(f"task{task.id}: [警告] WORKDIR 不一致，清单={task.workdir!r} 镜像={real_workdir!r}"
            f"（下游阶段将使用镜像里的真实值）")

    try:
        size = docker_output("image", "inspect", "--format", "{{.Size}}", task.base_image)
        result["image_size_bytes"] = int(size)
    except Exception:  # noqa: BLE001
        pass

    # task.toml 对拍（只告警）
    checks = spec_check(task, real_workdir)
    result["spec_check"] = checks
    mismatched = [k for k, v in checks.items() if not v.get("match")]
    if mismatched:
        log(f"task{task.id}: [警告] task.toml 对拍有 {len(mismatched)} 处不一致："
            + ", ".join(mismatched))

    result["ok"] = True
    log(f"task{task.id}: 就绪 workdir={real_workdir} "
        f"size={result.get('image_size_bytes', 0) / 1e6:.0f}MB "
        f"fingerprint={result['task_fingerprint']}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--only", default=None, help="逗号分隔的 task id，例如 1,2")
    ap.add_argument("--force", action="store_true", help="强制重建已有镜像")
    ap.add_argument("--timeout", type=int, default=DEFAULT_BUILD_TIMEOUT,
                    help="单题构建超时（秒）")
    args = ap.parse_args()

    ok, info = docker_ready()
    if not ok:
        die(f"Docker 引擎未就绪：{info}\n请先启动 Docker Desktop 并等它完成初始化。")
    log(f"Docker 引擎就绪，server version = {info}")

    _, tasks = load_tasks(only=parse_only(args.only))
    run_id = args.run_id or make_run_id()
    run_dir = new_run_dir(run_id)
    log(f"run id = {run_id} -> {run_dir}")

    results = []
    for task in tasks:
        try:
            results.append(build_one(task, run_dir, args.force, args.timeout))
        except Exception as exc:  # noqa: BLE001
            results.append({"task": task.id, "slug": task.slug, "image": task.base_image,
                            "ok": False, "stage": "exception", "error": repr(exc)})
            log(f"task{task.id}: 异常 {exc!r}")

    build_dir = run_dir / "_build"
    write_json(build_dir / "build_report.json", results)

    # 只回填成功的题，供 Phase 2/3 使用
    workdirs = {str(r["task"]): r["real_workdir"] for r in results if r.get("ok")}
    write_json(build_dir / "workdirs.json", workdirs)

    good = [r for r in results if r.get("ok")]
    log("=" * 60)
    log(f"构建完成：{len(good)}/{len(results)} 就绪")
    for r in results:
        flag = "OK  " if r.get("ok") else "FAIL"
        log(f"  [{flag}] task{r['task']:<3} {r['slug']:<38} {r.get('real_workdir', '-')}")
    log(f"报告: {build_dir / 'build_report.json'}")
    return 0 if len(good) == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
