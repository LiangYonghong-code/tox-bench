# -*- coding: utf-8 -*-
"""作者侧数据生成器 —— 生成 28 天重复给药毒性试验的 study.db。

本脚本**刻意放在 build-tools/**：它包含了全部设计的效应量（等价于真值的一部分），
不能进入镜像、也不能随任务包提交给被测 agent。
`environment/Dockerfile` 只 COPY study.db 与 SOP，因此本文件不会进容器。

用法：
    python build-tools/repeat-dose-noael-judgment/make_study_db.py
产出：
    tasks/repeat-dose-noael-judgment/environment/study.db
    tasks/repeat-dose-noael-judgment/tests/db.sha256   （判分器用它校验 study.db 未被修改）
"""
import hashlib
import json
import os
import sqlite3

import numpy as np

# 本脚本住在 build-tools/<题目录名>/ 下，与 tasks/<题目录名>/ 同名。
# 题目录名直接取自本文件的父目录名 —— 这样"这个生成器为哪道题服务"由
# 目录结构保证，不靠散落的字符串常量，也不依赖 eval/tasks.yaml 已经就位。
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TASK = os.path.join(REPO, "tasks", os.path.basename(HERE))
DB_PATH = os.path.join(TASK, "environment", "study.db")
SHA_PATH = os.path.join(TASK, "tests", "db.sha256")

SEED = 20260501
STUDY_NO = "TOX-2026-0187"
DOSES = [0, 50, 150, 500]
N_MAIN = 10          # 每性别每组主试验动物数
N_REC = 5            # 每性别每组恢复期动物数
REC_DOSES = [0, 500]  # 设恢复期的组

rng = np.random.default_rng(SEED)

# ---------------------------------------------------------------- 设计的效应量
# 主试验：末次体重增重相对对照的倍数（SOP 规则 2 的触发点：雌性 150 组 < 0.90）
GAIN_FACTOR = {"male": [1.00, 0.98, 0.96, 0.80],
               "female": [1.00, 0.97, 0.85, 0.72]}
# 摄食量：SOP 规则 2 要求同时满足下降 ≥5%
FOOD_FACTOR = {"male": [1.00, 0.99, 0.98, 0.88],
               "female": [1.00, 0.98, 0.90, 0.83]}
# 肝/脑重量比：150、500 组升高（500 组另伴肝细胞坏死）
LIVER_FACTOR = {"male": [1.00, 1.03, 1.20, 1.25],
                "female": [1.00, 1.02, 1.19, 1.26]}
# 红细胞：仅雄性 50 组孤立下降 5%，落在历史对照范围内
RBC_FACTOR = {"male": [1.00, 0.95, 1.00, 0.99],
              "female": [1.00, 1.00, 1.00, 1.00]}
ALT_FACTOR = {"male": [1.00, 1.03, 1.06, 3.33],
              "female": [1.00, 1.02, 1.05, 3.33]}
AST_FACTOR = {"male": [1.00, 1.02, 1.05, 2.14],
              "female": [1.00, 1.01, 1.04, 2.14]}

BASE = {
    "bw_day1": {"male": (200.0, 8.0), "female": (165.0, 7.0)},
    "gain_ctrl": {"male": (190.0, 7.0), "female": (105.0, 4.5)},
    "food": {"male": (22.0, 1.4), "female": (16.0, 1.1)},
    "alt": {"male": (45.0, 7.0), "female": (42.0, 7.0)},
    "ast": {"male": (110.0, 15.0), "female": (105.0, 14.0)},
    "rbc": {"male": (8.25, 0.20), "female": (7.95, 0.20)},
    "hgb": {"male": (148.0, 6.0), "female": (144.0, 6.0)},
    "wbc": {"male": (8.5, 2.0), "female": (7.8, 1.9)},
    "plt": {"male": (900.0, 120.0), "female": (860.0, 110.0)},
    "brain": {"male": (2.10, 0.06), "female": (1.95, 0.06)},
    "liver_brain_ratio": {"male": (5.00, 0.22), "female": (5.10, 0.22)},
    "kidney_brain_ratio": {"male": (1.05, 0.05), "female": (1.00, 0.05)},
    "spleen_brain_ratio": {"male": (0.30, 0.03), "female": (0.29, 0.03)},
    "bun": {"male": (6.5, 0.8), "female": (6.2, 0.8)},
    "crea": {"male": (42.0, 4.0), "female": (40.0, 4.0)},
    "alp": {"male": (180.0, 30.0), "female": (170.0, 28.0)},
}

