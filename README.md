# tox-bench

面向**临床前毒理/药理**场景的大模型 Coding 评测任务包：把贴近真实场景的专业数据装进 Docker 沙箱，
让 agent 依据机构 SOP 或题面规则完成专业判定与计算，再用**与 agent 完全隔离**的自动判分器
给出可复现的 reward。

> 这是"专业领域出题 + 环境搭建 + 验证脚本 + 参考答案"的一次完整实践：
> 题目设计参照真实科研/工业场景，判定标准客观可执行，判分器对变异解的识别能力经过自检确认。

当前覆盖两种题型：

| task | 题目 | 题型 | 交付物 |
|---|---|---|---|
| 1 | [28 天重复给药毒性试验的 NOAEL 判定](tasks/repeat-dose-noael-judgment/) | 规则判定类（依据容器内 SOP） | `judgment.json` + `report.md` |
| 2 | [临床前 TK 参数计算](tasks/clinical-tk-parameter-analysis/) | 数据清洗 + 数值计算类 | `tk_parameters.csv` |

---

## 1. 仓库结构

```
tox-bench/
├── README.md                          ← 本文件
├── harbor-example.yaml                ← Harbor 标准任务包跑法（可选，见第 2 节末尾）
├── tasks/
│   ├── repeat-dose-noael-judgment/    ← 可提交的任务包，一题一目录
│   │   ├── instruction.md             ← 给 agent 看的题面
│   │   ├── task.toml                  ← 元数据 + 各阶段超时与资源
│   │   ├── environment/               ← 构建上下文（只含数据与 SOP）
│   │   │   ├── Dockerfile
│   │   │   ├── SOP_toxicity_criteria.md   ← 容器内的判定标准，题面不重复其规则
│   │   │   └── study.db               ← 生成物，随仓库提交
│   │   ├── tests/                     ← 判分器，**不进镜像**
│   │   │   ├── test.sh
│   │   │   ├── test_outputs.py
│   │   │   ├── golden.json
│   │   │   └── db.sha256
│   │   └── solution/
│   │       └── solve.sh               ← 参考解（全部数值由 study.db 计算得出）
│   └── clinical-tk-parameter-analysis/
│       ├── instruction.md
│       ├── task.toml
│       ├── environment/
│       │   ├── Dockerfile
│       │   └── study.db               ← TK 原始数据（含 6 类设计好的数据质量问题）
│       ├── tests/
│       │   ├── test.sh
│       │   ├── test_outputs.py        ← 17 个用例
│       │   ├── tk_golden.json         ← 金标准（numpy 独立实现生成）
│       │   └── db.sha256
│       └── solution/
│           └── solve.sh               ← 参考解（纯标准库，与金标准互为交叉验证）
├── build-tools/                       ← 作者侧工具，**不进入镜像、不随任务包提交**
│   ├── repeat-dose-noael-judgment/    ← 按题分目录：目录名与 tasks/ 下的题目录同名
│   │   ├── make_study_db.py           ← 生成研究数据（含设计的效应量 = 部分真值）
│   │   └── check_discrimination.py    ← 变异解自检（只对本题的 10 个变异解有效）
│   ├── clinical-tk-parameter-analysis/
│   │   ├── make_study_db.py           ← 生成 TK 数据（含真值 PK 模型）+ 刷新 db.sha256
│   │   ├── build_golden.py            ← 生成 tk_golden.json（numpy 实现，独立于参考解）
│   │   ├── check_discrimination.py    ← 变异解自检（本题的 8 个变异解）
│   │   └── README.md                  ← 构建流程 + 两次判分偏差的复盘
│   └── test_agent_wiring.py           ← agent 链路自检（零模型调用成本、不需要 Docker，对任意题都跑）
└── eval/
    ├── tasks.yaml                     ← 测评清单：题号/slug/镜像名/workdir 的唯一来源
    ├── common.py                      ← 四阶段共享工具 + 无 Docker 的本机判分原语
    ├── phase1_build.py                ← Phase 1 建基础镜像 + WORKDIR / task.toml 对拍
    ├── phase2_agent.py                ← Phase 2 跑模型 + 把容器 commit 成快照
    ├── phase3_verify.py               ← Phase 3 起干净容器注入 tests 判分 + 收交付物
    ├── phase4_summarize.py            ← Phase 4 汇总成表 + 跨 run 指纹比对
    ├── mini_config.yaml               ← mini-swe-agent 的最小配置覆盖
    └── runs/<run-id>/                 ← 每次 run 的轨迹、交付物、判分产物（不入库）
```

