#!/bin/bash
# 参考解（oracle）：从 study.db 计算 TK 参数并写出 tk_parameters.csv。
# 纯标准库实现——与 build-tools/clinical-tk-parameter-analysis/build_golden.py
# 的 numpy 版本是两条独立编码路径，互为交叉验证。
#
# APP_DIR 约定：容器里是 /app（镜像 WORKDIR）；本机冒烟时由 eval/common.py 的
# run_oracle_local() 指到临时铺好的目录，所以路径必须从环境变量取。
#
# ⚠️ heredoc 标记必须是 PY 三个字符以内的一致写法：eval/common.py 的
# extract_payload() 用「文中第一次出现该标记」来切负载，所以本文件里
# **不要在其他地方再写出那个标记**（包括注释里），否则会切错位置。
set -euo pipefail

APP_DIR="${APP_DIR:-/app}"
export APP_DIR

cat > /tmp/_solve.py <<'PY'
# -*- coding: utf-8 -*-
import csv
import math
import os
import sqlite3

APP = os.environ.get("APP_DIR", "/app")
DB = os.path.join(APP, "study.db")
OUT = os.path.join(APP, "tk_parameters.csv")


# --------------------------------------------------------------------------
# 1. 读取协议参数
# --------------------------------------------------------------------------
conn = sqlite3.connect(DB)
cur = conn.cursor()
cfg = dict(cur.execute("SELECT key, value FROM protocol_config").fetchall())

LLOQ = float(cfg["lloq_ng_ml"])
ULOQ = float(cfg["uloq_ng_ml"])
WIN_ABS = float(cfg["sample_window_abs_h"])
WIN_REL = float(cfg["sample_window_rel_pct"]) / 100.0
DOSE_DEV_MAX = float(cfg["dose_deviation_max_pct"])
MIN_PTS = int(cfg["min_terminal_points"])
MAX_PTS = int(cfg["max_terminal_points"])
R2_MIN = float(cfg["terminal_r2_min"])
MAX_EXTRAP = float(cfg["max_extrap_pct"])


# --------------------------------------------------------------------------
# 2. 汇总每只动物的基本信息与实际剂量
# --------------------------------------------------------------------------
meta = {}
for aid, grp, nominal, route, _sex, _bw in cur.execute(
    "SELECT animal_id, dose_group, nominal_dose_mg_kg, route, sex, body_weight_kg FROM animals"
).fetchall():
    actual = cur.execute(
        "SELECT actual_dose_mg_kg FROM dosing_records WHERE animal_id=?", (aid,)
    ).fetchone()[0]
    deviation = abs(actual - nominal) / nominal * 100.0
    meta[aid] = {
        "group": grp,
        "route": route,
        "actual_dose": actual,
        "excluded": deviation > DOSE_DEV_MAX,
    }

# 采样记录
sampling = {}
for aid, tp, actual_t, status in cur.execute(
    "SELECT animal_id, timepoint_h, actual_time_h, sample_status FROM sample_records"
).fetchall():
    sampling[(aid, tp)] = (actual_t, status)

# 生分析结果：先按 run_id 升序读出所有接受记录，后者覆盖前者 -> 天然取 run_id 最大
measured = {}
for rid, aid, tp, conc, accepted in cur.execute(
    "SELECT run_id, animal_id, timepoint_h, conc_ng_ml, accepted FROM bioanalytical_runs ORDER BY run_id"
).fetchall():
    if accepted == 1:
        measured[(aid, tp)] = conc


# --------------------------------------------------------------------------
# 3. 逐动物构建"可定量时点序列"
# --------------------------------------------------------------------------
def build_points(aid):
    pts = []
    for (animal, tp), conc in measured.items():
        if animal != aid:
            continue
        actual_t, status = sampling[(aid, tp)]

        if status != "OK" or actual_t is None:
            continue                                    # 样本缺失
        window = WIN_ABS if tp <= 1.0 else tp * WIN_REL
        if abs(actual_t - tp) > window + 1e-9:
            continue                                    # 采样超窗
        if conc == "BLQ":
            continue                                    # 低于定量下限，不按 0 处理
        value = float(conc)
        if value < LLOQ or value > ULOQ:
            continue
        pts.append((actual_t, value))
    pts.sort(key=lambda p: p[0])
    return pts