MAIN_DAYS = [1, 8, 15, 22, 29]
REC_DAYS = [1, 8, 15, 22, 29, 36, 43, 50, 57]

# 组织病理学：(脏器, 发现, (发生动物数/10, 分级)) 按剂量索引
HISTO_MAIN = {
    "中央静脉周围肝细胞肥大": {
        "organ": "liver", "sex_counts": {
            "male": [(0, 0, 0), (0, 0, 0), (8, 2, 0), (7, 2, 0)],
            "female": [(0, 0, 0), (0, 0, 0), (7, 2, 0), (8, 2, 0)]}},
    "肝细胞坏死": {
        "organ": "liver", "sex_counts": {
            "male": [(0, 0, 0), (0, 0, 0), (0, 0, 0), (6, 3, 0)],
            "female": [(0, 0, 0), (0, 0, 0), (0, 0, 0), (7, 3, 0)]}},
    "肾小管嗜碱性变": {
        "organ": "kidney", "sex_counts": {
            "male": [(4, 1, 0), (3, 1, 0), (5, 1, 0), (5, 1, 0)],
            "female": [(4, 1, 0), (3, 1, 0), (5, 1, 0), (5, 1, 0)]}},
    "肾上腺皮质空泡化": {
        "organ": "adrenal", "sex_counts": {
            "male": [(0, 0, 0), (0, 0, 0), (0, 0, 0), (7, 2, 0)],
            "female": [(0, 0, 0), (0, 0, 0), (0, 0, 0), (6, 2, 0)]}},
    "胸腺萎缩": {
        "organ": "thymus", "sex_counts": {
            "male": [(0, 0, 0), (0, 0, 0), (0, 0, 0), (6, 2, 0)],
            "female": [(0, 0, 0), (0, 0, 0), (0, 0, 0), (5, 2, 0)]}},
}

# 恢复期：500 组病变明显减轻（肝细胞坏死 3 级 → 1 级，发生率 2/5）
HISTO_REC = {
    "肝细胞坏死": {"organ": "liver", "count": 2, "grade": 1},
    "中央静脉周围肝细胞肥大": {"organ": "liver", "count": 2, "grade": 1},
}