`jobs/` 是 Harbor 的运行产物目录（默认 `jobs/`，已在 `.gitignore` 中忽略）。
它与 `eval/runs/` 一样属于**可再生的评测输出**，不入库。

**评测驱动是四个阶段、四个独立进程**（详见第 2、5 节）。这样拆的好处是多题可并行、
任何一阶段都能单独重跑（`--only`/`--single`），且"基础设施失败"与"模型失败"不会混为一谈。

**仓库同时兼容 Harbor 的标准任务包格式**：`task.toml` 里的 `[task]` / `[environment]` /
`[tests]` / `[agent]` / `[verifier]` 都是 Harbor 原生字段，`harbor-example.yaml` 给出了一次
跑完两题的示例。两套跑法的分工见第 2 节末尾 —— 它们不是二选一，而是"快速回归"与"标准评测"的分工。

**`build-tools/` 刻意放在 `tasks/` 之外**：`make_study_db.py` 里写着每个剂量组的效应量，
拿到它就能反推大部分"真值结论"。让"不提交"由**目录结构**保证，而不是靠文档里一句口头约定：
`environment/Dockerfile` 只 `COPY study.db` 与 SOP，因此这些文件既不会进镜像，也不在任务包里。

**按题分目录的规则**：与某道题绑定的作者侧脚本（建库、金标准、变异解自检）放在
`build-tools/<题目录名>/`，目录名必须与 `eval/tasks.yaml` 里该题的 `dir` 末段一致 ——
这些脚本都用 `os.path.basename(__file__的父目录)` 反推自己服务哪道题，因此
**加一道题 = 加一个同名目录**，不需要改任何路径常量。与题无关的 harness 自检
（`test_agent_wiring.py`）留在 `build-tools/` 根下，它接 `--task N` 对任意题都能跑，
交付物清单从各题 `task.toml` 的 `[task].deliverables` 读，不硬编码。

---

## 2. 快速开始

```bash
# ① 生成研究数据（改了生成器才需要重跑；会同时刷新该题的 tests/db.sha256）
python build-tools/repeat-dose-noael-judgment/make_study_db.py      # task1
python build-tools/clinical-tk-parameter-analysis/make_study_db.py  # task2
# task2 重建数据后还必须接着重建金标准：
python build-tools/clinical-tk-parameter-analysis/build_golden.py

# ② 变异解自检：每个"看似合理但违反规则"的答案都必须被抓住（不需要 Docker）
python build-tools/repeat-dose-noael-judgment/check_discrimination.py      # 10 个变异解
python build-tools/clinical-tk-parameter-analysis/check_discrimination.py  # 8 个变异解

# ③ oracle / base 对照 —— 无 Docker 快版（期望 reward = 1 / 0）
#    --oracle 要起容器跑参考解，一次只能指定一题，故按题分别跑
python eval/phase3_verify.py --run-id smoke --only 1 --local --oracle
python eval/phase3_verify.py --run-id smoke --only 1 --local
python eval/phase3_verify.py --run-id smoke --only 2 --local --oracle
python eval/phase3_verify.py --run-id smoke --only 2 --local

# ④ agent 链路自检（零模型调用成本、不需要 Docker，用 DeterministicModel 跑通接线，对全部题）
python build-tools/test_agent_wiring.py

# ⑤ 完整链路 —— 需要 Docker，且必须等 ②③ 都通过之后
python eval/phase1_build.py     --run-id main
python eval/phase2_agent.py     --run-id main
python eval/phase3_verify.py    --run-id main
python eval/phase4_summarize.py --run-id main
```

