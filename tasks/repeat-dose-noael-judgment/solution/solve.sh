#!/bin/bash
# 参考解（oracle）：把 SOP-TOX-014 的判定规则实现在数据上。
# 所有数值均由 study.db 计算得出，没有任何硬编码的剂量结论。
set -euo pipefail

APP_DIR="${APP_DIR:-/app}"
export APP_DIR

cat > /tmp/_solve.py <<'PY'
# -*- coding: utf-8 -*-
"""参考解：依据 /app/SOP_toxicity_criteria.md 对 /app/study.db 做 NOAEL 判定。"""
import json
import math
import os
import sqlite3

import pandas as pd

APP = os.environ.get("APP_DIR", "/app")
DB = os.path.join(APP, "study.db")
con = sqlite3.connect(DB)

info = dict(pd.read_sql("SELECT key, value FROM study_info", con).values)
groups = pd.read_sql("SELECT * FROM groups", con)
animals = pd.read_sql("SELECT * FROM animals", con)
bw = pd.read_sql("SELECT * FROM body_weight", con)
food = pd.read_sql("SELECT * FROM food_intake", con)
organs = pd.read_sql("SELECT * FROM organ_weights", con)
hema = pd.read_sql("SELECT * FROM hematology", con)
biochem = pd.read_sql("SELECT * FROM biochemistry", con)
histo = pd.read_sql("SELECT * FROM histopathology", con)
hist_ref = pd.read_sql("SELECT * FROM historical_control", con)
events = pd.read_sql("SELECT * FROM historical_noael_events", con)

SEXES = ["male", "female"]
main_dose = {gid: int(dose) for gid, dose in zip(
    groups.loc[groups.is_recovery == 0, "group_id"],
    groups.loc[groups.is_recovery == 0, "dose_mg_kg_day"])}
rec_dose = {gid: int(dose) for gid, dose in zip(
    groups.loc[groups.is_recovery == 1, "group_id"],
    groups.loc[groups.is_recovery == 1, "dose_mg_kg_day"])}
LEVELS = sorted(set(main_dose.values()))
CTRL = min(LEVELS)
NECROPSY_MAIN = int(info["necropsy_days_main"])
NECROPSY_REC = int(info["necropsy_days_recovery"])

meta = animals.merge(groups[["group_id", "dose_mg_kg_day"]], on="group_id")
main = meta[meta.is_recovery == 0].copy()
rec = meta[meta.is_recovery == 1].copy()


def welch_p(a, b):
    """Welch t 检验的 p 值（正态近似，样本量小时偏保守）。"""
    a, b = list(a), list(b)
    if len(a) < 2 or len(b) < 2:
        return 1.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return 0.0 if ma != mb else 1.0
    t = abs(ma - mb) / se
    return math.erfc(t / math.sqrt(2))


def by_sex_dose(frame, value_col, animal_col="animal_id"):
    """{sex: {dose: [values]}}"""
    out = {s: {d: [] for d in LEVELS} for s in SEXES}
    for _, row in frame.iterrows():
        out[row["sex"]][row["dose_mg_kg_day"]].append(row[value_col])
    return out


def ratio(vals, ctrl_vals):
    return (sum(vals) / len(vals)) / (sum(ctrl_vals) / len(ctrl_vals))


def hist_range(parameter, sex):
    row = hist_ref[(hist_ref.parameter == parameter) & (hist_ref.sex == sex)].iloc[0]
    return float(row["min"]), float(row["max"])


def hist_mean_sd(parameter, sex):
    row = hist_ref[(hist_ref.parameter == parameter) & (hist_ref.sex == sex)].iloc[0]
    return float(row["mean"]), float(row["sd"])


def infer_threshold(events_df, parameter, value_col, flag_col, default, abs_value=True):
    """从 historical_noael_events 推断阈值。
    取 flag_col=0 的最大 value_col 与 flag_col=1 的最小 value_col，以中点作为阈值。
    """
    sub = events_df[events_df.parameter == parameter]
    if sub.empty:
        return default
    non_vals = sub[sub[flag_col] == 0][value_col].dropna()
    adv_vals = sub[sub[flag_col] == 1][value_col].dropna()
    if non_vals.empty or adv_vals.empty:
        return default
    if abs_value:
        non_max = abs(non_vals).max()
        adv_min = abs(adv_vals).min()
    else:
        non_max = non_vals.max()
        adv_min = adv_vals.min()
    return (non_max + adv_min) / 2.0


