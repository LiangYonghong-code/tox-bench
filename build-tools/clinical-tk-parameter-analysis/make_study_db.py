#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作者侧数据生成器 —— 生成临床前 TK 参数计算任务（task2）的 study.db。

本脚本**刻意放在 build-tools/**：它暴露了生成数据用的真值 PK 模型，
拿到它就能反推所有"真值"，不能进入镜像、也不能随任务包提交给被测 agent。
`environment/Dockerfile` 只 COPY study.db，因此本文件不会进容器。

数据全部由固定模型参数生成，不含随机数，保证任何人任何时候重建都得到同一份库。

模型
----
IV bolus : C(t) = A*exp(-alpha*t) + B*exp(-beta*t)
PO       : C(t) = S*(exp(-ke*t) - exp(-ka*t))

埋入的 6 类数据质量问题（见 instruction.md 的规则部分）：
  1. animal 101 : 1h 存在两条 accepted=1 的重复分析 run（需取 run_id 最大者）
  2. animal 201 : 1h 存在一条 accepted=0 的污染复测 run，且其 run_id 大于被接受的
     原始 run（需先按 accepted 过滤，再取 run_id 最大者；漏掉 accepted 过滤就会
     取到污染值）
  3. 全部动物   : t=0 与尾点 24h 为 BLQ（需排除，AUC0-t 从首个可定量点起算）
  4. animal 301 : 2h 实际采样时间 3.1h，超出允许窗口（该时点需排除）
  5. animal 302 : 实际给药剂量 38 mg/kg，偏离名义 50 mg/kg 超过 10%（整只动物排除）
  6. animal 102 : >=6h 样本 MISSING，终末相采样不足（外推比例 >20%）

用法
----
    python build-tools/clinical-tk-parameter-analysis/make_study_db.py
产出：
    tasks/clinical-tk-parameter-analysis/environment/study.db
    tasks/clinical-tk-parameter-analysis/tests/db.sha256  （判分器用它校验 study.db 未被修改）

⚠️ 重建 study.db 后必须接着重建金标准，否则判分器会拿旧金标准比对新数据：
    python build-tools/clinical-tk-parameter-analysis/build_golden.py
"""

import hashlib
import math
import os
import sqlite3
import sys

# 本脚本住在 build-tools/<题目录名>/ 下，与 tasks/<题目录名>/ 同名。
# 题目录名直接取自本文件的父目录名 —— 这样"这个生成器为哪道题服务"由
# 目录结构保证，不靠散落的字符串常量，也不依赖 eval/tasks.yaml 已经就位。
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TASK = os.path.join(REPO, "tasks", os.path.basename(HERE))
DB_PATH = os.path.join(TASK, "environment", "study.db")
SHA_PATH = os.path.join(TASK, "tests", "db.sha256")

LLOQ = 3.0
ULOQ = 20000.0
BLQ = "BLQ"  # bioanalytical_runs.conc_ng_ml 中的哨兵值

# ---------------------------------------------------------------------------
# 动物与模型参数
# ---------------------------------------------------------------------------
# kind=iv 用 A/alpha/B/beta；kind=po 用 S/ka/ke
ANIMALS = [
    dict(animal_id=101, group="G1", nominal=5.0, route="IV", sex="M",
         kind="iv", A=2500.0, alpha=4.0, B=1200.0, beta=0.30),
    # 102 终末相极长(beta=0.05)，且 >=6h 样本丢失 -> 外推超限
    dict(animal_id=102, group="G1", nominal=5.0, route="IV", sex="F",
         kind="iv", A=2500.0, alpha=4.0, B=1200.0, beta=0.05),
    dict(animal_id=201, group="G2", nominal=15.0, route="PO", sex="M",
         kind="po", S=1000.0, ka=3.0, ke=0.25),
    # 202 消除略快 -> 24h 自然落到 LLOQ 以下
    dict(animal_id=202, group="G2", nominal=15.0, route="PO", sex="F",
         kind="po", S=1000.0, ka=3.0, ke=0.28),
    dict(animal_id=301, group="G3", nominal=50.0, route="PO", sex="M",
         kind="po", S=3333.0, ka=3.0, ke=0.25),
    dict(animal_id=302, group="G3", nominal=50.0, route="PO", sex="F",
         kind="po", S=3333.0, ka=3.0, ke=0.25),
]

# 名义采样时点（h，相对给药）
TIMEPOINTS = {
    "IV": [0.0, 0.083, 0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 12.0, 24.0],
    "PO": [0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 12.0, 24.0],
}

# 实际采样时间的确定性微偏移（在窗口内），按索引取用
JITTER = [0.005, -0.004, 0.008, -0.006, 0.010, -0.008, 0.006, -0.005, 0.009, -0.007, 0.004]

# 体重（kg），仅作背景信息
BODY_WEIGHT = {101: 0.252, 102: 0.241, 201: 0.248, 202: 0.255, 301: 0.260, 302: 0.244}

PROTOCOL_CONFIG = [
    ("lloq_ng_ml", "3.0"),
    ("uloq_ng_ml", "20000.0"),
    ("blq_handling", "exclude_from_auc"),
    ("sample_window_abs_h", "0.0333"),   # 名义时点 <= 1 h 时使用绝对窗口(2 min)
    ("sample_window_rel_pct", "10.0"),   # 名义时点 > 1 h 时使用相对窗口
    ("dose_deviation_max_pct", "10.0"),
    ("min_terminal_points", "3"),
    ("max_terminal_points", "6"),
    ("terminal_r2_min", "0.90"),
    ("max_extrap_pct", "20.0"),
]


def sig(x, n=3):
    """保留 n 位有效数字，模拟真实生物分析报告精度。"""
    if x == 0:
        return 0.0
    digits = -int(math.floor(math.log10(abs(x)))) + (n - 1)
    return round(x, digits)


def true_conc(a, t):
    """动物 a 在时点 t 的真值浓度(ng/mL)，未做 BLQ 处理。"""
    if a["kind"] == "iv":
        return a["A"] * math.exp(-a["alpha"] * t) + a["B"] * math.exp(-a["beta"] * t)
    return a["S"] * (math.exp(-a["ke"] * t) - math.exp(-a["ka"] * t))


def measure(a, t):
    """返回该时点实测记录: (conc 值或 BLQ, 是否接受)。"""
    c = true_conc(a, t)
    if t == 0.0 or c < LLOQ:
        return BLQ
    if c > ULOQ:
        return BLQ  # 超上限需稀释复测，本题不涉及
    return sig(c)


def build(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE animals (
            animal_id       INTEGER PRIMARY KEY,
            dose_group      TEXT    NOT NULL,
            nominal_dose_mg_kg REAL NOT NULL,
            route           TEXT    NOT NULL,
            sex             TEXT    NOT NULL,
            body_weight_kg  REAL    NOT NULL
        );
        CREATE TABLE dosing_records (
            animal_id          INTEGER PRIMARY KEY,
            dose_group         TEXT    NOT NULL,
            nominal_dose_mg_kg REAL    NOT NULL,
            actual_dose_mg_kg  REAL    NOT NULL,
            route              TEXT    NOT NULL,
            dose_time_h        REAL    NOT NULL
        );
        CREATE TABLE sample_records (
            animal_id    INTEGER NOT NULL,
            timepoint_h  REAL    NOT NULL,   -- 计划采样时间
            actual_time_h REAL,              -- 实际采样时间(NULL=未采集)
            sample_status TEXT   NOT NULL,   -- OK / MISSING
            PRIMARY KEY (animal_id, timepoint_h)
        );
        CREATE TABLE bioanalytical_runs (
            run_id        INTEGER PRIMARY KEY,
            animal_id     INTEGER NOT NULL,
            timepoint_h   REAL    NOT NULL,
            conc_ng_ml    TEXT    NOT NULL,  -- 数值或 'BLQ'
            accepted      INTEGER NOT NULL   -- 1=接受 0=拒绝
        );
        CREATE TABLE protocol_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    run_id = 9000

    for a in ANIMALS:
        aid = a["animal_id"]
        cur.execute(
            "INSERT INTO animals VALUES (?,?,?,?,?,?)",
            (aid, a["group"], a["nominal"], a["route"], a["sex"], BODY_WEIGHT[aid]),
        )

        # --- 陷阱 5: 302 实际剂量显著偏离名义剂量 -------------------------
        actual_dose = a["nominal"]
        if aid == 302:
            actual_dose = 38.0  # 名义 50 -> 偏离 24%，超过 10% 阈值

        cur.execute(
            "INSERT INTO dosing_records VALUES (?,?,?,?,?,?)",
            (aid, a["group"], a["nominal"], actual_dose, a["route"], 0.0),
        )

        for idx, t in enumerate(TIMEPOINTS[a["route"]]):
            # --- 陷阱 4: 301 的 2h 时点实际采样严重超窗 -------------------
            if aid == 301 and t == 2.0:
                actual_t = 3.1
                status = "OK"
            # --- 陷阱 6: 102 的 >=6h 样本未采集到 -------------------------
            elif aid == 102 and t >= 6.0:
                actual_t = None
                status = "MISSING"
            else:
                actual_t = round(t + JITTER[idx % len(JITTER)], 4)
                status = "OK"

            cur.execute(
                "INSERT INTO sample_records VALUES (?,?,?,?)",
                (aid, t, actual_t, status),
            )

            if status == "MISSING":
                continue

            val = measure(a, t)

            run_id += 1
            cur.execute(
                "INSERT INTO bioanalytical_runs VALUES (?,?,?,?,?)",
                (run_id, aid, t, str(val), 1),
            )

            # --- 陷阱 2: 201 的 1h 原始 run 被接受后，因内标漂移做了复测，该复测
            # run 在 QC 复核中被拒收，且 run_id 更大。只按 run_id 取最大而不先按
            # accepted 过滤，就会拿到 3.4 倍的污染值。
            if aid == 201 and t == 1.0:
                run_id += 1
                cur.execute(
                    "INSERT INTO bioanalytical_runs VALUES (?,?,?,?,?)",
                    (run_id, aid, t, str(sig(true_conc(a, 1.0) * 3.4)), 0),
                )

            # --- 陷阱 1: 101 的 1h 复测，run_id 更大者为准 ----------------
            if aid == 101 and t == 1.0:
                run_id += 1
                cur.execute(
                    "INSERT INTO bioanalytical_runs VALUES (?,?,?,?,?)",
                    (run_id, aid, t, str(sig(true_conc(a, 1.0) * 0.83)), 1),
                )

    cur.executemany("INSERT INTO protocol_config VALUES (?,?)", PROTOCOL_CONFIG)
    conn.commit()
    conn.close()
    print(f"wrote {path}")


if __name__ == "__main__":
    # 允许显式给输出路径（作者调试用），默认写进任务包的 environment/。
    target = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    build(target)

    # 判分器把"不得修改 study.db"变成可执行契约，期望值就放在 tests/db.sha256。
    # 由生成器顺手刷新，避免"重建了库却忘了同步哈希"这个已知问题。
    # 只在写正式路径时刷新 —— 显式给了别的路径说明是作者在调试，不该动仓库里的哈希。
    if os.path.abspath(target) == os.path.abspath(DB_PATH):
        sha = hashlib.sha256(open(target, "rb").read()).hexdigest()
        os.makedirs(os.path.dirname(SHA_PATH), exist_ok=True)
        with open(SHA_PATH, "w", encoding="utf-8") as f:
            f.write(sha + "\n")
        print("SHA256:", sha, "->", SHA_PATH)