第 ① 步对 task2 是两步：`make_study_db.py` 重建数据后**必须**接着跑 `build_golden.py`，
否则判分器会拿旧金标准比对新数据。`tests/db.sha256` 由 `make_study_db.py` 自动刷新。

前置依赖：

| 用途 | 需要什么 |
|---|---|
| 跑判分器 / ①②③④ | Python 3.11+、`numpy`、`pandas`（`pytest` 可选） |
| 跑 ⑤ 的 Phase 1/2/3 | Docker 引擎可用 |
| 跑 ⑤ 的 Phase 2  | `mini-swe-agent`（已在 2.2.8 上验证）、模型 API key |

四个阶段的分工与依赖：

| 阶段 | 命令 | 依赖 | 产出 |
|---|---|---|---|
| 1 Build | `phase1_build.py --run-id ID` | Docker | `tox/<slug>:base`、`_build/workdirs.json` |
| 2 Agent | `phase2_agent.py --run-id ID` | 阶段 1、模型 API key | `tox/<slug>:solved-ID` 快照、`taskN/result.json` |
| 3 Verify | `phase3_verify.py --run-id ID` | 阶段 2（或 `--oracle/--base/--local`） | `taskN/reward.txt`、`ctrf.json`、交付物 |
| 4 Summarize | `phase4_summarize.py --run-id ID` | 阶段 2、3 的产物 | `summary.json`、`summary.md` |

阶段之间只通过 `eval/runs/<run-id>/` 里的文件与 Docker 镜像名 `tox/<slug>:solved-<run-id>` 耦合，
因此任何一阶段都能单独重跑：`--only 1,2` 选题、`--single 1` 单题、`--force` 强制重建。

`--local` 分支不需要 Docker、也不需要 pytest（缺失时用内置 runner 逐条调用 `test_*` 函数），
是"判分器本身对不对"的最快验证手段。

### 完整评测的两种跑法

| | 自建四阶段（`eval/phase*.py`） | Harbor（`harbor-example.yaml`） |
|---|---|---|
| 定位 | **作者侧**验证与复核 | **标准任务包**评测与平台复现 |
| 构建 / 跑 agent / 判分 | ✅ 四阶段独立进程 | ✅ 一条命令完成 |
| agent 选择 | 仅 mini-swe-agent | 30+ agent，含 `oracle` / `nop` |
| oracle / base 对照 | `--oracle` / `--base` / `--local` | `-a oracle` / `-a nop` |
| 无 Docker 快速回归 | ✅ `--local`，秒级 | ❌ 必须起容器 |
| 跨 run 指纹可比性 | ✅ 自动比对并告警 | ❌ |
| `task.toml` 与清单对拍 | ✅ `spec_check` | ❌ |
| 并发 / 重试 / 多 agent | ⚠️ 题级并行 | ✅ trial 级并发与重试 |
| 产物目录 | `eval/runs/<run-id>/` | `jobs/<job-id>/` |

**日常改题用自建四阶段**（第 5 节的"三道验证"全部靠它，秒级且零模型调用成本）；
**对外评测、平台提交或换 agent 时用 Harbor**：

```bash
uv tool run harbor run --config harbor-example.yaml
# 也可以直接命令行指定：
uv tool run harbor run --path tasks --agent mini-swe-agent \
  --model your-provider/your-model --n-concurrent 2
```

两者共享同一份 `tasks/` 与 `task.toml`，区别只在驱动方式。需要留意：`task.toml` 里
`[phases]` / `[resources]` 是自建 harness 读的字段，Harbor 会**静默忽略**它们；
给 Harbor 用的超时/资源必须写在 `[agent]` / `[verifier]` / `[environment]` 原生字段里。
两套值要手工保持一致（第 7 节有专门一条）。

---

## 3. 题目考什么

### 3.1 task1 — 28 天重复给药毒性试验的 NOAEL 判定

试验：大鼠 28 天重复给药（灌胃），剂量 0 / 50 / 150 / 500 mg/kg/day，每性别每组 10 只；
另设对照组与 500 mg/kg/day 组的 28 天恢复期。数据以 SQLite 提供（体重、摄食、血液学、
血液生化、脏器重量、组织病理学、历史对照范围），判定标准写在容器内的 SOP 里。