def infer_background_thresholds(events_df):
    """推断背景病变阈值：对照组发生率阈值 + 剂量相关升高阈值。"""
    sub = events_df[events_df.parameter == "组织病理学背景病变"]
    if sub.empty:
        return 0.30, 0.15
    non_rel = sub[sub.was_related == 0]
    rel = sub[sub.was_related == 1]
    if non_rel.empty or rel.empty:
        return 0.30, 0.15
    ctrl_thr = (non_rel.ctrl_incidence_pct.min() + rel.ctrl_incidence_pct.max()) / 2.0 / 100.0
    diff_thr = (non_rel.dose_related_increase_pct.max() + rel.dose_related_increase_pct.min()) / 2.0 / 100.0
    return ctrl_thr, diff_thr


# ---------------------------------------------------------------- 从历史数据推断阈值
# effect_size_pct 等字段以百分比存储，需除以 100 转为小数
BW_ADV_THRESHOLD = infer_threshold(events, "体重增重抑制", "effect_size_pct", "was_adverse", 0.10) / 100.0
FOOD_ADV_THRESHOLD = infer_threshold(events, "摄食量下降", "effect_size_pct", "was_adverse", 0.05) / 100.0
NEC_GRADE_THRESHOLD = infer_threshold(events, "肝细胞坏死", "histo_grade", "was_adverse", 1.5, abs_value=False)
BG_CTRL_THRESHOLD, BG_DIFF_THRESHOLD = infer_background_thresholds(events)

# 组织病理学显著程度用等级阈值的上取整
NEC_GRADE_CUTOFF = int(math.ceil(NEC_GRADE_THRESHOLD))

# ---------------------------------------------------------------- 体重增重与摄食
w = bw.merge(meta[["animal_id", "sex", "dose_mg_kg_day", "is_recovery"]], on="animal_id")
w1 = w[w.day == 1].set_index("animal_id")["weight_g"]
for col, day in (("w_end", NECROPSY_MAIN), ("w_rec", NECROPSY_REC)):
    idx = w[w.day == day].set_index("animal_id")["weight_g"]
    w = w.assign(**{col: w.animal_id.map(idx)})
gain = w[w.day == 1][["animal_id", "sex", "dose_mg_kg_day", "is_recovery",
                      "w_end", "w_rec"]].copy()
gain["gain_main"] = gain.w_end - gain.animal_id.map(w1)
gain["gain_rec"] = gain.w_rec - gain.animal_id.map(w1)

gain_main = by_sex_dose(gain[gain.is_recovery == 0], "gain_main")
gain_rec = by_sex_dose(gain[gain.is_recovery == 1], "gain_rec")

food_mean = (food.groupby("animal_id", as_index=False)["g_per_day"].mean()
             .merge(meta[["animal_id", "sex", "dose_mg_kg_day", "is_recovery"]], on="animal_id"))
food_main = by_sex_dose(food_mean[food_mean.is_recovery == 0], "g_per_day")

gain_ratio = {s: {d: ratio(gain_main[s][d], gain_main[s][CTRL]) for d in LEVELS} for s in SEXES}
food_ratio = {s: {d: ratio(food_main[s][d], food_main[s][CTRL]) for d in LEVELS} for s in SEXES}

# SOP 2.2：增重抑制超过阈值且摄食量下降超过阈值才判为有害
bw_adverse = {s: {d: (gain_ratio[s][d] <= (1.0 - BW_ADV_THRESHOLD)
                      and food_ratio[s][d] <= (1.0 - FOOD_ADV_THRESHOLD))
                  for d in LEVELS} for s in SEXES}

# ---------------------------------------------------------------- 肝脏
liver = organs[organs.organ == "liver"][["animal_id", "weight_g"]].rename(
    columns={"weight_g": "liver_g"})
brain = organs[organs.organ == "brain"][["animal_id", "weight_g"]].rename(
    columns={"weight_g": "brain_g"})
organ_ratio = (liver.merge(brain, on="animal_id")
               .assign(lb_ratio=lambda df: df.liver_g / df.brain_g)
               .merge(meta[["animal_id", "sex", "dose_mg_kg_day", "is_recovery"]], on="animal_id"))
lb_main = by_sex_dose(organ_ratio[organ_ratio.is_recovery == 0], "lb_ratio")
lb_ratio = {s: {d: ratio(lb_main[s][d], lb_main[s][CTRL]) for d in LEVELS} for s in SEXES}

