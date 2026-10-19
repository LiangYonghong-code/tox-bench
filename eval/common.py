# -*- coding: utf-8 -*-
"""Phase 1–4 共享的工具函数：清单加载、docker 调用封装、每题输出目录管理、
以及"不需要 Docker 的本机判分原语"。

设计约定
--------
- 所有 docker 调用都用 subprocess 的参数列表形式（不经 shell），避免 Windows/MSYS
  把 `C:\\...` 或 `/app` 改写成别的路径。
- 所有输出编码统一 utf-8 + errors="replace"，因为容器里 pytest 的输出可能含非 UTF-8 字节。
- 任何一题失败都不中断整批，由调用方决定是记录后继续还是抛出。
- 题名/镜像名/容器名**一律从 tasks.yaml 推导**，下游脚本不得硬编码。

本机判分原语（`prepare_local_app` 及以下）
----------------------------------------
这组函数服务三个消费者：`phase3_verify.py --local`、各题的
`build-tools/<题目录名>/check_discrimination.py`、`phase2_agent.py --local`。
它们让"判分器能不能区分正确解与错误解"这件事在没有 Docker、没有 pytest 的开发机上也能验证。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

REPO = Path(__file__).resolve().parent.parent
EVAL = REPO / "eval"
RUNS = EVAL / "runs"

# 题目指纹的**默认**输入（相对题目录）。
# 定义已冻结：task1 的历史分数以这组文件为准，改动它会让那些分数静默失去可比性。
# 后续任务在 tasks.yaml 里用自己的 fingerprint_files 声明（见该文件注释），
# 不要为了"看起来统一"去改这个默认值。
FINGERPRINT_FILES = ("instruction.md", "environment/SOP_toxicity_criteria.md", "environment/study.db")


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Task:
    id: int
    slug: str
    dir: Path
    workdir: str
    lang: str
    tests_entry: str
    runner: str
    image_prefix: str = "tox"
    base_tag: str = "base"
    solved_tag_prefix: str = "solved"
    fingerprint_files: tuple[str, ...] = FINGERPRINT_FILES

    @property
    def base_image(self) -> str:
        return f"{self.image_prefix}/{self.slug}:{self.base_tag}"

    def solved_image(self, run_id: str) -> str:
        return f"{self.image_prefix}/{self.slug}:{self.solved_tag_prefix}-{run_id}"

    def oracle_image(self, run_id: str) -> str:
        return f"{self.image_prefix}/{self.slug}:oracle-{run_id}"

    @property
    def tests_dir(self) -> Path:
        return self.dir / "tests"

    @property
    def instruction_file(self) -> Path:
        return self.dir / "instruction.md"

    @property
    def environment_dir(self) -> Path:
        return self.dir / "environment"

    @property
    def solution_file(self) -> Path:
        return self.dir / "solution" / "solve.sh"

    @property
    def spec_file(self) -> Path:
        return self.dir / "task.toml"


def load_tasks(only: Sequence[int] | None = None) -> tuple[dict[str, Any], list[Task]]:
    """读取 eval/tasks.yaml。only 非空时只返回这些 task id。"""
    data = yaml.safe_load((EVAL / "tasks.yaml").read_text(encoding="utf-8"))
    h = data.get("harness", {})
    tasks = [
        Task(
            id=int(t["id"]),
            slug=t["slug"],
            dir=REPO / t["dir"],
            workdir=t["workdir"],
            lang=t.get("lang", ""),
            tests_entry=t.get("tests_entry", ""),
            runner=t.get("runner", ""),
            image_prefix=h.get("image_prefix", "tox"),
            base_tag=h.get("base_tag", "base"),
            solved_tag_prefix=h.get("solved_tag_prefix", "solved"),
            fingerprint_files=tuple(
                t.get("fingerprint_files")
                or die(
                    f"task {t['id']} ({t.get('slug')}) 必须在 tasks.yaml 中显式声明 "
                    f"fingerprint_files，避免默认指纹误用导致历史分数失去可比性"
                )
            ),
        )
        for t in data["tasks"]
    ]
    if only:
        wanted = set(only)
        tasks = [t for t in tasks if t.id in wanted]
    return data, tasks


def read_task_spec(task: Task) -> dict:
    """读 task.toml（提交元数据）。缺失或解析失败返回 {}。"""
    if not task.spec_file.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:                     # py<3.11
        try:
            import tomli as tomllib                  # type: ignore
        except ModuleNotFoundError:
            return {}
    try:
        with open(task.spec_file, "rb") as f:
            return tomllib.load(f)
    except Exception:                               # noqa: BLE001
        return {}


def spec_timeout(task: Task, phase: str, fallback: int) -> int:
    """从 task.toml 的 [phases] 取超时，让 task.toml 成为超时的单一来源。"""
    spec = read_task_spec(task)
    value = spec.get("phases", {}).get(f"{phase}_timeout_sec")
    return int(value) if isinstance(value, (int, float)) and value > 0 else fallback


def spec_run_args(task: Task) -> list[str]:
    """把 task.toml 的 [resources] 翻成 docker run 的资源限制参数。

    这些字段此前只是"声明了但没有任何地方强制执行"，这里补上。
    """
    res = read_task_spec(task).get("resources", {})
    args: list[str] = []
    cpus = res.get("cpus")
    mem = res.get("memory_mb")
    if isinstance(cpus, (int, float)) and cpus > 0:
        args += ["--cpus", str(cpus)]
    if isinstance(mem, (int, float)) and mem > 0:
        args += ["--memory", f"{int(mem)}m"]
    return args


def resolve_workdir(task: Task, run_dir: Path) -> str:
    """优先用 Phase 1 从镜像里读到的真实 WORKDIR，回退到清单里的预期值。"""
    f = run_dir / "_build" / "workdirs.json"
    if f.exists():
        try:
            wd = read_json(f).get(str(task.id))
            if wd:
                return wd
        except Exception:  # noqa: BLE001
            pass
    return task.workdir


def task_fingerprint(task: Task) -> str:
    """题目指纹：题面 + 判定标准 + 数据。用来判断两次 run 的分数是否可比。

    输入文件由 `task.fingerprint_files` 决定（tasks.yaml 可选题级覆盖，缺省见
    FINGERPRINT_FILES）。**每组输入的取值一旦发布就冻结**（sha256 前 16 位十六进制）：
    改文件内容、或改这一题的 fingerprint_files 列表，都会静默地让该题的历史分数
    失去可比性——Phase 4 的跨 run 比对会因此报警，而不是悄悄混着算。
    """
    h = hashlib.sha256()
    for rel in task.fingerprint_files:
        h.update((task.dir / rel).read_bytes())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 输出目录
# --------------------------------------------------------------------------- #

def new_run_dir(run_id: str) -> Path:
    d = RUNS / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def task_out(run_dir: Path, task: Task) -> Path:
    d = run_dir / f"task{task.id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_run_id() -> str:
    return time.strftime("%Y-%m-%d_%H%M%S")


# --------------------------------------------------------------------------- #
# docker
# --------------------------------------------------------------------------- #

def run_cmd(
    cmd: Sequence[str],
    *,
    timeout: int | None = None,
    check: bool = False,
    log: Path | None = None,
) -> subprocess.CompletedProcess:
    """执行外部命令，返回 CompletedProcess（不抛 CalledProcessError，除非 check=True）。

    传入 log 时把完整的 stdout/stderr 追加写入该文件，便于事后审计。
    """
    printable = " ".join(str(c) for c in cmd)
    proc = subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )
    if log is not None:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n$ {printable}\n")
            f.write(f"[exit={proc.returncode}]\n")
            if proc.stdout:
                f.write(proc.stdout)
            if proc.stderr:
                f.write("\n--- stderr ---\n")
                f.write(proc.stderr)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"命令失败 (exit={proc.returncode}): {printable}\n"
            f"{(proc.stderr or proc.stdout or '')[:2000]}"
        )
    return proc


def docker(*args: str, timeout: int | None = None, check: bool = False,
           log: Path | None = None) -> subprocess.CompletedProcess:
    return run_cmd(["docker", *args], timeout=timeout, check=check, log=log)


def docker_output(*args: str, timeout: int | None = None) -> str:
    return docker(*args, timeout=timeout, check=True).stdout.strip()


def docker_ready() -> tuple[bool, str]:
    """检查 Docker 引擎是否已就绪。"""
    try:
        p = docker("version", "--format", "{{.Server.Version}}", timeout=30)
    except FileNotFoundError:
        return False, "找不到 docker 可执行文件（Docker Desktop 未安装或不在 PATH）"
    except subprocess.TimeoutExpired:
        return False, "docker version 超时，引擎可能在启动中"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "").strip()
    return True, p.stdout.strip()


def image_exists(image: str) -> bool:
    p = docker("image", "inspect", image)
    return p.returncode == 0


def image_workdir(image: str) -> str:
    """读取镜像真实的 WORKDIR，用于校验 tasks.yaml 里写的预期值。"""
    return docker_output("inspect", "--format", "{{.Config.WorkingDir}}", image)


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #

def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[FATAL] {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def parse_only(value: str | None) -> list[int] | None:
    return [int(x) for x in value.split(",")] if value else None


# --------------------------------------------------------------------------- #
# 本机判分原语（无 Docker / 无 pytest 也能跑）
# --------------------------------------------------------------------------- #

def extract_payload(solve_sh: Path, marker: str | None = None) -> str:
    """取出 solve.sh 中 heredoc 里的 Python 负载（便于无 bash 环境冒烟）。

    若未指定 marker，自动检测第一个非注释行里的单引号 heredoc 标记，
    避免 solve.sh 里 heredoc 标记写错或被注释干扰。
    """
    text = Path(solve_sh).read_text(encoding="utf-8")
    if marker is None:
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            m = re.search(r"<<'([A-Za-z0-9_]+)'", line)
            if m:
                marker = m.group(1)
                break
        if marker is None:
            raise ValueError(f"在 {solve_sh} 中未找到 heredoc 标记")
    start = text.index(f"<<'{marker}'") + len(f"<<'{marker}'")
    end = text.index(f"\n{marker}\n", start)
    return text[start:end]


def prepare_local_app(task: Task) -> Path:
    """在仓库内的临时工作目录铺好题目环境里的数据文件。

    铺的是 environment/ 下**除 Dockerfile 外**的全部文件 —— 即基础镜像 COPY 进去的那些。

    注意：这里刻意不用 tempfile.mkdtemp —— 某些受限沙箱会拒绝写入 mkdtemp 创建的目录，
    用 uuid + os.makedirs 生成工作目录可避免该问题。
    """
    root = os.environ.get("TOX_WORK_DIR") or os.path.join(REPO, ".work")
    os.makedirs(root, exist_ok=True)
    app = os.path.join(root, "app-" + uuid.uuid4().hex[:8])
    os.makedirs(app, exist_ok=True)
    for src in sorted(task.environment_dir.iterdir()):
        if src.is_file() and src.name != "Dockerfile":
            shutil.copy(src, app)
    return Path(app)


def load_test_module(task: Task, app_dir):
    """导入判分器模块。必须在 exec_module **之前**设 TASK_APP_DIR ——
    判分器在模块级就把它读进 APP 常量了。"""
    os.environ["TASK_APP_DIR"] = str(app_dir)
    spec = importlib.util.spec_from_file_location(
        "test_outputs", str(task.tests_dir / "test_outputs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tests_builtin(task: Task, app_dir) -> list[tuple[str, bool, str]]:
    """无 pytest 依赖的逐用例执行，返回 [(用例名, 是否通过, 失败信息)]。"""
    module = load_test_module(task, app_dir)
    results = []
    for name in sorted(n for n in dir(module) if n.startswith("test_")):
        try:
            getattr(module, name)()
            results.append((name, True, ""))
        except Exception as exc:  # noqa: BLE001 - 判分器任何异常都算该用例失败
            results.append((name, False, f"{type(exc).__name__}: {exc}".splitlines()[0]))
    return results


def _ctrf_from_builtin(results: list[tuple[str, bool, str]]) -> dict:
    """把内置 runner 的结果转成 pytest-json-ctrf 风格的结构。"""
    tests = []
    for name, ok, msg in results:
        entry: dict = {"name": name, "status": "passed" if ok else "failed"}
        if msg:
            entry["message"] = msg
        tests.append(entry)
    failed = sum(1 for _, ok, _ in results if not ok)
    return {
        "results": {
            "summary": {
                "tests": len(results),
                "passed": len(results) - failed,
                "failed": failed,
                "skipped": 0,
                "pending": 0,
                "other": 0,
            },
            "tests": tests,
        }
    }


def _pytest_has_ctrf() -> bool:
    """检查 pytest-json-ctrf 插件是否可用。"""
    try:
        import pytest_json_ctrf  # noqa: F401
        return True
    except ImportError:
        return False


def run_tests(task: Task, app_dir) -> tuple[bool, str, dict]:
    """返回 (ok, stdout, ctrf_dict)。优先 pytest，缺失时用内置 runner。

    为了与 Docker 分支的 Phase 4 行为一致，本地判分也要产出 ctrf 结构。
    若 pytest 存在但 pytest-json-ctrf 插件不存在，则回退到内置 runner。
    """
    env = dict(os.environ, TASK_APP_DIR=str(app_dir))

    if importlib.util.find_spec("pytest") is not None and _pytest_has_ctrf():
        ctrf_path = Path(app_dir) / ".ctrf.json"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(task.tests_dir / "test_outputs.py"),
             "-rA", "-p", "no:cacheprovider", "-q", "--ctrf", str(ctrf_path)],
            capture_output=True, text=True, env=env, encoding="utf-8", errors="replace")
        ctrf_data: dict = {}
        if ctrf_path.exists():
            try:
                ctrf_data = json.loads(ctrf_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
            ctrf_path.unlink(missing_ok=True)
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or ""), ctrf_data

    results = run_tests_builtin(task, app_dir)
    lines = [f"{'PASS' if ok else 'FAIL'} {name}" + (f"\n     {msg}" if msg else "")
             for name, ok, msg in results]
    failed = sum(1 for _, ok, _ in results if not ok)
    lines.append(f"\n{len(results) - failed} passed, {failed} failed (built-in runner)")
    return failed == 0, "\n".join(lines), _ctrf_from_builtin(results)


def run_oracle_local(task: Task, app_dir) -> tuple[bool, str]:
    """在本机 app 目录里执行参考解。返回 (是否成功, 输出)。"""
    payload = extract_payload(task.solution_file)
    script = Path(app_dir) / "_solve.py"
    script.write_text(payload, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          env=dict(os.environ, APP_DIR=str(app_dir)),
                          encoding="utf-8", errors="replace")
    script.unlink(missing_ok=True)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def judge_local(task: Task, app_dir) -> tuple[int, str, dict]:
    """本机判分：跑判分器，返回 (reward, 输出, ctrf_dict)。"""
    ok, output, ctrf = run_tests(task, app_dir)
    return (1 if ok else 0), output, ctrf