agent 必须交付 `judgment.json`（机器判分）与 `report.md`（证据说明）。

**正确答案**：整体 NOAEL = **50 mg/kg/day**（由雌性决定；雄性 150）。
靶器官仅 **liver**。

判定链路上的六个关键点（每题一个"陷阱"）：

| # | 数据中的设计 | 正确判定 | 典型错误 |
|---|---|---|---|
| 1 | 150、500 组肝细胞肥大 + 肝/脑重量比升高，但 ALT/AST 仍在历史范围内 | 适应性反应：**相关但非有害** | 误判为有害 |
| 2 | 500 组肝细胞坏死 ≥2 级，ALT/AST 超历史上限 | **有害**，靶器官 = liver | 漏判 |
| 3 | 雌性 150 组增重 −15%、摄食 −10%（雄性仅 −5%/−2%） | 雌性**有害** → 雌性 NOAEL = 50，整体 NOAEL = 50 | 只看病理，错报 NOAEL = 150 |
| 4 | 雄性 50 组红细胞下降 5%，统计学显著但落在历史对照范围内且无剂量-反应 | **非受试物相关** | 误判为相关/有害 |
| 5 | 肾小管嗜碱性变：对照组 40%，各剂量组差异 <15 个百分点 | **背景病变**（dose 填 0） | 误判为相关 |
| 6 | 500 组肾上腺皮质空泡化、胸腺萎缩，仅见于增重抑制 ≥10% 的动物 | 应激性继发改变：**相关但非有害**，不得计入靶器官 | 计入靶器官 |
| 7 | 500 组恢复期肝细胞坏死由 3 级降至 1 级 | 可逆，但**可逆性不改变给药期末的有害性判定** | 因"可逆"而上调 NOAEL |

#### 设计取舍：规则放在 SOP 里，题面不重复

题面只给**输入说明、交付格式、约束与完成标准**，判定阈值（≥10% 增重抑制、≥5% 摄食下降、
>30% 对照发生率、分级 ≥2 级等）全部写在容器内的 `SOP_toxicity_criteria.md`。

这样做有两个好处：

1. **贴近真实工作**：专题负责人本来就是"按机构 SOP 判定"，而不是凭个人经验；
2. **避免题面替模型完成判断**：判定规则放在 `SOP_toxicity_criteria.md` 里，
   模型必须**主动读取并正确套用**，同时还要自己算出统计量
   （组均值、相对变化、发生率、历史范围比较），而不是从题面直接照抄结论。

### 3.2 task2 — 临床前 TK 参数计算

某创新药大鼠单次给药毒代动力学研究：给药记录、采血记录、生物分析结果分散存放在
`study.db`（SQLite）中，需算出每只可评价动物的标准 TK 参数。实验室数据**未做清理**，
埋了 6 类常见质量问题：

| # | 动物 | 埋入的问题 | 若处理错误的后果 | 正确做法 |
|---|---|---|---|---|
| 1 | 101 | 1h 有两条 `accepted=1` 的重复分析 run | AUC0-t 偏高 2.88% | 取 `run_id` 最大者 |
| 2 | 201 | 复测 run 被 QC 拒收（`accepted=0`）且 `run_id` **更大** | Cmax 偏高 240.2% | **先**按 `accepted=1` 过滤，**再**取 `run_id` 最大 |
| 3 | 全部 | 首末时点为 BLQ | 202 的 AUC0-t 偏高 8.71% | BLQ 排除，**不得按 0 积分** |
| 4 | 301 | 2h 计划时点实际在 3.1h 采样 | AUC0-t 偏高 6.74% | 按窗口规则排除（≤1h 用绝对窗口，>1h 用相对窗口） |
| 5 | 302 | 实际给药 38 vs 名义 50 mg/kg | 该动物本不该出现 | 偏离超阈值 → 整只不评价 |
| 6 | 102 | >=6h 样本缺失 → 外推比例 76.8% | 4 个字段本应留空 | 外推超限则 AUC0-inf / MRT / CL / Vss 留空，但 **t½ 仍要报** |