alt = by_sex_dose(biochem.merge(meta[["animal_id", "sex", "dose_mg_kg_day", "is_recovery"]],
                                on="animal_id").query("is_recovery == 0"), "alt_u_l")
ast = by_sex_dose(biochem.merge(meta[["animal_id", "sex", "dose_mg_kg_day", "is_recovery"]],
                                on="animal_id").query("is_recovery == 0"), "ast_u_l")
alt_mean = {s: {d: sum(alt[s][d]) / len(alt[s][d]) for d in LEVELS} for s in SEXES}
ast_mean = {s: {d: sum(ast[s][d]) / len(ast[s][d]) for d in LEVELS} for s in SEXES}


def above_hist(sex, d, key):
    lo, hi = hist_range(key, sex)
    v = alt_mean[sex][d] if key.startswith("ALT") else ast_mean[sex][d]
    return v > hi


# ---------------------------------------------------------------- 组织病理学
hp = histo.merge(meta[["animal_id", "sex", "dose_mg_kg_day", "is_recovery"]], on="animal_id")
n_main = {(s, d): int(((main.sex == s) & (main.dose_mg_kg_day == d)).sum()) for s in SEXES for d in LEVELS}
n_rec = {(s, d): int(((rec.sex == s) & (rec.dose_mg_kg_day == d)).sum()) for s in SEXES for d in LEVELS}


def incidence(finding, sex, dose, recovery=False):
    sub = hp[(hp.finding == finding) & (hp.sex == sex) & (hp.dose_mg_kg_day == dose)
             & (hp.is_recovery == (1 if recovery else 0))]
    return len(sub)


def max_grade(finding, sex, dose, recovery=False):
    sub = hp[(hp.finding == finding) & (hp.sex == sex) & (hp.dose_mg_kg_day == dose)
             & (hp.is_recovery == (1 if recovery else 0))]
    return int(sub.grade.max()) if len(sub) else 0


F_HYPERTROPHY = "中央静脉周围肝细胞肥大"
F_NECROSIS = "肝细胞坏死"
F_BASOPHILIA = "肾小管嗜碱性变"
F_ADRENAL = "肾上腺皮质空泡化"
F_THYMUS = "胸腺萎缩"

findings = []


def add(finding, dose, sexes, related, adverse, reversible=None):
    sex = "both" if set(sexes) == {"male", "female"} else list(sexes)[0]
    findings.append({"finding": finding, "dose_mg_kg_day": int(dose), "sex": sex,
                     "related": bool(related), "adverse": bool(adverse),
                     "reversible": reversible})


# ---- 肝细胞肥大：伴肝重升高但 ALT/AST 在历史范围内 → 适应性，相关但非有害（SOP 3.1）
# 逐剂量判定：高剂量组出现坏死（ALT/AST 升高）不影响低剂量组肥大仍属适应性改变。
hyp_doses = {}
for s in SEXES:
    candidates = [d for d in LEVELS if d != CTRL
                  and incidence(F_HYPERTROPHY, s, d) > 0
                  and not above_hist(s, d, "ALT (U/L)")
                  and not above_hist(s, d, "AST (U/L)")]
    if candidates:
        hyp_doses[s] = min(candidates)
if set(hyp_doses) == {"male", "female"} and len(set(hyp_doses.values())) == 1:
    add(F_HYPERTROPHY, hyp_doses["male"], SEXES, True, False)
else:
    for s, d in hyp_doses.items():
        add(F_HYPERTROPHY, d, [s], True, False)

# ---- 肝细胞坏死：显著组织病理学改变且 ALT 或 AST 超历史上限 → 有害（SOP 3.2）
nec_first = {}
for s in SEXES:
    for d in LEVELS:
        if d == CTRL:
            continue
        if (max_grade(F_NECROSIS, s, d) >= NEC_GRADE_CUTOFF
                and (above_hist(s, d, "ALT (U/L)") or above_hist(s, d, "AST (U/L)"))):
            nec_first[s] = d
            break
