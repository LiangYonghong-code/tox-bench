# 任务：28 天重复给药毒性试验的 NOAEL 判定

## 背景

你是一名临床前毒理研究机构的**专题负责人**。一项大鼠 28 天重复给药（灌胃）毒性试验已经
完成，你需要依据本机构的判定标准给出该试验的 **NOAEL** 结论，并说明支持该结论的关键证据。

## 输入文件

| 文件 | 说明 |
|---|---|
| `/app/study.db` | 试验原始数据（SQLite 数据库） |
| `/app/SOP_toxicity_criteria.md` | 本机构判定标准（SOP），是本次判定的**唯一判定依据** |

### study.db 表结构

| 表 | 字段 | 说明 |
|---|---|---|
| `study_info` | key, value | 试验基本信息（试验编号、种属、给药途径、周期等） |
| `groups` | group_id, dose_mg_kg_day, label, n_male, n_female, is_recovery | 分组与剂量 |
| `animals` | animal_id, sex, group_id, is_recovery | 动物信息；`sex` ∈ {male, female} |
| `body_weight` | animal_id, day, weight_g | 个体体重（g），按天记录 |
| `food_intake` | animal_id, week, g_per_day | 个体摄食量（g/天），按周记录 |
| `organ_weights` | animal_id, organ, weight_g | 终末解剖脏器重量（g），organ 含 brain / liver / kidney / spleen |
| `hematology` | animal_id, rbc_10e12_l, hgb_g_l, wbc_10e9_l, plt_10e9_l | 血液学 |
| `biochemistry` | animal_id, alt_u_l, ast_u_l, alp_u_l, bun_mmol_l, crea_umol_l | 血液生化 |
| `histopathology` | animal_id, organ, finding, grade | 组织病理学所见；`grade` 为分级（1 轻微 → 4 重度），未列出的动物视为未见该改变 |
| `historical_control` | parameter, sex, mean, sd, min, max | 本机构历史对照范围 |

### 试验设计

- 主试验：对照组与 50 / 150 / 500 mg/kg/day 三个剂量组，每性别每组 10 只，给药 28 天，
  第 29 天终末解剖。
- 恢复期：对照组与 500 mg/kg/day 组，每性别每组 5 只，停药恢复 28 天，第 57 天解剖。
- **恢复期动物必须与主试验动物分开分析。**

## 交付物

### 1. `/app/judgment.json`

```json
{
  "study_no": "TOX-XXXX-XXXX",
  "noael_mg_kg_day": 0,
  "noael_by_sex": {"male": 0, "female": 0},
  "noael_determining_sex": "male",
  "target_organs": ["liver"],
  "findings": [
    {
      "finding": "肝细胞坏死",
      "dose_mg_kg_day": 500,
      "sex": "both",
      "related": true,
      "adverse": true,
      "reversible": true
    }
  ]
}
```

字段规则：

- `findings` 必须覆盖**体重与摄食、临床病理学（血液学与血液生化）、脏器重量与组织病理学**
  中所有在对照组与给药组之间存在有意义变化的指标，不得遗漏、不得虚构数据中不存在的变化。
- `finding` 使用**数据中出现的中文名称**（组织病理学所见直接使用 `histopathology.finding`
  的取值；体重/摄食与临床病理学指标使用清晰的中文名称，如"体重增重抑制"、"红细胞计数下降"）。
- `sex` 取值仅限 `"male"` / `"female"` / `"both"`。
- `dose_mg_kg_day` 填该发现**首次**被判定为受试物相关的剂量（**按性别分别判断**）；
  若该发现在对照组与各剂量组发生率相近（背景病变），填 `0`。
- 同一发现若两个性别的**首次相关剂量不同**，须按性别分别列出两条（例如雌性 150、
  雄性 500）；若相同，可用一条 `sex: "both"` 记录。
- `related` / `adverse` / `reversible` 的判定规则见 SOP（`reversible` 可为 `null`）。
- `target_organs` 使用英文器官名（`liver` / `kidney` / `adrenal` / `thymus` / `spleen` 等）。
- `noael_by_sex` 的键为 `male` / `female`。

### 2. `/app/report.md`

判定说明，**不超过 1 页**，须包含：

- 最终 NOAEL 结论（含分性别 NOAEL 与整体 NOAEL）；
- 支持该结论的**关键数值证据**（例如：判定为有害的最低剂量组的增重/摄食相对变化百分比、
  相关临床化学指标数值、组织病理学发生率与分级）；
- 判定为非有害但受试物相关的改变及其依据；
- 判定为背景病变或非受试物相关的改变及其依据。

## 约束

- **不得修改** `/app/study.db` 与 `/app/SOP_toxicity_criteria.md`。
- 只使用容器内已安装的库（`numpy`、`pandas`、`scipy` 等）。
- 所有数值**必须由 `study.db` 计算得出**，不得硬编码或凭经验填写。
- 不得使用本 SOP 之外的判定标准。

## 完成标准

- `/app/judgment.json` 可被解析，字段齐全且符合上述 schema；
- 分性别 NOAEL 与整体 NOAEL 与 SOP 一致；
- 关键发现的 `related` / `adverse` / `reversible` 判定与 SOP 一致；
- `target_organs` 与 SOP 一致；
- `/app/report.md` 给出支持结论的关键数值证据。
