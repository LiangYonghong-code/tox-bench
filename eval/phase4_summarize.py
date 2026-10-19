# -*- coding: utf-8 -*-
"""Phase 4：把 Phase 1/2/3 的产物汇总成一份可读报告。

用法：
    python eval/phase4_summarize.py --run-id main

产物：
    eval/runs/<run-id>/summary.json   机器可读（{meta, rows}）
    eval/runs/<run-id>/summary.md     人读（一题一行 + 失败明细）

关于"跨 run 可比性"
------------------
每道题有一个 `task_fingerprint`（题面 + 判定标准 + 数据的 sha256 前 16 位）。
参与哈希的文件由 `task.fingerprint_files` 决定：缺省是 task1 那组（题面 + SOP + 数据），
新题在 `eval/tasks.yaml` 里按自己的环境文件声明。两次 run 之间，
只要某题的指纹变了，该题的分数就**不可直接比较** —— 题目本身变了，不是模型变了。
本阶段会自动扫描 `eval/runs/*/summary.json`，取最近一次有指纹的 run 作基线，
指纹有出入就在报告顶部插一段告警。

关于 reward=None
----------------
`reward` 为 None 表示**基础设施失败**（没有快照、reward.txt 内容异常、参考解跑不起来），
不是"模型得 0 分"。两者混为一谈会让 benchmark 的评分效度失真，所以这里分列两节。
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from common import (
    RUNS,
    load_tasks,
    log,
    new_run_dir,
    read_json,
    write_json,
)

# 只有这一份是"模型的成绩"：Phase 3 对 Phase 2 快照的判分。
# 其余几份（_verify_oracle_/_verify_base_/_verify_local_）都是**对照**，
# 绝不能当成绩读 —— 否则只跑过 oracle 对照的 run 会被汇总成"模型 100% 通过"。
VERIFY_SECTION = "_verify_summary.json"

# 对照证据：只用于在 meta 里报告"地基验证过了没有"，不参与评分
CONTROL_SECTIONS = {
    "oracle_docker": "_verify_oracle_summary.json",
    "base_docker": "_verify_base_summary.json",
    "oracle_local": "_verify_local_oracle_summary.json",
    "base_local": "_verify_local_summary.json",
}


def load_section(run_dir: Path, name: str) -> dict:
    f = run_dir / name
    if not f.exists():
        return {}
    try:
        data = read_json(f)
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict):                 # 容忍 {meta, rows} 形态
        data = data.get("rows", [])
    return {str(r["task"]): r for r in data if isinstance(r, dict) and "task" in r}


def load_agent_section(run_dir: Path) -> dict:
    """Phase 2 的产物。

    批量跑时由 driver() 汇总成 `_agent_summary.json`；但用户也可能只跑了
    `phase2_agent.py --single N`，那时没有这个文件 —— 直接回退去读每题的
    `taskN/result.json`，否则汇总表里的步数/成本/改动文件会全是空的。
    注意用严格模式 `task<数字>` 匹配，避免把 taskN_base/taskN_oracle 也算进来。
    """
    sec = load_section(run_dir, "_agent_summary.json")
    if sec:
        return sec
    for d in sorted(run_dir.iterdir()):
        if not d.is_dir() or not re.fullmatch(r"task\d+", d.name):
            continue
        f = d / "result.json"
        if f.exists():
            try:
                sec[d.name[len("task"):]] = read_json(f)
            except Exception:  # noqa: BLE001
                pass
    return sec


def pick_verify(run_dir: Path) -> tuple[dict, str]:
    sec = load_section(run_dir, VERIFY_SECTION)
    return (sec, VERIFY_SECTION) if sec else ({}, "")


def controls_evidence(run_dir: Path) -> dict:
    """记录本 run 里有哪些对照证据（oracle 期望 1 / base 期望 0）。

    这是"地基验证过了没有"的存档；缺了不影响汇总，但会在报告里标出来。
    """
    ev: dict = {}
    for key, name in CONTROL_SECTIONS.items():
        sec = load_section(run_dir, name)
        if not sec:
            continue
        got = [r.get("reward") for r in sec.values()]
        want = 1 if key.startswith("oracle") else 0
        ev[key] = {
            "rewards": got,
            "expected": want,
            "ok": bool(got) and all(g == want for g in got),
        }
    return ev


def failed_from_log(text: str) -> list[str]:
    """本机判分（无 ctrf）时从 verify.log 里抠失败用例名。

    兼容两种输出：内置 runner 的 `FAIL test_x`，以及 pytest -rA 的
    `FAILED path::test_x - ...`。
    """
    names: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*FAIL(?:ED)?\s+(?:\S+::)?(\w+)", line)
        if m and m.group(1).startswith("test_"):
            names.append(m.group(1))
    return names


def failed_from_ctrf(ctrf_file: Path) -> list[str]:
    if not ctrf_file.exists():
        return []
    try:
        tests = read_json(ctrf_file)["results"]["tests"]
        return [t.get("name", "?") for t in tests if t.get("status") != "passed"]
    except Exception:  # noqa: BLE001
        return []


def collect(task, run_dir: Path, agent: dict, verify: dict) -> dict:
    row: dict = {"task": task.id, "slug": task.slug}
    a = agent.get(str(task.id), {})
    v = verify.get(str(task.id), {})
    row["slug"] = v.get("slug") or a.get("slug") or task.slug

    # reward：优先取 Phase 3 对快照的判分；没有 verify 段时（例如只跑了
    # `phase2_agent.py --local`，它内部已经判过分）退回 agent 结果里的值。
    # None 表示**基础设施失败**（无快照 / reward.txt 异常），不是"模型得 0 分"。
    row["reward"] = v.get("reward") if v else a.get("reward")
    row["passed"] = v.get("passed") if v else a.get("passed")
    row["ctrf"] = v.get("ctrf")
    row["mode"] = v.get("mode") or a.get("runner")
    row["verify_ok"] = v.get("ok")
    row["verify_error"] = v.get("error")
    row["deliverables"] = v.get("deliverables")

    row["steps"] = a.get("steps")
    row["cost"] = a.get("cost")
    row["agent_seconds"] = a.get("agent_seconds")
    row["changed_files"] = a.get("changed_files")
    row["snapshot"] = a.get("snapshot")
    row["exit_status"] = a.get("exit_status")
    row["agent_error"] = a.get("error")
    # 指纹：Phase 2 的 result.json 里有一份；没有 agent 阶段时退回自己算
    row["task_fingerprint"] = a.get("task_fingerprint") or _fingerprint_fallback(task)

    # 失败的用例名：有 ctrf 用 ctrf，没有（本机判分）就从 verify.log 里抠
    names = failed_from_ctrf(run_dir / f"task{task.id}" / "ctrf.json")
    if not names:
        out_dir = run_dir / f"task{task.id}{_out_suffix(v)}"
        vlog = out_dir / "verify.log"
        if vlog.exists():
            names = failed_from_log(vlog.read_text(encoding="utf-8", errors="replace"))
    row["failed_tests"] = names
    return row


MODE_SUFFIX = {
    "base": "_base",
    "oracle": "_oracle",
    "local_base": "_local",
    "local_oracle": "_local_oracle",
}


def _out_suffix(v: dict) -> str:
    return MODE_SUFFIX.get(v.get("mode") or "", "")


def _fingerprint_fallback(task) -> str | None:
    from common import task_fingerprint
    try:
        return task_fingerprint(task)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 跨 run 指纹比对
# --------------------------------------------------------------------------- #

def fingerprint_baseline(current_run_id: str) -> tuple[dict | None, list[dict]]:
    """找最近一次（非本次）带指纹的 run 作基线，比对每题指纹。

    返回 (baseline, diffs)。baseline 为 None 表示没有可比的基线。
    """
    candidates: list[tuple[float, Path]] = []
    if RUNS.is_dir():
        for d in RUNS.iterdir():
            if not d.is_dir() or d.name == current_run_id:
                continue
            if (d / "summary.json").exists():
                candidates.append(((d / "summary.json").stat().st_mtime, d))
    if not candidates:
        return None, []

    mtime, d = max(candidates, key=lambda x: x[0])
    data = read_json(d / "summary.json")
    rows = data.get("rows", data) if isinstance(data, dict) else data
    base_fp = {str(r.get("task")): r.get("task_fingerprint")
               for r in rows if isinstance(r, dict) and r.get("task_fingerprint")}
    if not base_fp:
        return None, []

    return (
        {"run_id": d.name, "generated_at": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(mtime)), "fingerprints": base_fp},
        base_fp,
    )


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #

def fmt_num(x, nd=2):
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def render_md(run_id: str, rows: list[dict], meta: dict, model: str | None) -> str:
    total = len(rows)
    graded = [r for r in rows if r["reward"] is not None]
    passed = [r for r in graded if r["passed"]]

    lines = [f"# 测评汇总 — run `{run_id}`", ""]
    if model:
        lines += [f"模型：`{model}`", ""]
    ctrl = meta.get("controls") or {}
    if ctrl:
        parts = [f"{k} {'✓' if v['ok'] else '✗'}" for k, v in ctrl.items()]
        lines += [f"对照证据（不参与评分）：{'、'.join(parts)}", ""]
    lines += [
        f"**{len(passed)}/{total} 通过**"
        + (f"（{len(graded)} 题有有效判定）" if len(graded) != total else ""),
        "",
    ]

    # 跨 run 可比性告警
    if meta.get("fingerprint_comparable") is False and meta.get("fingerprint_baseline"):
        b = meta["fingerprint_baseline"]
        lines += [
            f"> ⚠️ **与基线 run `{b['run_id']}` 不可比**：以下题目的指纹已变化，"
            f"说明题面/判定标准/数据被改过，两次 run 的分数不能直接比较。",
            "",
        ]
        for d in meta.get("fingerprint_diffs", []):
            lines.append(f"> - task{d['task']}：`{d['baseline']}` → `{d['current']}`")
        lines.append("")

    lines += [
        "| task | 题目 | 结果 | 用例(通过/总) | 步数 | 耗时(s) | 改动文件 | 成本($) | 指纹 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["reward"] is None:
            res = "未判定"
        else:
            res = "PASS" if r["passed"] else "FAIL"
        c = r.get("ctrf") or {}
        cases = f"{c.get('passed', '-')}/{c.get('tests', '-')}" if c else "-"
        fp = (r.get("task_fingerprint") or "-")[:8]
        lines.append(
            f"| task{r['task']} | {r['slug']} | {res} | {cases} | "
            f"{fmt_num(r['steps'], 0)} | {fmt_num(r['agent_seconds'], 0)} | "
            f"{fmt_num(r['changed_files'], 0)} | {fmt_num(r['cost'])} | `{fp}` |"
        )

    fails = [r for r in rows if r["reward"] == 0]
    if fails:
        lines += ["", "## 失败明细", ""]
        for r in fails:
            c = r.get("ctrf") or {}
            head = (f"### task{r['task']} {r['slug']} — "
                    f"{c.get('failed', '?')} 个用例失败" if c
                    else f"### task{r['task']} {r['slug']} — reward=0")
            lines.append(head)
            if r.get("agent_error"):
                lines.append(f"- agent 异常：`{r['agent_error']}`")
            if r.get("verify_error"):
                lines.append(f"- 验证异常：`{r['verify_error']}`")
            if r.get("exit_status"):
                lines.append(f"- 结束原因：`{r['exit_status']}`")
            for name in r.get("failed_tests", [])[:12]:
                lines.append(f"- `{name}`")
            if len(r.get("failed_tests", [])) > 12:
                lines.append(f"- …另有 {len(r['failed_tests']) - 12} 个")
            lines.append("")

    bad = [r for r in rows if r["reward"] is None]
    if bad:
        lines += ["", "## 无有效判定（需排查，非模型问题）", ""]
        for r in bad:
            reason = (r.get("verify_error") or r.get("agent_error")
                      or "该题还没有跑过 Phase 3（缺 taskN/verify_result.json）")
            lines.append(f"- task{r['task']} {r['slug']}：`{reason}`")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    run_dir = new_run_dir(args.run_id)

    agent = load_agent_section(run_dir)
    verify, verify_src = pick_verify(run_dir)
    if not verify:
        log("[警告] 没有找到任何 verify summary —— 是不是还没跑 Phase 3？")

    _, tasks = load_tasks()
    rows = [collect(t, run_dir, agent, verify) for t in tasks]

    baseline, base_fp = fingerprint_baseline(args.run_id)
    diffs = []
    if base_fp:
        for r in rows:
            cur = r.get("task_fingerprint")
            b = base_fp.get(str(r["task"]))
            if cur and b and cur != b:
                diffs.append({"task": r["task"], "baseline": b, "current": cur})

    model = next((a.get("model") for a in agent.values() if a.get("model")), None)
    graded = [r for r in rows if r["reward"] is not None]
    passed = [r for r in graded if r["passed"]]

    meta = {
        "run_id": args.run_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "verify_source": verify_src,
        "controls": controls_evidence(run_dir),
        "total": len(rows),
        "graded": len(graded),
        "passed": len(passed),
        "fingerprint_baseline": baseline,
        "fingerprint_comparable": (not diffs) if baseline else None,
        "fingerprint_diffs": diffs,
    }

    write_json(run_dir / "summary.json", {"meta": meta, "rows": rows})
    md = render_md(args.run_id, rows, meta, model)
    (run_dir / "summary.md").write_text(md, encoding="utf-8")

    if diffs:
        log(f"[警告] 与基线 run `{baseline['run_id']}` 的指纹不一致："
            + ", ".join(f"task{d['task']}" for d in diffs)
            + " —— 分数不可直接比较")
    log(f"汇总完成：{len(passed)}/{len(rows)} 通过"
        + (f"（{len(graded)} 题有有效判定）" if len(graded) != len(rows) else ""))
    log(f"  summary.json -> {run_dir / 'summary.json'}")
    log(f"  summary.md   -> {run_dir / 'summary.md'}")
    print()
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
