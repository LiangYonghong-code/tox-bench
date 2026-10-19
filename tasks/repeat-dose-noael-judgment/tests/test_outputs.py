# -*- coding: utf-8 -*-
"""判分器：只读取交付物与 tests/ 下的基准文件，不执行被测代码。

设计要点
--------
* **无 pytest 依赖也能跑**：本文件只使用 assert，不 import pytest、不使用 fixture，
  因此既能被 pytest 收集，也能被 eval/common.py 的内置 runner 直接调用
  （便于在没有 pytest / Docker 的机器上做冒烟验证）。
* **判分器与解题环境隔离**：本文件在 agent 结束、容器 commit 之后才注入，agent 看不到断言。
* **只判客观项**：NOAEL 数值、分性别 NOAEL、靶器官集合、关键发现的
  related / adverse / reversible 判定、以及 study.db 未被篡改。
  报告文体不做主观打分，只做最低限度的结构检查。
"""
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("TASK_APP_DIR", "/app")
JUDGMENT = os.path.join(APP, "judgment.json")
REPORT = os.path.join(APP, "report.md")
DB = os.path.join(APP, "study.db")

with open(os.path.join(HERE, "golden.json"), encoding="utf-8") as f:
    GOLDEN = json.load(f)

SEX_ALIASES = {
    "male": "male", "m": "male", "雄性": "male", "雄": "male",
    "female": "female", "f": "female", "雌性": "female", "雌": "female",
    "both": "both", "both sexes": "both", "m/f": "both", "f/m": "both",
    "male/female": "both", "male,female": "both", "两性别": "both",
    "雌雄": "both", "两性": "both", "雄雌": "both",
}
BACKGROUND_DOSE = {None, "", "background", "control", "对照", "背景", "背景病变", "n/a", "na"}


# ------------------------------------------------------------------ 工具
def _norm(text):
    return re.sub(r"[\s_\-（）()\[\]【】:：、,，.。/]+", "", str(text)).lower()


def _load_judgment():
    assert os.path.isfile(JUDGMENT), f"缺少交付物 {JUDGMENT}"
    with open(JUDGMENT, encoding="utf-8") as f:
        return json.load(f)


def _entries(sub):
    findings = sub.get("findings")
    assert isinstance(findings, list) and findings, "judgment.json 的 findings 必须是非空数组"
    out = []
    for item in findings:
        assert isinstance(item, dict), f"findings 元素必须是对象：{item!r}"
        for field in ("finding", "dose_mg_kg_day", "sex", "related", "adverse"):
            assert field in item, f"findings 元素缺少字段 {field}：{item!r}"
        sex = SEX_ALIASES.get(_norm(item["sex"]))
        assert sex, f"无法识别的 sex 取值：{item['sex']!r}（仅允许 male/female/both）"
        dose_raw = item["dose_mg_kg_day"]
        if isinstance(dose_raw, str) and dose_raw.strip().lower() in BACKGROUND_DOSE:
            dose = 0
        else:
            try:
                dose = float(dose_raw)
            except (TypeError, ValueError):
                raise AssertionError(f"无法识别的 dose_mg_kg_day：{dose_raw!r}")
        out.append({
            "finding": str(item["finding"]),
            "key": _norm(item["finding"]),
            "dose": dose,
            "sex": sex,
            "related": bool(item["related"]),
            "adverse": bool(item["adverse"]),
            "reversible": item.get("reversible", None),
        })
    return out


def _match(entries, spec):
    keys = {_norm(a) for a in spec["aliases"]}
    return [e for e in entries if e["key"] in keys and abs(e["dose"] - spec["dose_mg_kg_day"]) < 1e-6]


def _coverage(matched):
    covered = set()
    for e in matched:
        covered |= {"male", "female"} if e["sex"] == "both" else {e["sex"]}
    return covered


# ------------------------------------------------------------------ 1. 交付物完整性
def test_submission_files_exist():
    assert os.path.isfile(JUDGMENT), "缺少 /app/judgment.json"
    assert os.path.isfile(REPORT), "缺少 /app/report.md"
    text = open(REPORT, encoding="utf-8").read().strip()
    assert len(text) >= 200, "report.md 内容过短，未给出判定说明"


def test_judgment_schema():
    sub = _load_judgment()
    for field in ("study_no", "noael_mg_kg_day", "noael_by_sex",
                  "noael_determining_sex", "target_organs", "findings"):
        assert field in sub, f"judgment.json 缺少字段 {field}"
    assert isinstance(sub["noael_by_sex"], dict), "noael_by_sex 必须是对象"
    for sex in ("male", "female"):
        assert sex in sub["noael_by_sex"], f"noael_by_sex 缺少 {sex}"
    assert isinstance(sub["target_organs"], list), "target_organs 必须是数组"
    _entries(sub)  # 逐条校验 findings 结构


