# -*- coding: utf-8 -*-
"""变异解自检：把参考解的输出逐条改成"看似合理但违反数据清洗规则"的答案，
要求每个变异解至少被一个用例判失败。

这是任务包最重要的作者侧验证：它证明判分器不是"只要交了文件就给分"，
而是真的能分辨 TK 计算中的典型错误。

用法：
    python build-tools/clinical-tk-parameter-analysis/check_discrimination.py
退出码 0 = 全部变异解都被抓住；1 = 存在漏网的变异解（题目或判分器需要修）。

判哪道题由**目录位置**决定：本文件住在 build-tools/<题目录名>/ 下，
只对该目录名对应的 tasks/<题目录名>/ 生效。

与 repeat-dose-noael-judgment 那题的做法差异
--------------------------------------------
那题的变异解直接篡改 oracle 交出的 JSON 字段；本题的陷阱全在**计算过程**里
（取了错的 run、BLQ 当 0、超窗不排除……），篡改结果字段等于凭空捏造偏差。
所以这里保留一份带变异开关的**独立实现** `compute(db, mutant)`：它真的重算一遍，
产出的偏差是真实的（可在 tests/test_outputs.py 的注释里逐条对照）。

代价是这份实现可能与 solution/solve.sh 一起漂移——2026-10 的 AUMC 判分偏差正是如此
（三份实现共享同一个错误理解，自检全绿而题面被违背）。因此这里保留 golden_drift()
把 `""`（正确开关）的输出与冻结的 tk_golden.json 逐字段对拍：它挡不住"一起错"，
但能挡住"悄悄改了一处实现"。真正的外部独立验证（行业标准软件 / 真实 agent）
见 build-tools/clinical-tk-parameter-analysis/README.md。
"""
import csv
import json
import math
import os
import shutil
import sqlite3
import sys

# 与 repeat-dose-noael-judgment 的 check_discrimination.py 同一约定：
# 父目录名 == tasks/ 下的题目录名
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TASK_DIR_NAME = os.path.basename(HERE)
sys.path.insert(0, os.path.join(REPO, "eval"))

import common  # noqa: E402  （复用评测驱动的临时环境与内置 runner）

FIELDS = [
    "animal_id", "dose_group", "route",
    "cmax_ng_ml", "tmax_h", "auc_0_t_h_ng_ml", "auc_0_inf_h_ng_ml",
    "t_half_h", "mrt_h", "cl_ml_h_kg", "vss_ml_kg",
]

# (变异开关, 说明)。"" 表示正确实现——它必须全过，且与 tk_golden.json 逐字段一致。
MUTANTS = [
    ("", "正确实现（必须全过）"),
    ("ignore_accepted", "不做 accepted 过滤，直接取 run_id 最大者"),
    ("min_run", "同一时点取 run_id 最小者（重复分析取错了）"),
    ("blq_zero", "BLQ 按 0 参与梯形积分"),
    ("no_window", "不做过窗过滤"),
    ("no_dose_excl", "不做剂量偏离排除"),
    ("blank_all", "外推超限时把 t½ 也一起留空"),
    ("tmax_nominal", "Tmax/时间量用名义时点而非 actual_time_h"),
    ("aumc_log", "AUMC 下降段误用对数梯形（题面要求纯线性）"),
]


