# -*- coding: utf-8 -*-
"""Phase 3：从快照镜像起一个干净容器，注入 tests，跑 test.sh，收 reward 与交付物。

用法：
    # 正常评测：用 Phase 2 commit 出来的快照镜像
    python eval/phase3_verify.py --run-id main

    # 防伪阳性对照：故意用未跑 agent 的 base 镜像，期望 reward=0
    python eval/phase3_verify.py --run-id main --only 1 --base

    # 参考解对照：phase3 自己起容器、跑 solve.sh、commit，期望 reward=1
    python eval/phase3_verify.py --run-id main --only 1 --oracle

    # 无 Docker 冒烟（本机跑判分器，验证判分器本身）
    python eval/phase3_verify.py --run-id smoke --only 1 --local --oracle   # 期望 1
    python eval/phase3_verify.py --run-id smoke --only 1 --local            # 期望 0

    # 直接指定镜像（调试）
    python eval/phase3_verify.py --run-id main --only 1 --image <tag>

Docker 流程严格对应平台行为：
    1. docker run -d       起容器（容器里此刻**没有** tests）
    2. docker cp           把 tests 注入 /tests（agent 阶段早已结束，看不到断言）
    3. docker exec bash /tests/test.sh
    4. 读 /logs/verifier/reward.txt   ← 唯一判定：1=全绿，0=有失败
    5. 收 ctrf.json 明细与交付物，销毁容器

产物（默认写 taskN/；加了对照开关则写 taskN_base/ 等）：
    reward.txt / ctrf.json / verify.log / verify_result.json / <交付物>
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from common import (
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
    read_task_spec,
    resolve_workdir,
    run_oracle_local,
    spec_timeout,
    task_out,
    write_json,
)

DEFAULT_EXEC_TIMEOUT = 900
STARTUP_TIMEOUT = 180


def deliverables(task: Task) -> list[str]:
    """从 task.toml 的 [task].deliverables 读交付物文件名（相对 workdir）。"""
    d = read_task_spec(task).get("task", {}).get("deliverables") or []
    return [str(x) for x in d]


def resolve_image(task: Task, run_dir: Path, use_base: bool, run_id: str,
                  override: str | None = None) -> str | None:
    if override:
        return override
    if use_base:
        return task.base_image
    rf = run_dir / f"task{task.id}" / "result.json"
    if not rf.exists():
        return None
    data = read_json(rf)
    if not data.get("snapshot_ok"):
        return None
    return data.get("snapshot") or task.solved_image(run_id)


def out_dir_for(run_dir: Path, task: Task, suffix: str) -> Path:
    if not suffix:
        return task_out(run_dir, task)
    d = run_dir / f"task{task.id}{suffix}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# 参考解镜像（--oracle）
# --------------------------------------------------------------------------- #

def make_oracle_image(task: Task, run_id: str, logfile: Path) -> tuple[str | None, str]:
    """起 base 容器 → 跑 solve.sh → commit 成 oracle 镜像 → 销毁容器。

    容器全程由本函数自己起、自己 commit、自己销毁，所以不违反"快照归 Phase 2"
    那条约束 —— 这里不是 agent 的成果，而是判分器的阳性对照。
    """
    tag = task.oracle_image(run_id)
    name = f"tox-oracle-task{task.id}-{int(time.time()) % 100000}"
    docker("rm", "-f", name)

    p = docker("run", "-d", "--name", name, task.base_image, "sleep", "900",
               timeout=STARTUP_TIMEOUT, log=logfile)
    if p.returncode != 0:
        return None, f"起容器失败: {(p.stderr or '')[-500:]}"
    try:
        cp = docker("cp", task.solution_file.as_posix(), f"{name}:/tmp/solve.sh", log=logfile)
        if cp.returncode != 0:
            return None, f"拷贝参考解失败: {(cp.stderr or '')[-500:]}"
        ex = docker("exec", name, "bash", "/tmp/solve.sh",
                    timeout=spec_timeout(task, "verify", DEFAULT_EXEC_TIMEOUT), log=logfile)
        if ex.returncode != 0:
            return None, f"参考解执行失败(exit={ex.returncode}): {(ex.stderr or '')[-800:]}"
        cm = docker("commit", name, tag, timeout=600, log=logfile)
        if cm.returncode != 0:
            return None, f"commit 失败: {(cm.stderr or '')[-500:]}"
    finally:
        docker("rm", "-f", name, timeout=180)
    return tag, ""


# --------------------------------------------------------------------------- #
# Docker 判分
# --------------------------------------------------------------------------- #

def verify_one(task: Task, run_dir: Path, image: str, mode: str, out: Path) -> dict:
    name = f"tox-verify-task{task.id}-{int(time.time()) % 100000}"
    logfile = out / "verify.log"
    workdir = resolve_workdir(task, run_dir)

    result: dict = {
        "task": task.id,
        "slug": task.slug,
        "image": image,
        "mode": mode,
        "workdir": workdir,
    }

    # 0) 清理可能残留的同名容器
    docker("rm", "-f", name)

    # 1) 起容器（此刻没有 tests）
    p = docker("run", "-d", "--name", name, image, "sleep", "900",
               timeout=STARTUP_TIMEOUT, log=logfile)
    if p.returncode != 0:
        result |= {"ok": False, "stage": "run", "error": (p.stderr or "")[-800:]}
        write_json(out / "verify_result.json", result)
        return result
    result["container"] = name

    try:
        # 2) 注入 tests（事后注入，模拟平台行为）
        docker("exec", name, "mkdir", "-p", "/tests", log=logfile)
        # 用 "源目录/." 的形式复制内容，语义明确：把 tests/ 的内容铺进 /tests。
        # as_posix() 避免 Windows 反斜杠与 "/." 混用。
        cp = docker("cp", task.tests_dir.as_posix() + "/.", f"{name}:/tests", log=logfile)
        if cp.returncode != 0:
            result |= {"ok": False, "stage": "cp", "error": (cp.stderr or "")[-800:]}
            return result

        ls = docker("exec", name, "ls", "-la", "/tests", log=logfile)
        result["tests_in_container"] = ls.stdout

        # 3) 跑 test.sh（-w 用镜像真实 workdir，保证交付物相对路径语义正确）
        t0 = time.time()
        ex = docker("exec", "-w", workdir, name, "bash", "/tests/test.sh",
                    timeout=spec_timeout(task, "verify", DEFAULT_EXEC_TIMEOUT), log=logfile)
        result["verify_seconds"] = round(time.time() - t0, 1)
        result["test_sh_exit"] = ex.returncode

        # 4) 取 reward —— 唯一判定依据。
        #    注意 test.sh 永远 exit 0，所以 test_sh_exit 不能当通过信号。
        rw = docker("exec", name, "cat", "/logs/verifier/reward.txt", log=logfile)
        reward_txt = (rw.stdout or "").strip()
        result["reward_raw"] = reward_txt
        if reward_txt in ("0", "1"):
            result["reward"] = int(reward_txt)
            result["passed"] = (reward_txt == "1")
        else:
            result |= {"ok": False, "stage": "reward",
                       "error": f"reward.txt 内容异常: {reward_txt!r}"}

        (out / "reward.txt").write_text(reward_txt + "\n", encoding="utf-8")

        # 5) 收 ctrf 明细
        ctrf = docker("exec", name, "cat", "/logs/verifier/ctrf.json")
        if ctrf.returncode == 0 and (ctrf.stdout or "").strip():
            (out / "ctrf.json").write_text(ctrf.stdout, encoding="utf-8")
            try:
                summary = read_json(out / "ctrf.json")["results"]["summary"]
                result["ctrf"] = {k: summary.get(k)
                                  for k in ("tests", "passed", "failed", "skipped")}
            except Exception:  # noqa: BLE001
                pass

        # 6) 收交付物（快照容器里就是 agent 交上来的样子）
        collected = []
        for rel in deliverables(task):
            src = f"{name}:{workdir.rstrip('/')}/{rel}"
            dst = out / Path(rel).name
            if docker("cp", src, str(dst)).returncode == 0:
                collected.append(dst.name)
        result["deliverables"] = collected

        result.setdefault("ok", True)
    finally:
        docker("rm", "-f", name, timeout=180)

    write_json(out / "verify_result.json", result)
    return result


# --------------------------------------------------------------------------- #
# 本机判分（无 Docker）
# --------------------------------------------------------------------------- #

def verify_local(task: Task, run_dir: Path, use_oracle: bool, out: Path) -> dict:
    app = prepare_local_app(task)
    logfile = out / "verify.log"
    result: dict = {
        "task": task.id,
        "slug": task.slug,
        "image": None,
        "mode": "local_oracle" if use_oracle else "local_base",
        "workdir": str(app),
        "runner": "local",
    }
    lines: list[str] = []
    try:
        if use_oracle:
            ok, output = run_oracle_local(task, app)
            lines.append(f"$ python _solve.py  (exit_ok={ok})\n{output}")
            if not ok:
                # 参考解自己跑不起来 = 任务包本身有问题，不是"模型得 0 分"，必须区分开
                result |= {"ok": False, "stage": "oracle",
                           "error": "参考解执行失败（任务包问题，非模型问题）"}
                logfile.write_text("\n".join(lines), encoding="utf-8")
                write_json(out / "verify_result.json", result)
                return result

        reward, output, ctrf_data = judge_local(task, app)
        lines.append(output)
        result["reward"] = reward
        result["passed"] = (reward == 1)
        result["reward_raw"] = str(reward)
        result["ok"] = True
        (out / "reward.txt").write_text(f"{reward}\n", encoding="utf-8")

        if ctrf_data:
            write_json(out / "ctrf.json", ctrf_data)
            try:
                summary = ctrf_data["results"]["summary"]
                result["ctrf"] = {k: summary.get(k)
                                  for k in ("tests", "passed", "failed", "skipped")}
            except Exception:  # noqa: BLE001
                pass

        collected = []
        for rel in deliverables(task):
            src = app / Path(rel).name
            if src.exists():
                shutil.copy(src, out / Path(rel).name)
                collected.append(Path(rel).name)
        result["deliverables"] = collected
    finally:
        logfile.write_text("\n".join(lines), encoding="utf-8")
        shutil.rmtree(app, ignore_errors=True)

    write_json(out / "verify_result.json", result)
    return result


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--only", default=None, help="逗号分隔的 task id")
    ap.add_argument("--base", action="store_true",
                    help="用 base 镜像做防伪阳性对照（期望 reward=0）")
    ap.add_argument("--oracle", action="store_true",
                    help="起容器跑参考解再验证（期望 reward=1）")
    ap.add_argument("--local", action="store_true",
                    help="不走 Docker：在本机临时目录里直接调判分器")
    ap.add_argument("--image", default=None,
                    help="直接指定镜像 tag（调试用），需配合 --only 单题")
    ap.add_argument("--out-suffix", default=None,
                    help="输出子目录后缀；默认按模式推导（_base / _oracle / _local）")
    args = ap.parse_args()

    _, tasks = load_tasks(only=parse_only(args.only))
    if not tasks:
        die("没有选中任何题目，检查 --only")

    if args.image and len(tasks) != 1:
        die("--image 只能配合 --only 指定单题使用（不同题的镜像不同）")
    if args.oracle and len(tasks) != 1:
        die("--oracle 只能配合 --only 指定单题使用")
    if args.base and args.oracle:
        die("--base 与 --oracle 互斥")
    if args.local and args.image:
        die("--local 与 --image 互斥")

    args.run_id = args.run_id or make_run_id()
    run_dir = new_run_dir(args.run_id)

    if not args.local:
        ok, info = docker_ready()
        if not ok:
            die(f"Docker 引擎未就绪：{info}\n（只想验证判分器可用 --local，不需要 Docker）")

    # 输出后缀：默认按模式推导，保证不同对照互不覆盖
    if args.out_suffix is not None:
        out_suffix = args.out_suffix
    elif args.local:
        out_suffix = "_local_oracle" if args.oracle else "_local"
    elif args.base:
        out_suffix = "_base"
    elif args.oracle:
        out_suffix = "_oracle"
    else:
        out_suffix = ""

    results = []
    for task in tasks:
        out = out_dir_for(run_dir, task, out_suffix)
        logfile = out / "verify.log"
        try:
            if args.local:
                log(f"task{task.id}: 本机验证（{'oracle' if args.oracle else 'base'}）...")
                r = verify_local(task, run_dir, args.oracle, out)
            else:
                if args.oracle:
                    log(f"task{task.id}: 构造参考解镜像 ...")
                    image, err = make_oracle_image(task, args.run_id, logfile)
                    if image is None:
                        r = {"task": task.id, "slug": task.slug, "ok": False,
                             "stage": "oracle", "error": err}
                        results.append(r)
                        log(f"task{task.id}: [ERR ] {err}")
                        continue
                    mode = "oracle"
                else:
                    image = resolve_image(task, run_dir, args.base, args.run_id, args.image)
                    mode = "base" if args.base else "solved"
                if image is None:
                    log(f"task{task.id}: 没有可用快照（Phase 2 未成功或未跑），跳过")
                    r = {"task": task.id, "slug": task.slug, "ok": False,
                         "stage": "resolve_image", "error": "无快照镜像"}
                    results.append(r)
                    continue
                log(f"task{task.id}: 验证 {image} ...")
                r = verify_one(task, run_dir, image, mode, out)
        except Exception as exc:  # noqa: BLE001
            r = {"task": task.id, "slug": task.slug, "ok": False,
                 "stage": "exception", "error": repr(exc)}
        results.append(r)
        tag = "PASS" if r.get("passed") else ("ERR " if not r.get("ok") else "FAIL")
        log(f"task{task.id}: [{tag}] reward={r.get('reward')} "
            f"ctrf={r.get('ctrf')} {r.get('error', '') or ''}")

    write_json(run_dir / f"_verify{out_suffix}_summary.json", results)

    done = [r for r in results if r.get("ok")]
    passed = [r for r in results if r.get("passed")]
    log("=" * 60)
    log(f"验证完成：{len(passed)}/{len(done)} 通过（共 {len(results)} 题）")
    for r in results:
        log(f"  task{r['task']:<3} {r['slug']:<38} reward={r.get('reward')} {r.get('ctrf', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