除清洗外还要实现 PKNCA 风格的终末相选点（adj-R² 最大化）、线性上升/对数下降梯形法、
以及 AUMC 走**纯线性**梯形（与 AUC0-t 不同——这里曾出现过一次判分偏差，见
[build-tools README](build-tools/clinical-tk-parameter-analysis/README.md)）。

#### 两个题的取舍正好相反，这是有意的

| | task1 | task2 |
|---|---|---|
| 判定规则放哪 | 容器内 SOP，题面**不重复** | 题面**逐条列出** |
| 难点 | 主动读规则 + 专业判断（适应性 vs 有害） | 实现准确度（十几条规则一条都不能漏） |

task2 的规则之所以写在题面里，是因为它的规则（"取 run_id 最大者""BLQ 不按 0 处理"）
本身就是**可辩护的行业惯例**；如果只在判分器里隐含这些规则，会让判分变成猜出题人口味。
因此本题选择把规则明确写出。

---

## 4. 判分器怎么工作

- **隔离**：`tests/` 在 agent 跑完、容器被 commit 成快照之后才注入，agent 看不到任何断言。
- **唯一判据**：`test.sh` 全部用例通过写 `reward=1`，任一失败写 `0`；同时输出
  `ctrf.json` 供平台逐用例展示。
- **只判客观项**：NOAEL 数值、分性别 NOAEL、决定整体 NOAEL 的性别、靶器官集合、
  7 条关键发现的 `related` / `adverse` / `reversible`、以及 `study.db` 的 SHA256 未被篡改。
  报告不做主观打分，只检查是否给出了关键数值证据。
- **容忍措辞差异**：发现名称用别名表匹配，`male/M/雄性` 等写法归一化，
  背景病变的 `dose` 允许 `0` / `null` / `"background"`。
- **不惩罚多报**：多列出的发现本身不判错（但若误判为有害，会通过 `target_organs`
  与 NOAEL 体现出来）。

task2 的判分器（17 个用例）遵循同一约定：结构检查（文件/列名与顺序/行数/`study.db` 未被改动）、
纳排检查（302 因剂量偏离被排除）、与金标准 `tk_golden.json` 的**全量逐值对账**（相对误差 ≤0.5%）、
以及 8 个定向陷阱锚点（一个陷阱一个用例，失败即可定位漏掉哪条规则）。
其中 `test_aumc_uses_pure_linear_trapezoid` 故意**硬编码数值、不读金标准**，专门挡住
"金标准与参考解共享同一个理解错误"的回归——这正是 2026-10 那次判分偏差的形态。

两题的判分器都**不 import pytest**、不使用 fixture，因此既能被 pytest 收集，
也能被内置 runner 直接调用。

---

## 5. 改题之后必跑的三道验证

改数据、题面、SOP 或判分器之后，**三道都要跑**，缺任何一道都可能让缺陷被漏掉：

| # | 验证 | 命令（task1 / task2） | 期望 |
|---|---|---|---|
| 1 | 变异解自检 | `python build-tools/repeat-dose-noael-judgment/check_discrimination.py`<br>`python build-tools/clinical-tk-parameter-analysis/check_discrimination.py` | 10/10、8/8 被抓住，无漏网 |
| 2 | oracle 对照 | `phase3_verify.py --run-id smoke --only <N> --local --oracle` | `reward = 1` |
| 3 | base 对照 | `phase3_verify.py --run-id smoke --only <N> --local` | `reward = 0` |

第 1 道的必要性：oracle 与判分器出自同一套认知，它们只能证明"彼此一致"。
**变异解才是真正在问"判分器会不会被似是而非的答案骗过去"** —— 这些变异解，
每一个都对应一种模型很容易犯的专业判断错误。

三道都用 `--local`（无 Docker）即可，秒级出结果。需要**全保真**时换成 Docker 版
（`--oracle` 一次只能指定一题）：

```powershell
python eval/phase3_verify.py --run-id main --only 2 --oracle   # 期望 reward = 1
python eval/phase3_verify.py --run-id main --only 2 --base     # 期望 reward = 0
```

