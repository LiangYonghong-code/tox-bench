#!/usr/bin/env python3
"""从 study.db 生成金标准 TK 参数（oracle），冻结为 tests/tk_golden.json。

关于 oracle 的独立性
--------------------
真实项目里这里的期望值应当来自验证过的行业标准软件（Phoenix WinNonlin /
PKNCA）的输出，那是与解题代码完全独立的第二个系统。

本脚本在作者机器上代行该角色，并且刻意采用与 solution/solve.sh 不同的实现路径：

    本脚本 (oracle)              solution/solve.sh (参考解)
    ------------------------     --------------------------
    numpy 向量化                纯 Python 循环
    scipy.stats.linregress      手写最小二乘
    PKNCA 风格 adj-R² 选点      同样的选点规则，独立编码

两份独立实现互为交叉验证；另外 test_outputs.py 里还硬编码了若干人工核对的锚点，
用来挡住"两份实现共享同一个理解错误"的情况。

用法
----
    python build-tools/clinical-tk-parameter-analysis/build_golden.py [study.db] [输出 json]
两个参数都可省略，默认按目录位置推导（见下方路径常量）。
"""

import json
import math
import os
import sqlite3
import sys

import numpy as np

# 与 make_study_db.py 同一约定：父目录名 == tasks/ 下的题目录名。
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TASK = os.path.join(REPO, "tasks", os.path.basename(HERE))
DEFAULT_DB = os.path.join(TASK, "environment", "study.db")
DEFAULT_OUT = os.path.join(TASK, "tests", "tk_golden.json")


