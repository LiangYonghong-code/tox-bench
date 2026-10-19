# -*- coding: utf-8 -*-
"""临床前 TK 参数计算 —— 判分器：只读取交付物与 tests/ 下的基准文件，不执行被测代码。

设计要点
--------
* **无 pytest 依赖也能跑**：本文件只使用 assert，不 import pytest、不使用 fixture，
  因此既能被 pytest 收集，也能被 eval/common.py 的内置 runner 直接调用
  （便于在没有 pytest / Docker 的机器上做冒烟验证）。
* **判分器与解题环境隔离**：本文件在 agent 结束、容器 commit 之后才注入，agent 看不到断言。
* **四类用例**：
  结构检查 : 文件 / 列 / 行数 / 输入库未被改动
  纳排检查 : 可评价动物齐备、剂量偏离动物被排除
  全量对账 : 与金标准逐值比对（tk_golden.json）
  定向陷阱 : 每个埋入的数据质量陷阱一个独立测试，失败时可直接定位漏掉的规则

金标准 tk_golden.json 由 build-tools/clinical-tk-parameter-analysis/build_golden.py
生成（与 solution/solve.sh 实现路径不同），相当于行业标准软件 WinNonlin / PKNCA 的
替代物。要确认每条规则都真的被独立捕获，作者侧在仓库根跑：
    python build-tools/clinical-tk-parameter-analysis/check_discrimination.py

数值判定：相对误差 <= 0.5%。该容差足以吸收浮点与实现差异，又远小于任一陷阱造成的
偏差（实测 2.88% ~ 240%），因此不会放过错误解。
"""

import csv
import hashlib
import json
import os

# 容器里 WORKDIR 就是 /app；本机冒烟（eval/common.py 的 --local 分支）会把它指向
# 临时铺好的目录，因此路径必须从环境变量取，不能硬编码。
APP = os.environ.get("TASK_APP_DIR", "/app")
DB_PATH = os.path.join(APP, "study.db")
OUTPUT_PATH = os.path.join(APP, "tk_parameters.csv")

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN_PATH = os.path.join(HERE, "tk_golden.json")

REL_TOL = 0.005

EXPECTED_COLUMNS = [
    "animal_id", "dose_group", "route",
    "cmax_ng_ml", "tmax_h", "auc_0_t_h_ng_ml", "auc_0_inf_h_ng_ml",
    "t_half_h", "mrt_h", "cl_ml_h_kg", "vss_ml_kg",
]

# 可评价动物：302 因实际剂量偏离名义剂量 24%（> 10%）整体排除
EVALUABLE = [101, 102, 201, 202, 301]
EXCLUDED = [302]


# ---------------------------------------------------------------------------
# 读取工具
# ---------------------------------------------------------------------------
def load_golden():
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        return {int(r["animal_id"]): r for r in json.load(f)["rows"]}