`--oracle` 由 Phase 3 自己起容器、跑 `solution/solve.sh`、commit 成
`tox/<slug>:oracle-<run-id>`，再走与正式评测**完全相同**的注入与判分路径 ——
省掉了手工拼接 Docker 命令这一最容易失配的环节。

---

## 6. 用 mini-swe-agent 测模型

前三道验证证明**判分器**可信；这一步才开始测量**模型**。

```
base（空基线，reward 0） ←── 模型（agent）──→ oracle（标准解，reward 1）
        下限                    被测对象                上限
```

### 6.1 为什么用 mini-swe-agent

- 它是**框架**，不是"另一个要适配的评测集"：`DefaultAgent` + `DockerEnvironment` +
  `LitellmModel` 三个类就能拼出一次完整运行，容器 ID 挂在 `env.container_id` 上，
  便于我们在 agent 结束后接管判分。
- 走 litellm，不同厂商的模型切换只改一个 `--model` 字符串。
- **不覆盖上游 prompt**：`eval/mini_config.yaml` 只改环境超时，`system_template` /
  `instance_template` 一律沿用上游默认——上游模板里带着 bash heredoc / sed / nl 的示例，
  换掉它，测出来的就变成"模型熟不熟这套 prompt"，而不是"模型的毒理判定能力"。
  本次运行的**事实性约定**（交付物路径、结束命令）以 `harness_note` 追加在题面之后。

### 6.2 跑之前：先确认地基

```powershell
python eval/phase1_build.py  --run-id main              # 建基础镜像
python eval/phase3_verify.py --run-id main --only 1 --oracle   # 必须 = 1
python eval/phase3_verify.py --run-id main --only 1 --base     # 必须 = 0
python eval/phase2_agent.py  --run-id main              # 这才开始测模型
python eval/phase3_verify.py --run-id main
python eval/phase4_summarize.py --run-id main
```

**顺序不能颠倒**：如果 oracle/base 不对，判分器本身就是坏的，此时 agent 跑出来的 0 分
你无法区分是"模型能力不足"还是"判分器/参考解存在缺陷"。详见第 5 节。

模型名通过 `--model` 参数或环境变量 `MSWEA_MODEL_NAME` 显式指定，harness 不再提供
fallback 默认值，避免不同模型测评时因漏传参数而跑错模型。换模型只改一个 `--model`
字符串。`--only 1` 可选题重跑；`--cost-limit`（默认 $2）与 `--max-steps`（默认 40）
是有意的测量口径，不要为了让某个模型跑完就调松。

### 6.3 每次运行产出什么

```
eval/runs/<run-id>/
├── _build/build_report.json    ← Phase 1：构建结果 + WORKDIR 与 task.toml 对拍
├── _build/workdirs.json        ← 镜像里真实的 WORKDIR（供后续阶段使用）
├── _agent_summary.json         ← Phase 2：每题一行
├── _verify_summary.json        ← Phase 3：每题一行
├── summary.json / summary.md   ← Phase 4：汇总表
└── taskN/
    ├── prompt.md               ← 本次实际喂给模型的完整题面（题面 + harness 约定）
    ├── trajectory.json         ← mini 的完整轨迹（每步的思考、命令、观测、成本）
    ├── agent.log               ← mini 的运行日志（纯文本副本，JSON 解析失败时兜底）
    ├── result.json             ← 步数 / 成本 / 耗时 / exit_status / 改动文件数 / task_fingerprint
    ├── docker-diff.txt         ← 容器内改动文件清单
    ├── snapshot.tag            ← Phase 2 commit 出的快照镜像名
    ├── reward.txt / ctrf.json  ← Phase 3：唯一判据 + 逐用例状态（哪个陷阱没判对）
    ├── judgment.json / report.md  ← 模型交上来的交付物快照
    └── verify.log              ← 判分过程日志
```

`summary.md` 形如：

```
| task | 题目 | 结果 | 用例(通过/总) | 步数 | 耗时(s) | 改动文件 | 成本($) | 指纹 |
| task1 | noael-judgment | PASS | 10/10 | 23 | 412 | 7 | 0.31 | `6fb7d91f` |
```

