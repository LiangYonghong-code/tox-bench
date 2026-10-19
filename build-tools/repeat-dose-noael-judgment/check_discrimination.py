# -*- coding: utf-8 -*-
"""变异解自检：把参考解的输出逐条改成"看似合理但违反 SOP"的答案，
要求每个变异解至少被一个用例判失败。

这是任务包最重要的作者侧验证：它证明判分器不是"只要交了文件就给分"，
而是真的能分辨 NOAEL 判定中的典型错误。

用法：
    python build-tools/repeat-dose-noael-judgment/check_discrimination.py
退出码 0 = 全部变异解都被抓住；1 = 存在漏网的变异解（题目或判分器需要修）。

判哪道题由**目录位置**决定：本文件住在 build-tools/<题目录名>/ 下，
只对该目录名对应的 tasks/<题目录名>/ 生效。
"""
import copy
import json
import os
import shutil
import sys

# 与 make_study_db.py 同一约定：父目录名 == tasks/ 下的题目录名
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TASK_DIR_NAME = os.path.basename(HERE)
sys.path.insert(0, os.path.join(REPO, "eval"))

import common  # noqa: E402  （复用评测驱动的临时环境与内置 runner）


# ------------------------------------------------------------------ 变异操作
def _entry(judgment, keyword, dose=None):
    for f in judgment["findings"]:
        if keyword in f["finding"] and (dose is None or f["dose_mg_kg_day"] == dose):
            return f
    raise KeyError(f"{keyword} @ {dose} 不在参考解输出中")


def m_noael_150(j):
    """漏判雌性 150 mg/kg/day 的增重抑制 → 整体 NOAEL 错报为 150。"""
    j["noael_mg_kg_day"] = 150
    j["noael_by_sex"] = {"male": 150, "female": 150}
    j["noael_determining_sex"] = "male"
    j["findings"] = [f for f in j["findings"]
                     if not (f["finding"] == "体重增重抑制" and f["dose_mg_kg_day"] == 150)]


def m_noael_500(j):
    """只看临床病理、漏掉体重增重抑制 → 整体 NOAEL 错报为 500。"""
    j["noael_mg_kg_day"] = 500
    j["noael_by_sex"] = {"male": 500, "female": 500}
    j["findings"] = [f for f in j["findings"] if f["finding"] != "体重增重抑制"]


def m_hypertrophy_adverse(j):
    """把适应性肝细胞肥大误判为有害（SOP 3.1 的经典陷阱）。"""
    _entry(j, "肝细胞肥大")["adverse"] = True
    j["target_organs"] = ["liver"]


def m_kidney_background_related(j):
    """把背景病变误判为受试物相关（SOP 5.1）。"""
    _entry(j, "肾小管嗜碱性变")["related"] = True


def m_rbc_related(j):
    """把孤立、落在历史对照范围内的红细胞下降误判为受试物相关（SOP 1.2）。"""
    _entry(j, "红细胞")["related"] = True


def m_adrenal_as_adverse(j):
    """把应激性继发改变当作独立毒性终点（SOP 6.1）。"""
    _entry(j, "肾上腺皮质空泡化")["adverse"] = True
    j["target_organs"] = sorted(set(j["target_organs"]) | {"adrenal"})


def m_bw_150_both(j):
    """把只在雌性达到有害标准的 150 组写成两性别均有害（性别归属错误）。"""
    _entry(j, "体重增重抑制", 150)["sex"] = "both"


def m_necrosis_not_reversible(j):
    """可逆性判定错误（SOP 7.2）。"""
    _entry(j, "肝细胞坏死")["reversible"] = False


def m_drop_necrosis(j):
    """漏报靶器官病变。"""
    j["findings"] = [f for f in j["findings"] if f["finding"] != "肝细胞坏死"]


def m_empty_findings(j):
    """只给结论、不给发现清单。"""
    j["findings"] = []


VARIANTS = [
    ("NOAEL 错报为 150（漏判雌性增重抑制）", m_noael_150),
    ("NOAEL 错报为 500（完全忽略体重指标）", m_noael_500),
    ("适应性肝细胞肥大误判为有害", m_hypertrophy_adverse),
    ("背景病变误判为受试物相关", m_kidney_background_related),
    ("孤立血液学差异误判为受试物相关", m_rbc_related),
    ("应激性继发改变计入靶器官", m_adrenal_as_adverse),
    ("150 组增重抑制的性别归属错误", m_bw_150_both),
    ("可逆性误判为不可逆", m_necrosis_not_reversible),
    ("漏报肝细胞坏死", m_drop_necrosis),
    ("findings 为空", m_empty_findings),
]


def run_oracle(task, app):
    ok, output = common.run_oracle_local(task, app)
    if not ok:
        print(output)
        raise SystemExit("参考解执行失败，先修参考解")
    with open(os.path.join(app, "judgment.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    # 不接 --task：下面的 VARIANTS 是针对本题的 28 天重复给药试验写的，
    # 拿去跑别的题只会得到无意义的失败。目录名就是选题依据。
    _, tasks = common.load_tasks()
    mine = [t for t in tasks if t.dir.name == TASK_DIR_NAME]
    if not mine:
        raise SystemExit(
            f"eval/tasks.yaml 里没有 dir 名为 `{TASK_DIR_NAME}` 的题目；"
            f"本脚本住在 build-tools/{TASK_DIR_NAME}/，只能为本目录对应的题服务")
    return 0 if check_one(mine[0]) else 1


def check_one(task):
    print(f"\n######## task{task.id} {task.slug} ########")
    app = common.prepare_local_app(task)
    try:
        base = run_oracle(task, app)
        os.makedirs(os.path.join(REPO, ".work"), exist_ok=True)
        shutil.copy(os.path.join(app, "judgment.json"),
                    os.path.join(REPO, ".work", "judgment.oracle.json"))

        results = common.run_tests_builtin(task, app)
        oracle_failed = [n for n, ok, _ in results if not ok]
        print(f"[oracle] {'全部通过' if not oracle_failed else '失败: ' + ', '.join(oracle_failed)}")
        if oracle_failed:
            return False

        print(f"\n[变异解] 共 {len(VARIANTS)} 个，要求每个至少被 1 个用例抓住\n")
        header = f"{'变异解':<34} {'结果':<6} 抓住它的用例"
        print(header)
        print("-" * len(header))
        escaped = []
        for name, mutate in VARIANTS:
            j = copy.deepcopy(base)
            mutate(j)
            with open(os.path.join(app, "judgment.json"), "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False, indent=2)
            res = common.run_tests_builtin(task, app)
            caught = [n for n, ok, _ in res if not ok]
            ok = bool(caught)
            if not ok:
                escaped.append(name)
            label = "抓住" if ok else "漏网"
            print(f"{name:<34} {label:<6} {', '.join(caught[:3])}"
                  + (" ..." if len(caught) > 3 else ""))
        print()
        if escaped:
            print("存在漏网的变异解，判分器需要加强：")
            for name in escaped:
                print("  -", name)
            return False
        print("全部变异解均被抓住：判分器能识别每一类错误。")
        return True
    finally:
        shutil.rmtree(app, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