def load_output():
    assert os.path.exists(OUTPUT_PATH), f"输出文件不存在: {OUTPUT_PATH}"
    with open(OUTPUT_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        header = reader.fieldnames
    return header, rows


def output_by_id():
    _, rows = load_output()
    out = {}
    for r in rows:
        out[int(r["animal_id"])] = r
    return out


def num(value):
    """空字符串 / None -> None。"""
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    return float(value)


def close(actual, expected, rel=REL_TOL):
    if expected == 0:
        return abs(actual) < 1e-9
    return abs(actual - expected) / abs(expected) <= rel


# ---------------------------------------------------------------------------
# 一、结构检查
# ---------------------------------------------------------------------------
def test_output_file_exists():
    assert os.path.exists(OUTPUT_PATH), f"输出文件不存在: {OUTPUT_PATH}"


def test_output_columns_exact_order():
    header, _ = load_output()
    assert header == EXPECTED_COLUMNS, (
        f"列名或顺序不符。期望 {EXPECTED_COLUMNS}，实际 {header}"
    )


def test_correct_row_count():
    _, rows = load_output()
    assert len(rows) == len(EVALUABLE), (
        f"期望 {len(EVALUABLE)} 行可评价动物，实际 {len(rows)} 行"
    )


def test_study_db_unmodified():
    """题面约束"不得修改 study.db"：就地"清洗"数据库同样属于违规。

    期望值放在独立的 tests/db.sha256 里，由 build-tools/clinical-tk-parameter-analysis/
    make_study_db.py 重建数据库时自动刷新（而不是硬编码在判分器里，否则重建后必然忘记同步）。
    """
    expected = open(os.path.join(HERE, "db.sha256"), encoding="utf-8").read().strip()
    assert os.path.exists(DB_PATH), f"输入库丢失: {DB_PATH}"
    with open(DB_PATH, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    assert digest == expected, (
        f"study.db 已被修改（sha256 {digest}，期望 {expected}）。"
        "清洗逻辑必须写在程序里，不能改原始数据。"
    )


# ---------------------------------------------------------------------------
# 二、动物纳排
# ---------------------------------------------------------------------------
def test_evaluable_animals_present():
    out = output_by_id()
    missing = [a for a in EVALUABLE if a not in out]
    assert not missing, f"缺少可评价动物: {missing}"


def test_dose_deviation_animal_excluded():
    """302 实际剂量 38 mg/kg vs 名义 50 mg/kg，偏离 24% 超过阈值，必须整体排除。"""
    out = output_by_id()
    assert 302 not in out, (
        "animal 302 剂量偏离 24% 超过 protocol_config 的 "
        "dose_deviation_max_pct=10，不应出现在结果中"
    )


# ---------------------------------------------------------------------------
# 三、全量对账
# ---------------------------------------------------------------------------
def test_all_values_match_golden():
    golden = load_golden()
    out = output_by_id()

    for aid in EVALUABLE:
        assert aid in out, f"缺少 animal {aid}"
        exp_row = golden[aid]
        got_row = out[aid]

        for col in EXPECTED_COLUMNS:
            if col in ("animal_id", "dose_group", "route"):
                continue
            exp = exp_row[col]
            got = num(got_row[col])
            if exp is None:
                assert got is None, (
                    f"animal {aid} 字段 {col} 应为空，实际 {got_row[col]!r}"
                )
            else:
                assert got is not None, (
                    f"animal {aid} 字段 {col} 不应为空（期望 {exp}）"
                )
                assert close(got, exp), (
                    f"animal {aid} 字段 {col}: 期望 {exp}，实际 {got}，"
                    f"相对偏差 {abs(got - exp) / abs(exp) * 100:.2f}%"
                )


def test_categorical_fields():
    golden = load_golden()
    out = output_by_id()
    for aid in EVALUABLE:
        assert out[aid]["dose_group"] == golden[aid]["dose_group"], (
            f"animal {aid} dose_group 错误"
        )
        assert out[aid]["route"] == golden[aid]["route"], (
            f"animal {aid} route 错误"
        )


def test_sort_order():
    _, rows = load_output()
    keys = [(r["dose_group"], int(r["animal_id"])) for r in rows]
    assert keys == sorted(keys), f"未按 dose_group 升序、animal_id 升序排列: {keys}"


# ---------------------------------------------------------------------------
# 四、定向锚点 —— 一个陷阱一个测试
# ---------------------------------------------------------------------------
def test_trap_duplicate_run_uses_latest():
    """101 的 1h 有两条 accepted=1 的重复 run：run_id 9005 浓度 935，run_id 9006 浓度 776。

    规则要求取 run_id 最大者，即 9006 的 776 ng/mL。若误取 9005 的 935，
    AUC0-t 会从 4169.83 变成约 4289.92（+2.88%），远超 0.5% 容差。
    """
    out = output_by_id()
    auc = num(out[101]["auc_0_t_h_ng_ml"])
    assert auc is not None, "animal 101 缺少 auc_0_t_h_ng_ml"
    assert close(auc, 4169.83), (
        f"animal 101 AUC0-t 期望约 4169.83（1h 取 run_id 最大的 776 ng/mL），"
        f"实际 {auc}；若约为 4596 说明重复 run 取了错的那条"
    )


def test_trap_rejected_run_ignored():
    """201 的 1h 的复测 run（run_id 9024，2480 ng/mL）在 QC 复核中被拒收，accepted=0，
    且它的 run_id 大于被接受的原始 run（9023，729 ng/mL）。

    必须先按 accepted 过滤再取 run_id 最大者。若跳过 accepted 过滤直接取 run_id 最大，
    Cmax 会变成 2480（+240.2%）——这一条与"重复 run 取最新"是两个独立规则，不要合并。
    """
    out = output_by_id()
    cmax = num(out[201]["cmax_ng_ml"])
    assert cmax is not None, "animal 201 缺少 cmax_ng_ml"
    assert close(cmax, 729.0), (
        f"animal 201 Cmax 期望约 729（忽略 accepted=0 的 2480），实际 {cmax}"
    )


def test_trap_blq_not_treated_as_zero():
    """202 的首末时点（0h 与 24h）为 BLQ，应排除而非按 0 计入梯形积分。

    若按 0 处理，积分区间会被首末零点撑到 0~24h，
    AUC0-t 从 3025.06 变成约 3288.41（+8.71%）。
    """
    out = output_by_id()
    auc = num(out[202]["auc_0_t_h_ng_ml"])
    assert auc is not None, "animal 202 缺少 auc_0_t_h_ng_ml"
    assert close(auc, 3025.06), (
        f"animal 202 AUC0-t 期望约 3025.06（BLQ 排除），实际 {auc}；"
        f"若约为 3288 说明 BLQ 被当成了 0"
    )


def test_trap_out_of_window_sample_excluded():
    """301 的 2h 计划时点实际在 3.1h 采样（窗口 2h×10% = 0.2h），必须排除。

    若保留该时点，AUC0-t 从 11787.21 变成约 12581.46（+6.74%）。
    """
    out = output_by_id()
    auc = num(out[301]["auc_0_t_h_ng_ml"])
    assert auc is not None, "animal 301 缺少 auc_0_t_h_ng_ml"
    assert close(auc, 11787.21), (
        f"animal 301 AUC0-t 期望约 11787.21（排除超窗时点），实际 {auc}；"
        f"若约为 12769 说明超窗时点未排除"
    )


def test_trap_excessive_extrapolation_blanks_parameters():
    """102 因 >=6h 样本缺失导致外推比例 > 20%，auc_0_inf / mrt / cl / vss 必须留空。"""
    out = output_by_id()
    row = out[102]
    for col in ("auc_0_inf_h_ng_ml", "mrt_h", "cl_ml_h_kg", "vss_ml_kg"):
        assert num(row[col]) is None, (
            f"animal 102 外推超限，字段 {col} 应为空，实际 {row[col]!r}"
        )


def test_extrapolation_animal_still_reports_half_life():
    """外推超限只影响 AUC0-inf 及其派生参数，t1/2 仍应报告（终末相回归有效）。"""
    out = output_by_id()
    thalf = num(out[102]["t_half_h"])
    assert thalf is not None, (
        "animal 102 的 t_half_h 不应为空：终末相回归有效，外推超限不影响 t1/2"
    )
    assert close(thalf, 11.1267), (
        f"animal 102 t_half_h 期望约 11.1267，实际 {thalf}"
    )


def test_iv_only_parameters():
    """CL / Vss / MRT 仅对 IV 给药组有意义；PO 组必须留空。"""
    out = output_by_id()
    golden = load_golden()
    for aid in EVALUABLE:
        route = golden[aid]["route"]
        for col in ("cl_ml_h_kg", "vss_ml_kg", "mrt_h"):
            got = num(out[aid][col])
            if route == "IV" and golden[aid][col] is not None:
                assert got is not None, (
                    f"animal {aid} 为 IV 给药，字段 {col} 不应为空"
                )
            else:
                assert got is None, (
                    f"animal {aid} route={route}，字段 {col} 应留空，实际 {got}"
                )


def test_aumc_uses_pure_linear_trapezoid():
    """AUMC0-t 必须用纯线性梯形法（题面：「两段均用 (y1+y2)/2 × dt」），
    不能沿用 AUC0-t 的「线性上升 / 对数下降」。

    这里硬编码数值、故意不读 tk_golden.json：2026-10 曾出现 build_golden.py、
    solution/solve.sh、check_traps.py 三份实现都把 t×C 喂进复合函数、
    导致 AUMC 下降段误走对数的判分偏差。当时金标准与 oracle 共享同一错误，
    两侧对照全绿却一起偏离题面，只有跑真实 agent 才暴露出来。
    这条锚点独立于金标准，专门挡住同类回归。

    101 是唯一可算 MRT 的动物（102 外推超限已留空，201/202/301 为 PO）。
    纯线性 -> MRT 3.10472 / Vss 3627.698；误用对数 -> 3.06961 / 3586.671，
    偏差 1.14%，远超 0.5% 容差。
    """
    out = output_by_id()
    mrt = num(out[101]["mrt_h"])
    vss = num(out[101]["vss_ml_kg"])
    assert mrt is not None, "animal 101 缺少 mrt_h"
    assert vss is not None, "animal 101 缺少 vss_ml_kg"
    assert close(mrt, 3.10472), (
        f"animal 101 MRT 期望约 3.10472（AUMC 走纯线性梯形），实际 {mrt}；"
        f"若约为 3.06961 说明 AUMC 下降段误用了对数梯形"
    )
    assert close(vss, 3627.698), (
        f"animal 101 Vss 期望约 3627.698（= MRT × CL），实际 {vss}；"
        f"若约为 3586.671 说明 AUMC 下降段误用了对数梯形"
    )