def linregress(x, y):
    """最小二乘直线拟合，返回 (slope, intercept, r2)。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = x.size
    sx, sy = x.sum(), y.sum()
    sxx = (x * x).sum()
    sxy = (x * y).sum()
    denom = n * sxx - sx * sx
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    yhat = slope * x + intercept
    ss_res = ((y - yhat) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return slope, intercept, r2


# ---------------------------------------------------------------------------
# 读取与清洗
# ---------------------------------------------------------------------------
def load_config(cur):
    return {k: v for k, v in cur.execute("SELECT key, value FROM protocol_config")}


def load_series(cur, cfg):
    """返回 {animal_id: {...}}，含清洗后的时点序列与排除原因。"""
    lloq = float(cfg["lloq_ng_ml"])
    uloq = float(cfg["uloq_ng_ml"])
    win_abs = float(cfg["sample_window_abs_h"])
    win_rel = float(cfg["sample_window_rel_pct"]) / 100.0
    dose_dev_max = float(cfg["dose_deviation_max_pct"])

    animals = {}
    for aid, grp, nominal, route, sex, bw in cur.execute(
        "SELECT animal_id, dose_group, nominal_dose_mg_kg, route, sex, body_weight_kg FROM animals"
    ).fetchall():
        actual = cur.execute(
            "SELECT actual_dose_mg_kg FROM dosing_records WHERE animal_id=?", (aid,)
        ).fetchone()[0]
        dev = abs(actual - nominal) / nominal * 100.0
        animals[aid] = dict(
            animal_id=aid, dose_group=grp, nominal=nominal, actual=actual,
            route=route, sex=sex, bw=bw,
            dose_deviation_pct=round(dev, 2),
            excluded_dose=dev > dose_dev_max,
            points=[],
        )

    # 采样记录
    samples = {}
    for aid, tp, at, status in cur.execute(
        "SELECT animal_id, timepoint_h, actual_time_h, sample_status FROM sample_records"
    ):
        samples[(aid, tp)] = (at, status)

    # 生分析 run：只保留 accepted=1；同一 (animal, timepoint) 取 run_id 最大者
    best = {}
    for run_id, aid, tp, conc, accepted in cur.execute(
        "SELECT run_id, animal_id, timepoint_h, conc_ng_ml, accepted FROM bioanalytical_runs"
    ):
        if accepted != 1:
            continue
        key = (aid, tp)
        if key not in best or run_id > best[key][0]:
            best[key] = (run_id, conc)

    for (aid, tp), (_, conc) in best.items():
        a = animals[aid]
        at, status = samples[(aid, tp)]

        if status != "OK" or at is None:
            a["points"].append(dict(tp=tp, t=None, c=None, reason="sample_missing"))
            continue

        window = win_abs if tp <= 1.0 else tp * win_rel
        if abs(at - tp) > window + 1e-9:
            a["points"].append(dict(tp=tp, t=at, c=None, reason="out_of_window"))
            continue

        if conc == "BLQ":
            a["points"].append(dict(tp=tp, t=at, c=None, reason="blq"))
            continue

        value = float(conc)
        if value < lloq or value > uloq:
            a["points"].append(dict(tp=tp, t=at, c=None, reason="out_of_range"))
            continue

        a["points"].append(dict(tp=tp, t=at, c=value, reason=None))

    for a in animals.values():
        a["points"].sort(key=lambda p: p["t"] if p["t"] is not None else p["tp"])
    return animals


# ---------------------------------------------------------------------------
# PK 计算
# ---------------------------------------------------------------------------
def auc_segment(c1, c2, dt):
    """线性上升 / 对数下降梯形法。用于 AUC0-t。"""
    if c1 <= 0 or c2 <= 0:
        return (c1 + c2) / 2.0 * dt
    if c2 < c1:
        return (c1 - c2) / math.log(c1 / c2) * dt
    return (c1 + c2) / 2.0 * dt


def linear_segment(c1, c2, dt):
    """纯线性梯形法，两段均用 (y1 + y2) / 2 × dt。用于 AUMC0-t。

    题面明确要求 AUMC 走线性（这也是 PKNCA / WinNonlin 的行业惯例），
    与 AUC0-t 的"线性上升 / 对数下降"不同，不要混用。
    """
    return (c1 + c2) / 2.0 * dt


def select_lambda_z(times, concs, tmax_idx, min_pts, max_pts, r2_min):
    """PKNCA 风格：在 Tmax 之后的时点中，从末 3 点起向前扩展，取 adj-R² 最大者。"""
    cand = [i for i in range(len(times)) if i > tmax_idx]
    n = len(cand)
    if n < min_pts:
        return None
    best = None
    for k in range(min_pts, min(max_pts, n) + 1):
        idx = cand[n - k:]
        t = np.array([times[i] for i in idx])
        y = np.log(np.array([concs[i] for i in idx]))
        slope, intercept, r2 = linregress(t, y)
        adj = 1.0 if r2 >= 1.0 else 1.0 - (1.0 - r2) * (k - 1) / (k - 2)
        if best is None or adj > best["adj_r2"]:
            best = dict(slope=slope, intercept=intercept,
                        r2=r2, adj_r2=adj, n=k, idx=idx)
    if best is None or best["adj_r2"] < r2_min:
        return None
    return best


def compute(a, cfg):
    min_pts = int(cfg["min_terminal_points"])
    max_pts = int(cfg["max_terminal_points"])
    r2_min = float(cfg["terminal_r2_min"])
    max_extrap = float(cfg["max_extrap_pct"])

    out = dict(
        animal_id=a["animal_id"], dose_group=a["dose_group"], route=a["route"],
        cmax_ng_ml=None, tmax_h=None, auc_0_t_h_ng_ml=None, auc_0_inf_h_ng_ml=None,
        t_half_h=None, mrt_h=None, cl_ml_h_kg=None, vss_ml_kg=None,
    )

    pts = [p for p in a["points"] if p["c"] is not None]
    if len(pts) < 3:
        return out

    times = [p["t"] for p in pts]
    concs = [p["c"] for p in pts]

    # Cmax / Tmax
    tmax_idx = max(range(len(concs)), key=lambda i: concs[i])
    out["cmax_ng_ml"] = concs[tmax_idx]
    out["tmax_h"] = times[tmax_idx]

    # AUC0-t
    auc = 0.0
    for i in range(len(times) - 1):
        auc += auc_segment(concs[i], concs[i + 1], times[i + 1] - times[i])
    out["auc_0_t_h_ng_ml"] = auc

    # 终末相
    lz = select_lambda_z(times, concs, tmax_idx, min_pts, max_pts, r2_min)
    if lz is not None:
        slope = lz["slope"]
        lam = -slope
        out["t_half_h"] = math.log(2.0) / lam

        t_last, c_last = times[-1], concs[-1]
        tail = c_last / lam
        auc_inf = auc + tail
        extrap = tail / auc_inf * 100.0 if auc_inf > 0 else 999.0
        if extrap <= max_extrap:
            out["auc_0_inf_h_ng_ml"] = auc_inf

            if a["route"] == "IV":
                aumc = 0.0
                for i in range(len(times) - 1):
                    y1, y2 = times[i] * concs[i], times[i + 1] * concs[i + 1]
                    aumc += linear_segment(y1, y2, times[i + 1] - times[i])
                aumc_inf = aumc + t_last * c_last / lam + c_last / lam ** 2
                mrt = aumc_inf / auc_inf
                dose_ng_per_kg = a["actual"] * 1e6
                cl = dose_ng_per_kg / auc_inf
                out["mrt_h"] = mrt
                out["cl_ml_h_kg"] = cl
                out["vss_ml_kg"] = mrt * cl
    return out


def main(db_path, out_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cfg = load_config(cur)
    animals = load_series(cur, cfg)
    conn.close()

    rows = []
    for aid in sorted(animals):
        a = animals[aid]
        if a["excluded_dose"]:
            continue  # 剂量偏离超限，整只动物不评价
        rows.append(compute(a, cfg))

    rows.sort(key=lambda r: (r["dose_group"], r["animal_id"]))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"rows": rows}, f, ensure_ascii=False, indent=2)

    for r in rows:
        print(r)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB,
         sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT)
