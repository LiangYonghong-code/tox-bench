# 临床前毒代动力学（TK）参数计算

## 背景

某创新药已完成大鼠单次给药毒代动力学研究。动物房给药记录、采血记录与生物分析实验室的仪器结果分散存放在 `study.db`（SQLite）中。现需据此计算每只可评价动物的 TK 参数，供申报资料使用。

实验室数据在转交前未做清理，存在重复分析、复测、拒收 run、低于定量下限、采样超窗等常见情况。**请按下列规则完成清洗与计算。**

---

## 数据

`study.db` 位于 `/app/study.db`，包含以下表：

| 表 | 说明 |
|---|---|
| `animals` | 动物编号、剂量组、名义剂量、给药途径、性别、体重 |
| `dosing_records` | 名义剂量与实际给药剂量 |
| `sample_records` | 计划采样时间、实际采样时间、采样状态 |
| `bioanalytical_runs` | 生分析结果、是否接受 |
| `protocol_config` | 本研究的阈值与规则参数 |

表结构需自行探索。**`protocol_config` 中的规则参数（采样窗口、剂量偏离阈值、终末相点数上下限、R² 下限、外推上限等）必须从库中读取，不得硬编码在程序里**——本研究的参数值不保证与历史项目相同。

---

## 数据清洗规则（强制）

1. 仅使用 `bioanalytical_runs.accepted = 1` 的记录。
2. 同一 `(animal_id, timepoint_h)` 存在多条接受记录时，**取 `run_id` 最大者**（复测以最后一次结果为准）。
3. `sample_status` 不为 `OK` 或 `actual_time_h` 为 `NULL` 的时点不参与计算。
4. **实际采样时间超出允许窗口的时点不参与计算。** 窗口规则：
   - 名义时点 ≤ 1 h：允许偏差为 `sample_window_abs_h`（单位：小时，绝对值）
   - 名义时点 > 1 h：允许偏差为名义时点的 `sample_window_rel_pct`（相对百分比）
5. 浓度值为 `BLQ` 的时点不参与计算。**不得按 0 处理。**
6. 实际给药剂量相对名义剂量的偏离超过 `dose_deviation_max_pct` 时，该动物**整只不评价**：
   不计算任何参数，**且不出现在输出文件中**（不是输出一整行空值）。

---

## 计算规则

所有时间量一律使用 `actual_time_h`。

### 基础参数

- `Cmax` = 可定量时点中的最大浓度
- `Tmax` = `Cmax` 对应的 `actual_time_h`

### AUC0-t

从首个到最后一个可定量时点，采用**线性上升 / 对数下降**梯形法：

- 上升段（`C2 >= C1`）：`(C1 + C2) / 2 × dt`
- 下降段（`C2 < C1`）：`(C1 - C2) / ln(C1 / C2) × dt`

### 终末相消除速率常数 λz

在**排除 Tmax 及其之前**的时点后：

1. 从最后 `min_terminal_points` 个时点开始，每次向前增加 1 个时点，直到 `max_terminal_points` 个，得到若干候选组合。
2. 对每个候选组合做 `ln(C)` 对 `t` 的线性回归，计算调整后判定系数
   `adj R² = 1 - (1 - R²)(n - 1) / (n - 2)`
3. 取 `adj R²` 最大的一组，其斜率取负即为 `λz`。
4. 若最大 `adj R² < terminal_r2_min`，则 `λz` 不可得，该动物的 `auc_0_inf_h_ng_ml`、`t_half_h`、`mrt_h`、`cl_ml_h_kg`、`vss_ml_kg` 全部留空。

### 派生参数

- `t½ = ln(2) / λz`
- `AUC0-inf = AUC0-t + C_last / λz`（`C_last` 为最后一个可定量时点的浓度）
- **外推比例** `= (C_last / λz) / AUC0-inf × 100%`
- 若外推比例 > `max_extrap_pct`：`auc_0_inf_h_ng_ml`、`mrt_h`、`cl_ml_h_kg`、`vss_ml_kg` 留空，**`t_half_h` 仍然输出**（终末相回归本身有效）
- `AUMC0-t`：对 `t × C` 使用**线性梯形法**（两段均用 `(y1 + y2) / 2 × dt`）
- `AUMC0-inf = AUMC0-t + t_last × C_last / λz + C_last / λz²`
- `MRT = AUMC0-inf / AUC0-inf`
- `CL = 实际给药剂量 / AUC0-inf`

  ⚠️ 单位换算：剂量 `mg/kg` 需换算为 `ng/kg`（`× 1e6`），AUC 为 `h·ng/mL`，故 `CL` 单位为 `mL/h/kg`。

- `Vss = MRT × CL`
- **`MRT`、`CL`、`Vss` 仅对 IV 给药组计算；非 IV 组一律留空。**

---

## 输出

将结果写入 `/app/tk_parameters.csv`，UTF-8 编码，含表头，列顺序严格一致：

```text
animal_id,dose_group,route,cmax_ng_ml,tmax_h,auc_0_t_h_ng_ml,auc_0_inf_h_ng_ml,t_half_h,mrt_h,cl_ml_h_kg,vss_ml_kg
```

- 排序：`dose_group` 升序，`animal_id` 升序
- 不可得或不适用的字段写**空字符串**（不要写 `NA`、`-`、`0`）
- 数值直接输出原始浮点数，评估按相对误差 ≤ 0.5% 判定。
  ⚠️ **不要为了排版对数值做四舍五入**：时间量本身很小（如 `tmax_h` 约 `0.079`），
  保留两位小数会带来 1% 以上的相对误差而直接判错。**所有数值字段请至少保留
  6 位有效数字，或原样输出未经格式化的浮点数。**

---

## 约束

- 不得修改 `study.db`
- 程序不得需要交互输入