`reward` 为 **`未判定`** 表示**基础设施失败**（没有快照、`reward.txt` 内容异常、参考解跑不起来），
不是"模型得 0 分"。两者在 `summary.md` 里分列两节，避免把基础设施故障计入模型失败。

`task_fingerprint` 是题面 + SOP + 数据的联合哈希。Phase 4 会自动扫 `eval/runs/*/summary.json`，
取最近一次作基线比对：**指纹不同就在报告顶部插一段"不可比"告警**，
并写进 `summary.json` 的 `meta.fingerprint_comparable`。这是做跨 run 比较最容易忽略的问题。

### 6.4 判分的隔离方式

四阶段架构下隔离是**结构性**的，不再是可选项：

```
Phase 2: agent 在容器里跑 ──→ docker commit ──→ tox/<slug>:solved-<run-id>
                                                      │
Phase 3: 从快照起干净容器 ←───────────────────────────┘
         └─ 这时才 docker cp tests/ → /tests → 跑 test.sh → 读 reward.txt
```

agent 全程看不到 `tests/`（它不在镜像里），判分器跑在一个由快照新起的容器里。
**快照必须由 Phase 2 产出**：Phase 3 是另一个进程，那时 agent 容器早已被销毁。

---

## 7. 已知限制与工程边界

这一节保留完整的工程问题记录。每一条都对应一次真实的判分偏差、一次基础设施故障，
或一次跨平台行为差异；它们共同构成了判分器"为什么可信"的反面证据。
阅读时建议按 **现象 → 根因 → 守卫（代码/断言/自检）** 的顺序看。

- **重建 `study.db` 后必须刷新 `tests/db.sha256`**（`make_study_db.py` 会自动写），
  否则 `test_study_db_not_modified` 会永远失败。
- **数据生成器内有硬断言**（`make_study_db.py` 末尾）：它校验设计出来的数据确实能触发
  SOP 的阈值条件（例如雌性 150 组增重下降必须落在 10%–20% 区间内且雄性同剂量组
  必须 >90%）。改了效应量后如果断言失败，说明这个"陷阱"已经失效，不要放行。
- **`findings` 的剂量口径**：`dose_mg_kg_day` 填该发现**首次**被判定为受试物相关的剂量，
  且**按性别分别判断**。体重增重抑制在雌性 150 组首次达成有害标准、在雄性 500 组才达成，
  因此正确做法是两条记录（150/female 与 500/male）；写成 500/both 也接受
  （判分器允许 `sex_allowed_extra`），但 150/both 会被判错。
- **恢复期数据不得与主试验合并**：SOP 1.3 明确要求。把恢复期动物算进组均值，
  会同时污染增重与病理发生率。
- **Windows 上别用 `tempfile.mkdtemp` 建工作目录**：某些受限沙箱会拒绝写入
  mkdtemp 创建的目录，`eval/common.py` 因此改用 `uuid + os.makedirs`。
- **快照镜像不删**：`tox/<slug>:solved-<run-id>` 是 Phase 3 唯一的判分输入，
  也是 Phase 4 回查的依据。重跑同一 `--run-id` 会**覆盖**该 tag，使上次快照失效
  （想留档就换一个 run-id）。积累多了按前缀手工清理，不要写进流水线里自动删。
- **mini 的容器回收在 Windows 上不生效**：`DockerEnvironment.cleanup()` 用
  `shell=True` 执行 `(timeout 60 docker stop X || docker rm -f X) >/dev/null 2>&1 &`，
  cmd.exe 无法解析（只会打印 `The system cannot find the path specified.`），
  且 `__del__` 会在 GC 时再触发一次。`phase2_agent.py` 因此自己 `stop` + `rm -f`，
  再把 `env.container_id` 置空，让 `cleanup()` 与 `__del__` 都变成空操作。
- **`test.sh` 永远 `exit 0`**：它把结果写进 `/logs/verifier/reward.txt` 后无条件成功返回。
  所以 `test_sh_exit` **不能当通过信号**，唯一判据是 `reward.txt` 的内容（必须是 `0` 或 `1`，
  否则 Phase 3 判为基础设施失败而不是 0 分）。