# --------------------------------------------------------------------------
# 4. PK 计算工具
# --------------------------------------------------------------------------
def trapezoid(c1, c2, dt):
    """线性上升 / 对数下降梯形法。用于 AUC0-t。"""
    if c1 <= 0 or c2 <= 0 or c2 >= c1:
        return (c1 + c2) / 2.0 * dt
    return (c1 - c2) / math.log(c1 / c2) * dt


def linear_trap(c1, c2, dt):
    """纯线性梯形法，两段均用 (y1 + y2) / 2 × dt。用于 AUMC0-t。

    题面明确要求 AUMC 走线性（这也是 PKNCA / WinNonlin 的行业惯例），
    与 AUC0-t 的"线性上升 / 对数下降"不同，不要混用。
    """
    return (c1 + c2) / 2.0 * dt


def linear_fit(xs, ys):
    """返回 (slope, intercept, adj_r2)。"""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    if n > 2 and r2 < 1.0:
        adj = 1.0 - (1.0 - r2) * (n - 1) / (n - 2)
    else:
        adj = 1.0
    return slope, intercept, adj


def terminal_slope(pts, tmax_idx):
    """在 Tmax 之后的时点中，取 adj-R² 最大的一组（末 3 点起向前扩展）。"""
    tail = pts[tmax_idx + 1:]
    m = len(tail)
    if m < MIN_PTS:
        return None
    best = None
    for k in range(MIN_PTS, min(MAX_PTS, m) + 1):
        subset = tail[m - k:]
        xs = [p[0] for p in subset]
        ys = [math.log(p[1]) for p in subset]
        slope, _, adj = linear_fit(xs, ys)
        if best is None or adj > best[2]:
            best = (slope, k, adj)
    if best is None or best[2] < R2_MIN:
        return None
    return best[0]


# --------------------------------------------------------------------------
# 5. 主流程
# --------------------------------------------------------------------------
FIELDS = [
    "animal_id", "dose_group", "route",
    "cmax_ng_ml", "tmax_h", "auc_0_t_h_ng_ml", "auc_0_inf_h_ng_ml",
    "t_half_h", "mrt_h", "cl_ml_h_kg", "vss_ml_kg",
]

rows = []
for aid in sorted(meta):
    info = meta[aid]
    if info["excluded"]:
        continue

    row = {f: "" for f in FIELDS}
    row["animal_id"] = aid
    row["dose_group"] = info["group"]
    row["route"] = info["route"]

    pts = build_points(aid)
    if len(pts) < 3:
        rows.append(row)
        continue

    times = [p[0] for p in pts]
    concs = [p[1] for p in pts]

    tmax_idx = max(range(len(concs)), key=lambda i: concs[i])
    row["cmax_ng_ml"] = concs[tmax_idx]
    row["tmax_h"] = times[tmax_idx]

    auc = 0.0
    for i in range(len(times) - 1):
        auc += trapezoid(concs[i], concs[i + 1], times[i + 1] - times[i])
    row["auc_0_t_h_ng_ml"] = auc

    slope = terminal_slope(pts, tmax_idx)
    if slope is not None:
        lam = -slope
        t_last, c_last = times[-1], concs[-1]
        tail_area = c_last / lam
        auc_inf = auc + tail_area
        extrap = tail_area / auc_inf * 100.0 if auc_inf > 0 else float("inf")

        row["t_half_h"] = math.log(2.0) / lam

        if extrap <= MAX_EXTRAP:
            row["auc_0_inf_h_ng_ml"] = auc_inf

            if info["route"] == "IV":
                aumc = 0.0
                for i in range(len(times) - 1):
                    aumc += linear_trap(
                        times[i] * concs[i], times[i + 1] * concs[i + 1],
                        times[i + 1] - times[i],
                    )
                aumc_inf = aumc + t_last * c_last / lam + c_last / lam ** 2
                mrt = aumc_inf / auc_inf
                cl = info["actual_dose"] * 1e6 / auc_inf   # mg/kg -> ng/kg
                row["mrt_h"] = mrt
                row["cl_ml_h_kg"] = cl
                row["vss_ml_kg"] = mrt * cl

    rows.append(row)

rows.sort(key=lambda r: (r["dose_group"], r["animal_id"]))

with open(OUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)

conn.close()
print(f"wrote {OUT} ({len(rows)} rows)")
PY

python3 /tmp/_solve.py