# ------------------------------------------------------------------ 变异实现
def compute(db_path, mutant):
    """带变异开关的独立实现，返回 CSV 行列表（已按 dose_group, animal_id 排序）。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cfg = dict(cur.execute("SELECT key, value FROM protocol_config").fetchall())
    lloq, uloq = float(cfg["lloq_ng_ml"]), float(cfg["uloq_ng_ml"])
    win_abs = float(cfg["sample_window_abs_h"])
    win_rel = float(cfg["sample_window_rel_pct"]) / 100.0
    dose_max = float(cfg["dose_deviation_max_pct"])
    min_pts, max_pts = int(cfg["min_terminal_points"]), int(cfg["max_terminal_points"])
    r2_min, max_extrap = float(cfg["terminal_r2_min"]), float(cfg["max_extrap_pct"])

    meta = {}
    for aid, grp, nom, route in cur.execute(
        "SELECT animal_id, dose_group, nominal_dose_mg_kg, route FROM animals"
    ).fetchall():
        actual = cur.execute(
            "SELECT actual_dose_mg_kg FROM dosing_records WHERE animal_id=?", (aid,)
        ).fetchone()[0]
        dev = abs(actual - nom) / nom * 100.0
        meta[aid] = dict(
            group=grp, route=route, actual=actual,
            excluded=dev > dose_max and mutant != "no_dose_excl",
        )

    samples = {(a, tp): (at, st) for a, tp, at, st in cur.execute(
        "SELECT animal_id, timepoint_h, actual_time_h, sample_status FROM sample_records"
    ).fetchall()}

    measured = {}
    for rid, aid, tp, conc, accepted in cur.execute(
        "SELECT run_id, animal_id, timepoint_h, conc_ng_ml, accepted "
        "FROM bioanalytical_runs ORDER BY run_id"
    ).fetchall():
        if accepted != 1 and mutant != "ignore_accepted":
            continue
        if mutant == "min_run":
            if (aid, tp) not in measured:
                measured[(aid, tp)] = conc
        else:
            measured[(aid, tp)] = conc

    def points(aid):
        out = []
        for (a, tp), conc in measured.items():
            if a != aid:
                continue
            at, st = samples[(aid, tp)]
            if st != "OK" or at is None:
                continue
            window = win_abs if tp <= 1.0 else tp * win_rel
            if mutant != "no_window" and abs(at - tp) > window + 1e-9:
                continue
            if conc == "BLQ":
                if mutant != "blq_zero":
                    continue
                value = 0.0
            else:
                value = float(conc)
                if value < lloq or value > uloq:
                    continue
            out.append((at, value))
        out.sort()
        return out

    def segment(c1, c2, dt):
        """线性上升 / 对数下降梯形法。用于 AUC0-t。"""
        if c1 <= 0 or c2 <= 0 or c2 >= c1:
            return (c1 + c2) / 2.0 * dt
        return (c1 - c2) / math.log(c1 / c2) * dt

    def linear(c1, c2, dt):
        """纯线性梯形法。用于 AUMC0-t（题面要求，与 AUC0-t 不同）。"""
        return (c1 + c2) / 2.0 * dt

    def fit(xs, ys):
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        slope = sxy / sxx
        intercept = my - slope * mx
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        ss_tot = sum((y - my) ** 2 for y in ys)
        r2 = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
        adj = 1.0 if (n <= 2 or r2 >= 1.0) else 1.0 - (1.0 - r2) * (n - 1) / (n - 2)
        return slope, adj

    rows = []
    for aid in sorted(meta):
        info = meta[aid]
        if info["excluded"]:
            continue
        row = {f: "" for f in FIELDS}
        row["animal_id"] = aid
        row["dose_group"] = info["group"]
        row["route"] = info["route"]

        pts = points(aid)
        if len(pts) < 3:
            rows.append(row)
            continue

        # tmax_nominal：时间量退回名义时点。名义时点从 sample_records 反查。
        if mutant == "tmax_nominal":
            nominal = {v[0]: k[1] for k, v in samples.items() if k[0] == aid and v[0] is not None}
            pts = [(nominal.get(t), c) for t, c in pts]
            pts = [(t, c) for t, c in pts if t is not None]

        times = [p[0] for p in pts]
        concs = [p[1] for p in pts]
        tmax_idx = max(range(len(concs)), key=lambda i: concs[i])
        row["cmax_ng_ml"] = concs[tmax_idx]
        row["tmax_h"] = times[tmax_idx]

        auc = 0.0
        for i in range(len(times) - 1):
            auc += segment(concs[i], concs[i + 1], times[i + 1] - times[i])
        row["auc_0_t_h_ng_ml"] = auc

        tail = pts[tmax_idx + 1:]
        m = len(tail)
        best = None
        for k in range(min_pts, min(max_pts, m) + 1):
            # 只对 c>0 的点做 ln(C) 回归：blq_zero 变异体会引入 0 浓度，
            # 这里简化为"跳过 0 值点"，让它产出一个数值解供 verifier 判，
            # 而不是直接崩溃——崩溃只能说明程序异常，不能证明断言抓得住。
            subset = [p for p in tail[m - k:] if p[1] > 0]
            if len(subset) < min_pts:
                continue
            slope, adj = fit([p[0] for p in subset], [math.log(p[1]) for p in subset])
            if best is None or adj > best[1]:
                best = (slope, adj)

        if best is not None and best[1] >= r2_min:
            lam = -best[0]
            t_last, c_last = times[-1], concs[-1]
            tail_area = c_last / lam
            auc_inf = auc + tail_area
            extrap = tail_area / auc_inf * 100.0 if auc_inf > 0 else float("inf")

            if mutant != "blank_all":
                row["t_half_h"] = math.log(2.0) / lam

            if extrap <= max_extrap:
                row["auc_0_inf_h_ng_ml"] = auc_inf
                if info["route"] == "IV":
                    aumc = 0.0
                    for i in range(len(times) - 1):
                        fn = segment if mutant == "aumc_log" else linear
                        aumc += fn(
                            times[i] * concs[i], times[i + 1] * concs[i + 1],
                            times[i + 1] - times[i],
                        )
                    aumc_inf = aumc + t_last * c_last / lam + c_last / lam ** 2
                    mrt = aumc_inf / auc_inf
                    cl = info["actual"] * 1e6 / auc_inf
                    row["mrt_h"] = mrt
                    row["cl_ml_h_kg"] = cl
                    row["vss_ml_kg"] = mrt * cl
        rows.append(row)

    conn.close()
    rows.sort(key=lambda r: (r["dose_group"], r["animal_id"]))
    return rows


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {int(r["animal_id"]): r for r in csv.DictReader(f)}


def golden_drift(rows, golden):
    """正确开关的输出与冻结金标准是否一致，防止这份实现悄悄漂移。"""
    got = {int(r["animal_id"]): r for r in rows}
    for exp in golden:
        aid = int(exp["animal_id"])
        if aid not in got:
            return f"缺少 animal {aid}"
        for col in FIELDS:
            if col in ("animal_id", "dose_group", "route"):
                continue
            g = got[aid][col]
            e = exp[col]
            if e is None:
                if str(g).strip() != "":
                    return f"{aid}.{col} 应为空"
            elif str(g).strip() == "":
                return f"{aid}.{col} 不应为空"
            elif abs(float(g) - e) / abs(e) > 0.005:
                return f"{aid}.{col} 偏差 >0.5%"
    return None


# ------------------------------------------------------------------ 主流程
def check_one(task):
    print(f"\n######## task{task.id} {task.slug} ########")
    app = common.prepare_local_app(task)
    try:
        db = os.path.join(app, "study.db")
        out = os.path.join(app, "tk_parameters.csv")
        with open(task.tests_dir / "tk_golden.json", encoding="utf-8") as f:
            golden = json.load(f)["rows"]

        # 参考解必须自己跑得起来，否则"变异解被抓住"这件事无从谈起
        ok, output = common.run_oracle_local(task, app)
        if not ok:
            print(output)
            raise SystemExit("参考解执行失败，先修参考解")

        results = common.run_tests_builtin(task, app)
        oracle_failed = [n for n, ok, _ in results if not ok]
        print(f"[oracle] {'全部通过' if not oracle_failed else '失败: ' + ', '.join(oracle_failed)}")
        if oracle_failed:
            return False

        print(f"\n[变异解] 共 {len(MUTANTS)} 个，要求每个至少被 1 个用例抓住\n")
        header = f"{'变异解':<40} {'结果':<6} 抓住它的用例"
        print(header)
        print("-" * len(header))
        escaped = []
        for mutant, label in MUTANTS:
            try:
                rows = compute(db, mutant)
            except Exception as exc:  # noqa: BLE001 - 变异解自己崩溃也算被捕获
                print(f"{label:<40} {'抓住':<6} 变异解崩溃 ({type(exc).__name__})")
                continue
            write_csv(out, rows)

            if mutant == "":
                # 正确开关：必须全过，且与冻结金标准逐字段一致（交叉验证参考解）
                drift = golden_drift(rows, golden)
                res = common.run_tests_builtin(task, app)
                caught = [n for n, ok, _ in res if not ok]
                if caught or drift:
                    print(f"{label:<40} {'漏网':<6} 正确解竟然失败: "
                          f"{', '.join(caught) if caught else drift}")
                    return False
                print(f"{label:<40} {'通过':<6} 全过，且与 tk_golden.json 逐字段一致")
                continue

            res = common.run_tests_builtin(task, app)
            caught = [n for n, ok, _ in res if not ok]
            if not caught:
                escaped.append(label)
            print(f"{label:<40} {'抓住' if caught else '漏网':<6} {', '.join(caught[:3])}"
                  + (" ..." if len(caught) > 3 else ""))

        print()
        if escaped:
            print("存在漏网的变异解，判分器需要加强：")
            for label in escaped:
                print("  -", label)
            return False
        print("全部变异解均被抓住：判分器能识别每一类错误。")
        return True
    finally:
        shutil.rmtree(app, ignore_errors=True)


def main():
    # 不接 --task：下面的 MUTANTS 是针对本题的 TK 数据清洗规则写的，
    # 拿去跑别的题只会得到无意义的失败。目录名就是选题依据。
    _, tasks = common.load_tasks()
    mine = [t for t in tasks if t.dir.name == TASK_DIR_NAME]
    if not mine:
        raise SystemExit(
            f"eval/tasks.yaml 里没有 dir 名为 `{TASK_DIR_NAME}` 的题目；"
            f"本脚本住在 build-tools/{TASK_DIR_NAME}/，只能为本目录对应的题服务")
    return 0 if check_one(mine[0]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