- **task1 的 `golden.json` 由人工维护**：`make_study_db.py` 只写 `db.sha256`；
  改数据后必须手工同步 `golden.json`，第 5 节的变异解自检是发现漂移的主要守卫。
  （task2 的 `tk_golden.json` **有**生成器 `build_golden.py`，重建数据后接着跑它即可。）
- **`extract_payload()` 按"文中第一次出现 heredoc 标记"切参考解负载**：
  所以 `solution/solve.sh` 里**不能在其他地方（包括注释）再写出那个标记**，
  否则会切错位置、报出难以定位的 `SyntaxError`。task2 移植时曾出现过一次。
- **task2 的 `solve.sh` 认 `APP_DIR` 环境变量，task1 也认**：新增题目的参考解若硬编码
  `/app`，`--local` 冒烟会往真实路径写文件而不是临时目录 —— 必须从环境变量取。
- **Harbor 会静默忽略 `[phases]` / `[resources]`**：这两个是自建 harness 读的字段。
  只填它们就去 `harbor run`，Harbor 会按默认值跑 —— 而默认是 **agent 不限时、判分 600s**。
  真跑模型前必须把超时口径补到 `[agent].timeout_sec` / `[verifier].timeout_sec` /
  `[environment].build_timeout_sec`，并保证两套值一致（`task.toml` 里已用注释标出）。
- **Harbor 的 `[task].name` 必须是 `org/name` 形式**：它被解析成 `PackageInfo` 的包标识，
  与顶层给人看的中文 `name` 用途不同。少了斜杠会在任务加载阶段直接失败。
- **两套 harness 的镜像命名不同**：Harbor 按 Dockerfile 构建 `hb__<hash>`，自建 Phase 1
  构建 `tox/<slug>:base`，Phase 2 再 commit 出 `tox/<slug>:solved-<run-id>`。
  清理镜像时不要只按 `tox/` 前缀找，否则 Harbor 的构建缓存会越积越多。
- **`jobs/` 是 Harbor 的默认输出目录**：位于**当前工作目录**下（可用 `--jobs-dir` 改），
  里面是每次 trial 的日志、判分产物与交付物快照。它与 `eval/runs/` 同级同性质，已在
  `.gitignore` 中忽略；在别的目录跑 Harbor 会再生成一份，注意别误提交。

---

## 8. 后续工作

**已完成**

- [x] **判分器可信度**：两题均通过 oracle（`reward = 1`）/ base（`reward = 0`）对照，
      以及变异解自检（task1 10/10、task2 8/8 被抓住）。
- [x] **增加第 2 题（TK 参数计算）**，让 bench 覆盖"判定类"与"计算类"两种题型
      —— 2026-10 已从独立原型仓库移植为 task2。
- [x] **接入 Harbor 标准任务包格式**：`task.toml` 的原生字段 + `harbor-example.yaml`
      可直接 `harbor run`，与自建四阶段共享同一份 `tasks/`。
- [x] **跨 run 可比性守卫**：Phase 4 自动比对 `task_fingerprint`，题面/SOP/数据一变就告警。

**后续计划**

- [ ] **扩展模型覆盖**：在多厂商、多规模的模型上运行标准评测，形成可比较的基线。
- [ ] **两条评测路径的一致性基线**：用 Harbor 与自建四阶段各跑一次 oracle/nop，
      确认同一题在两条路径上的 reward 与用例结果完全一致。
- [ ] **接入 CI**：把变异解自检、oracle/base 对照与 `test_agent_wiring.py`
      接入持续集成，每次改动自动回归。
- [ ] **持续补充题目**：按现有题目结构新增任务包，扩大题型覆盖。

---

## 9. 红线

本仓库的数据与题目均为**用生成器合成**的模拟研究：

- task1：`build-tools/repeat-dose-noael-judgment/make_study_db.py`
- task2：`build-tools/clinical-tk-parameter-analysis/make_study_db.py`

不包含任何真实试验数据、第三方委托数据或内部 SOP。请勿把来源受限的题目包、
真实试验数据或内部材料上传到公开仓库 —— 这是保密与知识产权问题，与题目质量无关。