# ------------------------------------------------------------------ 2. 数据完整性
def test_study_db_not_modified():
    sha_path = os.path.join(HERE, "db.sha256")
    expected = open(sha_path, encoding="utf-8").read().strip()
    actual = hashlib.sha256(open(DB, "rb").read()).hexdigest()
    assert actual == expected, "study.db 被修改过（违反任务约束）"


# ------------------------------------------------------------------ 3. NOAEL
def test_overall_noael():
    sub = _load_judgment()
    got = float(sub["noael_mg_kg_day"])
    exp = float(GOLDEN["noael_mg_kg_day"])
    assert got == exp, f"整体 NOAEL 判定错误：期望 {exp:g} mg/kg/day，实际 {got:g}"


def test_noael_by_sex():
    sub = _load_judgment()
    got = {SEX_ALIASES.get(_norm(k)) or _norm(k): float(v)
           for k, v in sub["noael_by_sex"].items()}
    exp = {k: float(v) for k, v in GOLDEN["noael_by_sex"].items()}
    assert got == exp, f"分性别 NOAEL 判定错误：期望 {exp}，实际 {got}"


def test_noael_determining_sex():
    sub = _load_judgment()
    got = SEX_ALIASES.get(_norm(sub["noael_determining_sex"]))
    exp = GOLDEN["noael_determining_sex"]
    assert got == exp, f"决定整体 NOAEL 的性别判定错误：期望 {exp}，实际 {got}"


def test_target_organs():
    sub = _load_judgment()
    got = {_norm(x) for x in sub["target_organs"]}
    exp = {_norm(x) for x in GOLDEN["target_organs"]}
    assert got == exp, f"靶器官集合错误：期望 {sorted(exp)}，实际 {sorted(got)}"


# ------------------------------------------------------------------ 4. 关键发现的判定
def test_key_findings_classification():
    entries = _entries(_load_judgment())
    problems = []
    for spec in GOLDEN["findings"]:
        matched = _match(entries, spec)
        if not matched:
            problems.append(f"[{spec['key']}] 未在 findings 中报告（剂量 {spec['dose_mg_kg_day']}）")
            continue
        required = set(spec["sex_required"])
        allowed = required | set(spec.get("sex_allowed_extra", []))
        covered = _coverage(matched)
        if not required <= covered:
            problems.append(
                f"[{spec['key']}] 性别归属缺失：必须覆盖 {sorted(required)}，实际 {sorted(covered)}")
        elif not covered <= allowed:
            problems.append(
                f"[{spec['key']}] 性别归属错误（把未达到判定标准的性别也列为相关/有害）："
                f"允许 {sorted(allowed)}，实际 {sorted(covered)}")
        bad = [e for e in matched
               if e["related"] != spec["related"] or e["adverse"] != spec["adverse"]]
        if bad:
            problems.append(
                f"[{spec['key']}] related/adverse 判定错误：期望 "
                f"related={spec['related']}, adverse={spec['adverse']}；实际 "
                + "; ".join(f"related={e['related']}, adverse={e['adverse']}" for e in bad))
    assert not problems, "关键发现判定不符：\n  - " + "\n  - ".join(problems)


def test_reversibility_flags():
    entries = _entries(_load_judgment())
    problems = []
    for spec in GOLDEN["findings"]:
        if spec["reversible"] is None:
            continue
        matched = _match(entries, spec)
        if not matched:
            problems.append(f"[{spec['key']}] 未报告，无法判定 reversible")
            continue
        wrong = [e for e in matched if e["reversible"] is not spec["reversible"]]
        if wrong:
            problems.append(
                f"[{spec['key']}] reversible 判定错误：期望 {spec['reversible']}，实际 "
                + ", ".join(str(e["reversible"]) for e in wrong))
    assert not problems, "可逆性判定不符：\n  - " + "\n  - ".join(problems)


# ------------------------------------------------------------------ 5. 报告的证据链
def test_report_cites_key_numbers():
    text = open(REPORT, encoding="utf-8").read()
    compact = re.sub(r"\s+", "", text)
    assert "50" in compact, "report.md 未出现最终 NOAEL（50 mg/kg/day）"
    assert ("增重" in compact or "体重" in compact), "report.md 未提及体重增重这一决定性证据"
    assert ("150" in compact or "500" in compact), "report.md 未给出判定所依据的剂量组"
    assert ("肝" in compact or "liver" in compact.lower()), "report.md 未说明靶器官（肝脏）"