nec_sexes = sorted(nec_first, key=lambda s: nec_first[s])
if nec_first:
    top = nec_first[nec_sexes[0]]
    group = [s for s in SEXES if nec_first.get(s) == top]
    # 可逆性（SOP 7.2）：恢复期分级下降 >=1 或发生率下降 >=50%
    rev = False
    for s in group:
        g0, g1 = max_grade(F_NECROSIS, s, top), max_grade(F_NECROSIS, s, top, recovery=True)
        i0, i1 = incidence(F_NECROSIS, s, top), incidence(F_NECROSIS, s, top, recovery=True)
        r0 = i0 / max(n_main[(s, top)], 1)
        r1 = i1 / max(n_rec[(s, top)], 1)
        if (g0 - g1) >= 1 or (r0 > 0 and (r0 - r1) / r0 >= 0.5):
            rev = True
    add(F_NECROSIS, top, group, True, True, rev)

# ---- 体重增重抑制：按性别各自的首次有害剂量（SOP 2.2）
bw_first = {s: next((d for d in LEVELS if d != CTRL and bw_adverse[s][d]), None) for s in SEXES}
recovered = {}
rec_ratio = {s: (ratio(gain_rec[s][max(LEVELS)], gain_rec[s][CTRL])
                 if gain_rec[s][max(LEVELS)] and gain_rec[s][CTRL] else 0.0) for s in SEXES}
for s in SEXES:
    recovered[s] = rec_ratio[s] >= 0.90
bw_sexes = [s for s in SEXES if bw_first[s] is not None]
if bw_sexes:
    grouped = {}
    for s in bw_sexes:
        grouped.setdefault(bw_first[s], []).append(s)
    for d in sorted(grouped):
        sexes = grouped[d]
        add("体重增重抑制", d, sexes, True, True, recovered[sexes[0]] if d == max(LEVELS) else None)

# ---- 血液学：孤立、无剂量-反应且落在历史对照范围内 → 非受试物相关（SOP 1.2 / 4.1）
hema_meta = hema.merge(meta[["animal_id", "sex", "dose_mg_kg_day", "is_recovery"]], on="animal_id")
HEMA_PARAMS = [("rbc_10e12_l", "RBC (10^12/L)", "红细胞计数下降"),
               ("hgb_g_l", "HGB (g/L)", "血红蛋白下降"),
               ("wbc_10e9_l", "WBC (10^9/L)", "白细胞计数变化"),
               ("plt_10e9_l", "PLT (10^9/L)", "血小板计数变化")]
for col, label, finding_name in HEMA_PARAMS:
    val = by_sex_dose(hema_meta[hema_meta.is_recovery == 0], col)
    for s in SEXES:
        flagged = []
        for d in LEVELS:
            if d == CTRL:
                continue
            p = welch_p(val[s][d], val[s][CTRL])
            r = ratio(val[s][d], val[s][CTRL])
            if p < 0.05 and abs(r - 1) >= 0.03:
                flagged.append((d, r))
        if not flagged:
            continue
        lo, hi = hist_range(label, s)
        in_range = all(lo <= sum(val[s][d]) / len(val[s][d]) <= hi for d, _ in flagged)
        monotonic = len(flagged) > 1 and all(
            (flagged[i + 1][1] - flagged[i][1]) * (flagged[0][1] - 1) > 0
            for i in range(len(flagged) - 1))
        if in_range and not monotonic:      # SOP 1.2：非受试物相关
            add(finding_name, min(d for d, _ in flagged), [s], False, False)

# ---- 背景病变（SOP 5.1）：对照组常见且各剂量组无明确剂量相关升高
for finding in sorted(hp.finding.unique()):
    bg_sexes = []
    for s in SEXES:
        ctrl_inc = incidence(finding, s, CTRL) / max(n_main[(s, CTRL)], 1)
        if ctrl_inc < BG_CTRL_THRESHOLD:
            continue
        max_diff = max(abs(incidence(finding, s, d) / max(n_main[(s, d)], 1) - ctrl_inc)
                       for d in LEVELS if d != CTRL)
        if max_diff < BG_DIFF_THRESHOLD:
            bg_sexes.append(s)
    if bg_sexes:
        add(finding, 0, bg_sexes, False, False)

# ---- 应激性继发改变（SOP 6.1）：肾上腺皮质空泡化 / 胸腺萎缩，仅见于增重抑制有害的剂量组
for finding in (F_ADRENAL, F_THYMUS):
    stress = {}
    for s in SEXES:
        for d in LEVELS:
            if d != CTRL and incidence(finding, s, d) > 0 and bw_adverse[s][d]:
                stress[s] = d
                break
    if set(stress) == {"male", "female"} and len(set(stress.values())) == 1:
        add(finding, stress["male"], SEXES, True, False)
    else:
        for s, d in stress.items():
            add(finding, d, [s], True, False)

