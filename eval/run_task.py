# -*- coding: utf-8 -*-
"""评测驱动：构建镜像 → 运行（参考解 / 空基线）→ 判分 → 输出 reward。

两种 runner
-----------
* `--runner docker`（正式）：docker build 基础镜像，起容器、注入 solution 或直接判分。
* `--runner local`（冒烟）：不依赖 Docker —— 在临时目录里铺好 study.db 与 SOP，
  直接跑参考解里嵌的 Python 负载，再用 pytest（若可用）或内置 runner 执行判分器。
  用途是在没有 Docker 的开发机上验证"判分器能否区分正确解与错误解"。

用法
----
    python eval/run_task.py --mode oracle                 # 期望 reward=1
    python eval/run_task.py --mode base                    # 期望 reward=0
    python eval/run_task.py --mode oracle --runner docker  # 需要 Docker 引擎可用
"""
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TASK_DIR = os.path.join(REPO, "tasks", "repeat-dose-noael-judgment")
ENV_DIR = os.path.join(TASK_DIR, "environment")
TESTS_DIR = os.path.join(TASK_DIR, "tests")
SOLVE = os.path.join(TASK_DIR, "solution", "solve.sh")
IMAGE = "tox/noael-judgment:base"
CONTAINER = "tox-noael-run"


# ------------------------------------------------------------------ 工具
def extract_payload(solve_sh):
    """取出 solve.sh 中 heredoc 里的 Python 负载（便于无 bash 环境冒烟）。"""
    with open(solve_sh, encoding="utf-8") as f:
        text = f.read()
    start = text.index("<<'PY'") + len("<<'PY'")
    end = text.index("\nPY\n", start)
    return text[start:end]


def load_test_module(app_dir):
    os.environ["TASK_APP_DIR"] = app_dir
    spec = importlib.util.spec_from_file_location(
        "test_outputs", os.path.join(TESTS_DIR, "test_outputs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tests_builtin(app_dir):
    """无 pytest 依赖的逐用例执行，返回 [(用例名, 是否通过, 失败信息)]。"""
    module = load_test_module(app_dir)
    results = []
    for name in sorted(n for n in dir(module) if n.startswith("test_")):
        try:
            getattr(module, name)()
            results.append((name, True, ""))
        except Exception as exc:  # noqa: BLE001 - 判分器任何异常都算该用例失败
            results.append((name, False, f"{type(exc).__name__}: {exc}".splitlines()[0]))
    return results


def run_tests(app_dir):
    """返回 (ok, stdout)。优先 pytest，缺失时用内置 runner。"""
    os.environ["TASK_APP_DIR"] = app_dir
    env = dict(os.environ, TASK_APP_DIR=app_dir)

    if importlib.util.find_spec("pytest") is not None:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", os.path.join(TESTS_DIR, "test_outputs.py"),
             "-rA", "-p", "no:cacheprovider", "-q"],
            capture_output=True, text=True, env=env)
        return proc.returncode == 0, proc.stdout + proc.stderr

    results = run_tests_builtin(app_dir)
    lines = [f"{'PASS' if ok else 'FAIL'} {name}" + (f"\n     {msg}" if msg else "")
             for name, ok, msg in results]
    failed = sum(1 for _, ok, _ in results if not ok)
    lines.append(f"\n{len(results) - failed} passed, {failed} failed (built-in runner)")
    return failed == 0, "\n".join(lines)


def prepare_local_app():
    """在仓库内的临时工作目录铺好 study.db 与 SOP。

    注意：这里刻意不用 tempfile.mkdtemp —— 某些受限沙箱会拒绝写入 mkdtemp 创建的目录，
    用 uuid + os.makedirs 生成工作目录可避免该问题。
    """
    root = os.environ.get("TOX_WORK_DIR") or os.path.join(REPO, ".work")
    os.makedirs(root, exist_ok=True)
    app = os.path.join(root, "app-" + uuid.uuid4().hex[:8])
    os.makedirs(app, exist_ok=True)
    shutil.copy(os.path.join(ENV_DIR, "study.db"), app)
    shutil.copy(os.path.join(ENV_DIR, "SOP_toxicity_criteria.md"), app)
    return app


def local_mode(mode):
    app = prepare_local_app()
    try:
        if mode == "oracle":
            payload = extract_payload(SOLVE)
            script = os.path.join(app, "_solve.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(payload)
            env = dict(os.environ, APP_DIR=app)
            proc = subprocess.run([sys.executable, script], capture_output=True,
                                  text=True, env=env)
            if proc.returncode != 0:
                print(proc.stdout + proc.stderr)
                return 0, "参考解执行失败"
        return run_tests(app)
    finally:
        shutil.rmtree(app, ignore_errors=True)


def docker_mode(mode):
    def sh(*args, **kw):
        return subprocess.run(args, capture_output=True, text=True, **kw)

    sh("docker", "rm", "-f", CONTAINER)
    build = sh("docker", "build", "-t", IMAGE, ENV_DIR)
    if build.returncode != 0:
        print(build.stdout + build.stderr)
        return 0, "docker build 失败"
    run = sh("docker", "run", "-d", "--name", CONTAINER, IMAGE, "sleep", "1800")
    if run.returncode != 0:
        print(run.stdout + run.stderr)
        return 0, "docker run 失败"
    try:
        if mode == "oracle":
            sh("docker", "cp", SOLVE, f"{CONTAINER}:/tmp/solve.sh")
            solve = sh("docker", "exec", CONTAINER, "bash", "/tmp/solve.sh")
            if solve.returncode != 0:
                print(solve.stdout + solve.stderr)
                return 0, "参考解执行失败"
        sh("docker", "exec", CONTAINER, "mkdir", "-p", "/app/tests")
        sh("docker", "cp", os.path.join(TESTS_DIR, "."), f"{CONTAINER}:/app/tests")
        verify = sh("docker", "exec", CONTAINER, "bash", "/app/tests/test.sh")
        reward = sh("docker", "exec", CONTAINER, "cat", "/logs/verifier/reward.txt")
        print(verify.stdout + verify.stderr)
        return int(reward.stdout.strip() or 0), "docker 判分完成"
    finally:
        sh("docker", "rm", "-f", CONTAINER)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["oracle", "base"], default="oracle",
                    help="oracle=注入参考解；base=不注入（空基线，期望 reward=0）")
    ap.add_argument("--runner", choices=["local", "docker"], default="local")
    args = ap.parse_args()

    if args.runner == "docker":
        reward, note = docker_mode(args.mode)
    else:
        ok, output = local_mode(args.mode)
        reward = 1 if ok else 0
        note = f"local runner（{args.mode}）"
        print(output)

    print(f"\n[{note}] reward = {reward}")
    expected = 1 if args.mode == "oracle" else 0
    if reward != expected:
        print(f"!! 与预期不符：{args.mode} 期望 reward={expected}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