HISTORICAL = [
    # (parameter, sex, mean, sd, min, max)
    ("RBC (10^12/L)", "male", 8.22, 0.28, 7.40, 9.10),
    ("RBC (10^12/L)", "female", 7.95, 0.26, 7.10, 8.80),
    ("HGB (g/L)", "male", 148.0, 6.0, 130.0, 170.0),
    ("HGB (g/L)", "female", 144.0, 6.0, 125.0, 165.0),
    ("WBC (10^9/L)", "male", 8.5, 2.0, 4.0, 15.0),
    ("WBC (10^9/L)", "female", 7.8, 1.9, 3.5, 14.0),
    ("PLT (10^9/L)", "male", 900.0, 120.0, 600.0, 1250.0),
    ("PLT (10^9/L)", "female", 860.0, 110.0, 580.0, 1200.0),
    ("ALT (U/L)", "male", 44.0, 8.0, 20.0, 80.0),
    ("ALT (U/L)", "female", 42.0, 8.0, 20.0, 80.0),
    ("AST (U/L)", "male", 108.0, 18.0, 60.0, 200.0),
    ("AST (U/L)", "female", 104.0, 17.0, 60.0, 200.0),
    ("ALP (U/L)", "male", 180.0, 30.0, 90.0, 300.0),
    ("ALP (U/L)", "female", 170.0, 28.0, 90.0, 290.0),
    ("BUN (mmol/L)", "male", 6.5, 0.8, 4.0, 9.5),
    ("BUN (mmol/L)", "female", 6.2, 0.8, 3.8, 9.0),
    ("CREA (umol/L)", "male", 42.0, 4.0, 30.0, 58.0),
    ("CREA (umol/L)", "female", 40.0, 4.0, 28.0, 55.0),
    # 新增：体重、摄食、脏器重量比的历史对照，供 agent 推断阈值
    ("体重增重 (g)", "male", 190.0, 7.0, 170.0, 210.0),
    ("体重增重 (g)", "female", 105.0, 4.5, 92.0, 118.0),
    ("摄食量 (g/天)", "male", 22.0, 1.4, 18.0, 26.0),
    ("摄食量 (g/天)", "female", 16.0, 1.1, 13.0, 19.0),
    ("肝/脑重量比", "male", 5.00, 0.22, 4.40, 5.60),
    ("肝/脑重量比", "female", 5.10, 0.22, 4.50, 5.70),
    ("肾/脑重量比", "male", 1.05, 0.05, 0.90, 1.20),
    ("肾/脑重量比", "female", 1.00, 0.05, 0.85, 1.15),
]

# 新增：本机构历史 NOAEL 判定案例，agent 需据此推断“多大幅度的改变算有害”。
# 设计原则：边界清晰，让简单推断（取 was_adverse=0 的最大值与 was_adverse=1 的最小值的中点）
# 能还原到与原 SOP 阈值接近的数值。
HISTORICAL_NOAEL_EVENTS = [
    # event_id, compound_id, species, parameter, dose_mg_kg_day, effect_size_pct, co_change_pct,
    # histo_grade, ctrl_incidence_pct, dose_related_increase_pct, historical_max, was_adverse, was_related, notes
    (1, "CMP-A", "大鼠", "体重增重抑制", 100, -5.0, -2.0, None, None, None, None, 0, None,
     "轻度增重下降，无摄食下降，不判为有害"),
    (2, "CMP-B", "大鼠", "体重增重抑制", 200, -8.0, -4.0, None, None, None, None, 0, None,
     "中度增重下降，仍在历史变异范围，不判为有害"),
    (3, "CMP-C", "大鼠", "体重增重抑制", 300, -12.0, -6.0, None, None, None, None, 1, None,
     "明显增重抑制伴摄食下降，判为有害"),
    (4, "CMP-D", "大鼠", "体重增重抑制", 400, -18.0, -10.0, None, None, None, None, 1, None,
     "严重增重抑制，判为有害"),
    (5, "CMP-E", "大鼠", "摄食量下降", 150, -3.0, None, None, None, None, None, 0, None,
     "轻度摄食下降，不单独构成有害"),
    (6, "CMP-F", "大鼠", "摄食量下降", 250, -7.0, None, None, None, None, None, 1, None,
     "明显摄食下降，与增重抑制共同构成有害"),
    (7, "CMP-G", "大鼠", "肝细胞坏死", 500, None, None, 1, None, None, 75.0, 0, None,
     "1级肝细胞坏死，ALT未超历史上限，不判为有害"),
    (8, "CMP-H", "大鼠", "肝细胞坏死", 500, None, None, 2, None, None, 85.0, 1, None,
     "2级肝细胞坏死且ALT超历史上限，判为有害"),
    (9, "CMP-I", "大鼠", "红细胞计数下降", 100, -4.0, None, None, None, None, 9.10, 0, 0,
     "红细胞下降在历史对照范围内，无剂量反应，非受试物相关"),
    (10, "CMP-J", "大鼠", "红细胞计数下降", 200, -9.0, None, None, None, None, 8.80, 1, 1,
     "红细胞下降超历史对照上限，受试物相关"),
    (11, "CMP-K", "大鼠", "组织病理学背景病变", 0, None, None, None, 40.0, 5.0, None, 0, 0,
     "对照组发生率40%，组间差异5个百分点，判为背景病变"),
    (12, "CMP-L", "大鼠", "组织病理学背景病变", 0, None, None, None, 35.0, 10.0, None, 0, 0,
     "对照组发生率35%，组间差异10个百分点，判为背景病变"),
    (13, "CMP-M", "大鼠", "组织病理学背景病变", 200, None, None, None, 5.0, 25.0, None, 0, 1,
     "对照组发生率5%，剂量相关升高25个百分点，不判为背景病变"),
    (14, "CMP-N", "大鼠", "组织病理学背景病变", 300, None, None, None, 10.0, 20.0, None, 0, 1,
     "对照组发生率10%，剂量相关升高20个百分点，不判为背景病变"),
]