# ---------------------------------------------------------------- NOAEL
adverse_by_sex = {s: sorted({f["dose_mg_kg_day"] for f in findings
                             if f["adverse"] and (f["sex"] == "both" or f["sex"] == s)})
                  for s in SEXES}
noael_sex = {}
for s in SEXES:
    if adverse_by_sex[s]:
        lower = [d for d in LEVELS if d < adverse_by_sex[s][0]]
        noael_sex[s] = max(lower) if lower else 0
    else:
        noael_sex[s] = max(LEVELS)
overall = min(noael_sex.values())
determining = "female" if noael_sex["female"] <= noael_sex["male"] else "male"

targets = sorted({o for f in findings if f["adverse"]
                  for o in (["liver"] if "肝" in f["finding"] else
                            ["kidney"] if "肾小管" in f["finding"] else
                            ["adrenal"] if "肾上腺" in f["finding"] else
                            ["thymus"] if "胸腺" in f["finding"] else [])})

judgment = {
    "study_no": info["study_no"],
    "noael_mg_kg_day": overall,
    "noael_by_sex": {"male": noael_sex["male"], "female": noael_sex["female"]},
    "noael_determining_sex": determining,
    "target_organs": targets,
    "findings": sorted(findings, key=lambda f: (f["dose_mg_kg_day"], f["finding"], f["sex"])),
}
with open(os.path.join(APP, "judgment.json"), "w", encoding="utf-8") as fh:
    json.dump(judgment, fh, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------- 报告
def pct(x):
    return f"{(x - 1) * 100:+.1f}%"


lines = [
    f"# NOAEL 判定说明（{info['study_no']}）",
    "",
    "## 1. 结论",
    "",
    f"- 整体 NOAEL：**{overall} mg/kg/day**（由**{'雌性' if determining == 'female' else '雄性'}**决定）",
    f"- 雄性 NOAEL：{noael_sex['male']} mg/kg/day；雌性 NOAEL：{noael_sex['female']} mg/kg/day",
    f"- 靶器官：{'、'.join(targets) if targets else '无'}",
    "",
    "## 2. 支持 NOAEL 的关键数值证据",
    "",
    "### 2.1 体重增重与摄食（决定整体 NOAEL）",
    "",
    "| 性别 | 剂量 (mg/kg/day) | 增重相对对照 | 摄食量相对对照 | 判定 |",
    "|---|---|---|---|---|",
]
for s in SEXES:
    for d in LEVELS:
        if d == CTRL:
            continue
        verdict = "有害" if bw_adverse[s][d] else "非有害"
        lines.append(f"| {'雄性' if s == 'male' else '雌性'} | {d} | {pct(gain_ratio[s][d])} "
                     f"| {pct(food_ratio[s][d])} | {verdict} |")
lines += [
    "",
    f"依据 `historical_noael_events` 推断：增重抑制阈值约为 {BW_ADV_THRESHOLD*100:.0f}%，"
    f"摄食下降阈值约为 {FOOD_ADV_THRESHOLD*100:.0f}%，两者同时满足才判为有害。"
    f"雌性 {bw_first['female']} mg/kg/day 组同时满足，故雌性 NOAEL 降至 "
    f"{noael_sex['female']} mg/kg/day；"
    f"雄性最低有害剂量为 {bw_first['male']} mg/kg/day，雄性 NOAEL 为 {noael_sex['male']} mg/kg/day。",
    "",
    "### 2.2 肝脏（靶器官）",
    "",
    "| 性别 | 剂量 | 肝/脑重量比相对对照 | ALT (U/L) | AST (U/L) | 组织病理学 |",
    "|---|---|---|---|---|---|",
]
for s in SEXES:
    for d in LEVELS:
        if d == CTRL:
            continue
        h = incidence(F_HYPERTROPHY, s, d)
        n = incidence(F_NECROSIS, s, d)
        path = []
        if h:
            path.append(f"肝细胞肥大 {h}/{n_main[(s, d)]}（{max_grade(F_HYPERTROPHY, s, d)} 级）")
        if n:
            path.append(f"肝细胞坏死 {n}/{n_main[(s, d)]}（{max_grade(F_NECROSIS, s, d)} 级）")
        lines.append(f"| {'雄性' if s == 'male' else '雌性'} | {d} | {pct(lb_ratio[s][d])} "
                     f"| {alt_mean[s][d]:.1f} | {ast_mean[s][d]:.1f} | {'；'.join(path) or '未见'} |")
lo_alt, hi_alt = hist_range("ALT (U/L)", "male")
lo_ast, hi_ast = hist_range("AST (U/L)", "male")
lines += [
    "",
    f"ALT 历史对照范围 {lo_alt:.0f}–{hi_alt:.0f} U/L，AST {lo_ast:.0f}–{hi_ast:.0f} U/L。"
    f"{hyp_doses.get('male', '-')} mg/kg/day 组肝细胞肥大伴肝/脑重量比升高，但 ALT/AST 仍在历史"
    "范围内，按 SOP 3.1 判为**适应性反应（相关但非有害）**；"
    f"{nec_first.get('male', '-')} mg/kg/day 组出现肝细胞坏死（≥{NEC_GRADE_CUTOFF} 级）"
    f"且 ALT/AST 超出历史上限，按 SOP 3.2 判为**有害**。",
    "",
    "### 2.3 可逆性",
    "",
]
if nec_first:
    d = max(nec_first.values())
    for s in SEXES:
        if s in nec_first:
            lines.append(
                f"- 肝细胞坏死（{d} mg/kg/day，{'雄性' if s == 'male' else '雌性'}）：给药期末 "
                f"{max_grade(F_NECROSIS, s, d)} 级 {incidence(F_NECROSIS, s, d)}/{n_main[(s, d)]} → "
                f"恢复期末 {max_grade(F_NECROSIS, s, d, True)} 级 "
                f"{incidence(F_NECROSIS, s, d, True)}/{n_rec[(s, d)]}，明显减轻，判为可逆；"
                "按 SOP 7.1，可逆性不改变给药期末的有害性判定。")
lines += [
    f"- 体重增重（{max(LEVELS)} mg/kg/day）：恢复期末增重恢复至对照的 "
    f"{rec_ratio['male'] * 100:.1f}%（雄性）/{rec_ratio['female'] * 100:.1f}%（雌性），判为可逆。",
    "",
    "## 3. 判定为相关但非有害的改变",
    "",
    f"- 肝细胞肥大（{hyp_doses.get('male', '-')} mg/kg/day）：肝/脑重量比升高但无肝细胞损伤标志物升高，"
    "属适应性反应（SOP 3.1）。",
    f"- 肾上腺皮质空泡化 / 胸腺萎缩（{max(LEVELS)} mg/kg/day）：仅见于出现有害增重抑制的动物，"
    "判为应激性继发改变（SOP 6.1），不作为独立毒性终点。",
    "",
    "## 4. 判定为非受试物相关或背景病变的改变",
    "",
]
rbc_main = by_sex_dose(hema_meta[hema_meta.is_recovery == 0], "rbc_10e12_l")
for s in SEXES:
    lo, hi = hist_range("RBC (10^12/L)", s)
    for d in LEVELS:
        if d == CTRL:
            continue
        p = welch_p(rbc_main[s][d], rbc_main[s][CTRL])
        r = ratio(rbc_main[s][d], rbc_main[s][CTRL])
        if p < 0.05 and abs(r - 1) >= 0.03:
            val = sum(rbc_main[s][d]) / len(rbc_main[s][d])
            lines.append(
                f"- 红细胞计数下降（{'雄性' if s == 'male' else '雌性'} {d} mg/kg/day）："
                f"均值 {val:.2f}×10¹²/L，落在历史对照范围 {lo:.2f}–{hi:.2f} 内，"
                "且其余剂量组无同向改变（无剂量-反应关系），按 SOP 1.2 / 4.1 判为非受试物相关。")
top = max(LEVELS)
lines.append(
    f"- 肾小管嗜碱性变：对照组发生率 "
    f"{incidence(F_BASOPHILIA, 'male', CTRL) / n_main[('male', CTRL)] * 100:.0f}%，"
    f"最高剂量组 {incidence(F_BASOPHILIA, 'male', top) / n_main[('male', top)] * 100:.0f}%，"
    f"组间差异小于背景阈值（约 {BG_DIFF_THRESHOLD*100:.0f} 个百分点），"
    "按 SOP 5.1 判为背景病变。")
with open(os.path.join(APP, "report.md"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")

print(json.dumps(judgment, ensure_ascii=False, indent=2))
PY

python /tmp/_solve.py