def g(key, sex):
    m, s = BASE[key][sex]
    return m + rng.normal(0, s)


def effect(key, sex, dose_idx, noise=0.03):
    table = {"gain": GAIN_FACTOR, "food": FOOD_FACTOR, "liver": LIVER_FACTOR,
             "rbc": RBC_FACTOR, "alt": ALT_FACTOR, "ast": AST_FACTOR}[key]
    f = table[sex][dose_idx]
    return f * (1.0 + rng.normal(0, noise))


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE study_info (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE groups (
        group_id INTEGER PRIMARY KEY, dose_mg_kg_day REAL NOT NULL,
        label TEXT NOT NULL, n_male INTEGER, n_female INTEGER, is_recovery INTEGER NOT NULL);
    CREATE TABLE animals (
        animal_id TEXT PRIMARY KEY, sex TEXT NOT NULL, group_id INTEGER NOT NULL,
        is_recovery INTEGER NOT NULL);
    CREATE TABLE body_weight (animal_id TEXT, day INTEGER, weight_g REAL);
    CREATE TABLE food_intake (animal_id TEXT, week INTEGER, g_per_day REAL);
    CREATE TABLE organ_weights (animal_id TEXT, organ TEXT, weight_g REAL);
    CREATE TABLE hematology (
        animal_id TEXT, rbc_10e12_l REAL, hgb_g_l REAL, wbc_10e9_l REAL, plt_10e9_l REAL);
    CREATE TABLE biochemistry (
        animal_id TEXT, alt_u_l REAL, ast_u_l REAL, alp_u_l REAL,
        bun_mmol_l REAL, crea_umol_l REAL);
    CREATE TABLE histopathology (
        animal_id TEXT, organ TEXT, finding TEXT, grade INTEGER);
    CREATE TABLE historical_control (
        parameter TEXT, sex TEXT, mean REAL, sd REAL, min REAL, max REAL);
    CREATE TABLE historical_noael_events (
        event_id INTEGER PRIMARY KEY,
        compound_id TEXT,
        species TEXT,
        parameter TEXT,
        dose_mg_kg_day REAL,
        effect_size_pct REAL,
        co_change_pct REAL,
        histo_grade INTEGER,
        ctrl_incidence_pct REAL,
        dose_related_increase_pct REAL,
        historical_max REAL,
        was_adverse INTEGER,
        was_related INTEGER,
        notes TEXT
    );
    """)

    info = {
        "study_no": STUDY_NO,
        "test_article": "化合物 X-27",
        "species": "大鼠",
        "strain": "SD",
        "route": "灌胃",
        "dosing_duration_days": "28",
        "recovery_duration_days": "28",
        "glp": "是",
        "necropsy_days_main": "29",
        "necropsy_days_recovery": "57",
    }
    cur.executemany("INSERT INTO study_info VALUES (?,?)", list(info.items()))
    cur.executemany("INSERT INTO historical_control VALUES (?,?,?,?,?,?)", HISTORICAL)
    cur.executemany(
        "INSERT INTO historical_noael_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        HISTORICAL_NOAEL_EVENTS)

    for i, dose in enumerate(DOSES):
        cur.execute("INSERT INTO groups VALUES (?,?,?,?,?,?)",
                    (i + 1, dose, f"G{i + 1} {dose} mg/kg/day", N_MAIN, N_MAIN, 0))
    for j, dose in enumerate(REC_DOSES):
        cur.execute("INSERT INTO groups VALUES (?,?,?,?,?,?)",
                    (10 + j, dose, f"R{j + 1} {dose} mg/kg/day (recovery)", N_REC, N_REC, 1))

    histo_rows = []
    animals = []

    # ---------------------------------------------------------- 主试验动物
    for sex in ("male", "female"):
        idx = 0
        for di, dose in enumerate(DOSES):
            for k in range(N_MAIN):
                idx += 1
                aid = f"{'M' if sex == 'male' else 'F'}{di + 1}{k + 1:02d}"
                animals.append((aid, sex, di + 1, 0))
                base_bw = g("bw_day1", sex)
                ctrl_gain = g("gain_ctrl", sex)
                total_gain = ctrl_gain * effect("gain", sex, di, 0.015)
                # 周体重：线性增长 + 轻微噪声
                for step, day in enumerate(MAIN_DAYS):
                    frac = step / (len(MAIN_DAYS) - 1)
                    w = base_bw + total_gain * frac + rng.normal(0, 1.2)
                    cur.execute("INSERT INTO body_weight VALUES (?,?,?)", (aid, day, round(w, 1)))
                for wk in range(1, 5):
                    fi = g("food", sex) * effect("food", sex, di)
                    cur.execute("INSERT INTO food_intake VALUES (?,?,?)", (aid, wk, round(fi, 2)))
                brain = g("brain", sex)
                liver = brain * g("liver_brain_ratio", sex) * effect("liver", sex, di, 0.05)
                kidney = brain * g("kidney_brain_ratio", sex) * (1 + rng.normal(0, 0.04))
                spleen = brain * g("spleen_brain_ratio", sex) * (1 + rng.normal(0, 0.06))
                for organ, val in (("brain", brain), ("liver", liver),
                                   ("kidney", kidney), ("spleen", spleen)):
                    cur.execute("INSERT INTO organ_weights VALUES (?,?,?)",
                                (aid, organ, round(val, 3)))
                cur.execute("INSERT INTO hematology VALUES (?,?,?,?,?)", (
                    aid,
                    round(g("rbc", sex) * effect("rbc", sex, di), 2),
                    round(g("hgb", sex) * effect("rbc", sex, di), 1),
                    round(g("wbc", sex), 2),
                    round(g("plt", sex), 0)))
                cur.execute("INSERT INTO biochemistry VALUES (?,?,?,?,?,?)", (
                    aid,
                    round(g("alt", sex) * effect("alt", sex, di), 1),
                    round(g("ast", sex) * effect("ast", sex, di), 1),
                    round(g("alp", sex), 1),
                    round(g("bun", sex), 2),
                    round(g("crea", sex), 1)))
                # 组织病理学
                for finding, spec in HISTO_MAIN.items():
                    count, grade, _ = spec["sex_counts"][sex][di]
                    if k < count:
                        histo_rows.append((aid, spec["organ"], finding, grade))

    # ---------------------------------------------------------- 恢复期动物
    for sex in ("male", "female"):
        for ri, dose in enumerate(REC_DOSES):
            di = DOSES.index(dose)
            for k in range(N_REC):
                aid = f"{'M' if sex == 'male' else 'F'}R{ri + 1}{k + 1:02d}"
                animals.append((aid, sex, 10 + ri, 1))
                base_bw = g("bw_day1", sex)
                ctrl_gain = g("gain_ctrl", sex)
                w29 = base_bw + ctrl_gain * (1.0 if dose == 0 else 0.72)
                w57 = w29 + ctrl_gain * (1.00 if dose == 0 else 1.10)
                for step, day in enumerate(REC_DAYS):
                    frac = step / (len(REC_DAYS) - 1)
                    w = base_bw + (w57 - base_bw) * frac + rng.normal(0, 1.2)
                    cur.execute("INSERT INTO body_weight VALUES (?,?,?)", (aid, day, round(w, 1)))
                for wk in range(1, 9):
                    fi = g("food", sex) * (1.0 if dose == 0 else 0.97)
                    cur.execute("INSERT INTO food_intake VALUES (?,?,?)", (aid, wk, round(fi, 2)))
                brain = g("brain", sex)
                liver = brain * g("liver_brain_ratio", sex) * (
                    1.00 if dose == 0 else 1.06) * (1 + rng.normal(0, 0.05))
                kidney = brain * g("kidney_brain_ratio", sex) * (1 + rng.normal(0, 0.04))
                spleen = brain * g("spleen_brain_ratio", sex) * (1 + rng.normal(0, 0.06))
                for organ, val in (("brain", brain), ("liver", liver),
                                   ("kidney", kidney), ("spleen", spleen)):
                    cur.execute("INSERT INTO organ_weights VALUES (?,?,?)",
                                (aid, organ, round(val, 3)))
                cur.execute("INSERT INTO hematology VALUES (?,?,?,?,?)", (
                    aid, round(g("rbc", sex), 2), round(g("hgb", sex), 1),
                    round(g("wbc", sex), 2), round(g("plt", sex), 0)))
                cur.execute("INSERT INTO biochemistry VALUES (?,?,?,?,?,?)", (
                    aid,
                    round(g("alt", sex) * (1.00 if dose == 0 else 1.10), 1),
                    round(g("ast", sex) * (1.00 if dose == 0 else 1.08), 1),
                    round(g("alp", sex), 1), round(g("bun", sex), 2),
                    round(g("crea", sex), 1)))
                if dose != 0:
                    for finding, spec in HISTO_REC.items():
                        if k < spec["count"]:
                            histo_rows.append((aid, spec["organ"], finding, spec["grade"]))

    cur.executemany("INSERT INTO animals VALUES (?,?,?,?)", animals)
    cur.executemany("INSERT INTO histopathology VALUES (?,?,?,?)", histo_rows)
    con.commit()

    # ---------------------------------------------------------------- 自检
    def mean_gain(sex, di, rec=False):
        vals = []
        for aid, s, gid, isrec in animals:
            if s != sex or isrec != int(rec):
                continue
            if (gid - 1) != di:
                continue
            rows = dict(cur.execute(
                "SELECT day, weight_g FROM body_weight WHERE animal_id=?", (aid,)).fetchall())
            vals.append(rows[57 if rec else 29] - rows[1])
        return float(np.mean(vals))

    def rel(key, sex, di, rec=False):
        return mean_gain(sex, di, rec) / mean_gain(sex, 0, rec)

    report = {"study_no": STUDY_NO, "gain_ratio": {}, "food_ratio": {}, "liver_ratio": {},
              "alt_mean": {}, "rbc_mean": {}, "histo_incidence": {}}
    for sex in ("male", "female"):
        report["gain_ratio"][sex] = {str(DOSES[i]): round(rel("gain", sex, i), 3) for i in range(4)}
        fr = {}
        for i, dose in enumerate(DOSES):
            gid = i + 1
            v = [x[0] for x in cur.execute(
                "SELECT AVG(f.g_per_day) FROM food_intake f JOIN animals a USING(animal_id) "
                "WHERE a.sex=? AND a.group_id=? AND a.is_recovery=0 GROUP BY a.animal_id", (sex, gid))]
            ctrl = [x[0] for x in cur.execute(
                "SELECT AVG(f.g_per_day) FROM food_intake f JOIN animals a USING(animal_id) "
                "WHERE a.sex=? AND a.group_id=1 AND a.is_recovery=0 GROUP BY a.animal_id", (sex,))]
            fr[str(dose)] = round(float(np.mean(v)) / float(np.mean(ctrl)), 3)
        report["food_ratio"][sex] = fr
        lr = {}
        for i, dose in enumerate(DOSES):
            v = [x[0] for x in cur.execute(
                "SELECT o1.weight_g/o2.weight_g FROM organ_weights o1 "
                "JOIN organ_weights o2 ON o1.animal_id=o2.animal_id AND o2.organ='brain' "
                "JOIN animals a ON a.animal_id=o1.animal_id "
                "WHERE o1.organ='liver' AND a.sex=? AND a.group_id=? AND a.is_recovery=0", (sex, i + 1))]
            ctrl = [x[0] for x in cur.execute(
                "SELECT o1.weight_g/o2.weight_g FROM organ_weights o1 "
                "JOIN organ_weights o2 ON o1.animal_id=o2.animal_id AND o2.organ='brain' "
                "JOIN animals a ON a.animal_id=o1.animal_id "
                "WHERE o1.organ='liver' AND a.sex=? AND a.group_id=1 AND a.is_recovery=0", (sex,))]
            lr[str(dose)] = round(float(np.mean(v)) / float(np.mean(ctrl)), 3)
        report["liver_ratio"][sex] = lr
        report["alt_mean"][sex] = {str(DOSES[i]): round(float(np.mean([x[0] for x in cur.execute(
            "SELECT b.alt_u_l FROM biochemistry b JOIN animals a USING(animal_id) "
            "WHERE a.sex=? AND a.group_id=? AND a.is_recovery=0", (sex, i + 1))])), 1)
            for i in range(4)}
        report["rbc_mean"][sex] = {str(DOSES[i]): round(float(np.mean([x[0] for x in cur.execute(
            "SELECT h.rbc_10e12_l FROM hematology h JOIN animals a USING(animal_id) "
            "WHERE a.sex=? AND a.group_id=? AND a.is_recovery=0", (sex, i + 1))])), 2)
            for i in range(4)}
    for finding in HISTO_MAIN:
        rows = cur.execute(
            "SELECT a.sex, a.group_id, COUNT(*) FROM histopathology p "
            "JOIN animals a USING(animal_id) WHERE p.finding=? AND a.is_recovery=0 "
            "GROUP BY a.sex, a.group_id", (finding,)).fetchall()
        report["histo_incidence"][finding] = {f"{s}-G{gid}": n for s, gid, n in rows}

    con.close()
    print(json.dumps(report, ensure_ascii=True, indent=1))

    # 关键设计点的硬校验：若这些断言失败，说明数据没能复现 SOP 的触发条件
    assert 0.81 <= report["gain_ratio"]["female"]["150"] <= 0.89, "female G3 gain should drop ~15%"
    assert report["gain_ratio"]["male"]["150"] > 0.92, "male G3 gain drop should stay <10%"
    assert report["gain_ratio"]["male"]["500"] < 0.88 and report["gain_ratio"]["female"]["500"] < 0.80, "G4 gain drop should be clearly adverse"
    assert report["food_ratio"]["female"]["150"] <= 0.95, "female G3 food intake should drop >=5%"
    assert report["food_ratio"]["male"]["150"] > 0.95, "male G3 food intake should stay >95%"
    assert report["alt_mean"]["male"]["500"] > 80.0, "500 组 ALT 应超历史对照上限"
    assert report["alt_mean"]["male"]["150"] < 80.0, "150 组 ALT 应在历史对照范围内"
    assert 7.40 <= report["rbc_mean"]["male"]["50"] <= 9.10, "50 组雄性 RBC 应落在历史对照范围内"

    sha = hashlib.sha256(open(DB_PATH, "rb").read()).hexdigest()
    os.makedirs(os.path.dirname(SHA_PATH), exist_ok=True)
    with open(SHA_PATH, "w", encoding="utf-8") as f:
        f.write(sha + "\n")
    print("DB:", DB_PATH)
    print("SHA256:", sha)


if __name__ == "__main__":
    main()
