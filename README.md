# tox-bench

面向**临床前毒理**场景的大模型 Coding 评测任务包：把一项 28 天重复给药毒性试验的原始数据
装进 Docker 沙箱，让 agent 依据机构 SOP 判定 **NOAEL**，再用**与 agent 完全隔离**的自动判分器
给出可复现的 reward。

> 这是"专业领域出题 + 环境搭建 + 验证脚本 + 参考答案"的一次完整实践：
> 题目来自真实科研/工业场景，判定标准客观可执行，难度经过变异解自检确认。

---

## 1. 仓库结构

```
tox-bench/
├── README.md                          ← 本文件
├── tasks/
│   └── repeat-dose-noael-judgment/    ← 可提交的任务包，一题一目录
│       ├── instruction.md             ← 给 agent 看的题面
│       ├── task.toml                  ← 元数据 + 各阶段超时与资源
│       ├── environment/               ← 构建上下文（只含数据与 SOP）
│       │   ├── Dockerfile
│       │   ├── SOP_toxicity_criteria.md   ← 容器内的判定标准，题面不重复其规则
│       │   └── study.db               ← 生成物，随仓库提交
│       ├── tests/                     ← 判分器，**不进镜像**
│       │   ├── test.sh
│       │   ├── test_outputs.py
│       │   ├── golden.json
│       │   └── db.sha256
│       └── solution/
│           └── solve.sh               ← 参考解（全部数值由 study.db 计算得出）
├── build-tools/                       ← 作者侧工具，**不进入镜像、不随任务包提交**
│   ├── make_study_db.py               ← 生成研究数据（含设计的效应量 = 部分真值）
│   ├── check_discrimination.py        ← 变异解自检
│   └── test_agent_wiring.py           ← agent 链路自检（不花钱、不需要 Docker）
└── eval/
    ├── run_task.py                    ← 评测驱动：oracle / base（证明判分器可信）
    ├── agent.py                       ← 用 mini-swe-agent 跑模型（测量模型能力）
    ├── mini_config.yaml               ← mini-swe-agent 的最小配置覆盖
    └── runs/<run-id>/                 ← 每次模型运行的轨迹、交付物、判分产物
```

**`build-tools/` 刻意放在 `tasks/` 之外**：`make_study_db.py` 里写着每个剂量组的效应量，
拿到它就能反推大部分"真值结论"。让"不提交"由**目录结构**保证，而不是靠文档里一句口头约定：
`environment/Dockerfile` 只 `COPY study.db` 与 SOP，因此这些文件既不会进镜像，也不在任务包里。

---

## 2. 快速开始

```bash
# ① 生成研究数据（改了生成器才需要重跑；会同时刷新 tests/db.sha256）
python build-tools/make_study_db.py

# ② 参考解 → 判分（期望 reward = 1）
python eval/run_task.py --mode oracle

# ③ 空基线（不注入参考解）→ 判分（期望 reward = 0）
python eval/run_task.py --mode base

# ④ 变异解自检：10 个"看似合理但违反 SOP"的答案必须全部被抓住
python build-tools/check_discrimination.py

# ⑤ agent 链路自检（不花钱、不需要 Docker，用 DeterministicModel 跑通接线）
python build-tools/test_agent_wiring.py
```

前置依赖：

| 用途 | 需要什么 |
|---|---|
| 跑判分器 / 前四步 | Python 3.11+、`numpy`、`pandas`（`pytest` 可选） |
| 起容器（`--runner docker`） | Docker 引擎可用 |
| 测模型（`--mode agent`） | `mini-swe-agent`（已在 2.2.8 上验证）、模型 API key |

`eval/run_task.py` 有两种 runner：

| runner | 依赖 | 用途 |
|---|---|---|
| `--runner local`（默认） | 只需本机 Python + numpy/pandas | 开发机冒烟：验证判分器能否区分正确解与错误解 |
| `--runner docker` | Docker 引擎 | 正式：`docker build` 基础镜像 → 起容器 → 注入参考解 → 注入 tests 判分 |

`local` runner 不依赖 pytest（缺失时用内置 runner 逐条调用 `test_*` 函数），
因此可以在任何装了 numpy/pandas 的机器上做冒烟验证。

Docker 单题全流程：

```bash
cd tasks/repeat-dose-noael-judgment
docker build -t tox/noael-judgment:base environment

# 起一个任务容器（真实评测时由平台完成）
docker run -d --name tox-run tox/noael-judgment:base sleep 1800

# 参考解
docker cp solution/solve.sh tox-run:/tmp/solve.sh
docker exec tox-run bash /tmp/solve.sh

# 判分（agent 结束之后才注入 tests）
docker exec tox-run mkdir -p /app/tests
docker cp tests/. tox-run:/app/tests
docker exec tox-run bash /app/tests/test.sh
docker exec tox-run cat /logs/verifier/reward.txt
```

---

## 3. 题目考什么

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

### 设计取舍：规则放在 SOP 里，题面不重复

题面只给**输入说明、交付格式、约束与完成标准**，判定阈值（≥10% 增重抑制、≥5% 摄食下降、
>30% 对照发生率、分级 ≥2 级等）全部写在容器内的 `SOP_toxicity_criteria.md`。

这样做有两个好处：

1. **贴近真实工作**：专题负责人本来就是"按机构 SOP 判定"，而不是凭个人经验；
2. **保住区分度**：如果题面把 7 类陷阱连同处理规则全部交代清楚，任务会退化成"规格转写"
   —— 模型只需照抄，难度和真实专业判断无关。规则在 SOP 里，模型必须**主动读取并正确套用**，
   同时还要自己算出统计量（组均值、相对变化、发生率、历史范围比较）。

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

判分器**不 import pytest**、不使用 fixture，因此既能被 pytest 收集，也能被内置 runner 直接调用。

---

## 5. 改题之后必跑的三道验证

改数据、题面、SOP 或判分器之后，**三道都要跑**，缺任何一道都可能让缺陷溜过去：

| # | 验证 | 命令 | 期望 |
|---|---|---|---|
| 1 | 变异解自检 | `python build-tools/check_discrimination.py` | 10/10 被抓住，无漏网 |
| 2 | oracle 对照 | `python eval/run_task.py --mode oracle` | `reward = 1` |
| 3 | base 对照 | `python eval/run_task.py --mode base` | `reward = 0` |

第 1 道的必要性：oracle 与判分器出自同一套认知，它们只能证明"彼此一致"。
**变异解才是真正在问"判分器会不会被似是而非的答案骗过去"** —— 上表的 10 个变异解，
每一个都对应一种模型很容易犯的专业判断错误。

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
- 走 litellm，DeepSeek / OpenAI / Anthropic 换模型只改一个 `--model` 字符串。
- **不覆盖上游 prompt**：`eval/mini_config.yaml` 只改环境超时，`system_template` /
  `instance_template` 一律沿用上游默认——上游模板里带着 bash heredoc / sed / nl 的示例，
  换掉它，测出来的就变成"模型熟不熟这套 prompt"，而不是"模型的毒理判定能力"。
  本次运行的**事实性约定**（交付物路径、结束命令）以 `harness_note` 追加在题面之后。

### 6.2 跑之前：先确认地基

```powershell
python eval\run_task.py --mode oracle --runner docker   # 必须 = 1
python eval\run_task.py --mode base   --runner docker   # 必须 = 0
python eval\run_task.py --mode agent  --runner docker --model deepseek/deepseek-chat --run-id flash-1
```

**顺序不能颠倒**：如果 oracle/base 不对，判分器本身就是坏的，此时 agent 跑出来的 0 分
你无法区分是"模型弱"还是"判分器/参考解有 bug"。详见第 5 节。

### 6.3 每次运行产出什么

```
eval/runs/<run-id>/
├── trajectory.json    ← mini 的完整轨迹（每步的思考、命令、观测、成本）
├── run.json           ← 本次元数据：reward / 步数 / 成本 / 耗时 / exit_status / task_fingerprint
├── judgment.json      ← 模型交上来的交付物快照
├── report.md          ← 同上
├── ctrf.json          ← 逐用例通过状态（哪个陷阱没判对）
└── verify.log         ← 判分过程日志
```

`task_fingerprint` 是题面 + SOP + 数据的联合哈希：**两次 run 的指纹不同就说明题目变了，
分数不可直接比较**——这是做难度标定最容易踩的坑。

### 6.4 判分的隔离方式

`--verify-mode snapshot`（默认）：agent 跑完 → `docker commit` 快照 → 从快照起干净容器
→ 这时才注入 `tests/` → 判分。agent 全程看不到断言。
`--verify-mode inplace` 直接在 agent 容器里判分，快，但不体现隔离。

---

## 7. 已知的坑

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
  mkdtemp 创建的目录，`eval/run_task.py` 因此改用 `uuid + os.makedirs`。

---

## 8. 待办（下一步的标定工作）

- [ ] **难度标定**：本题专家约 4 小时，需要真实跑 2–3 个模型，记录通过率、步数与成本。
- [ ] 若模型通过率过高（题目变成"规格转写"），可考虑：把 SOP 的阈值改为需要从历史数据
      自行推断，或增加一个"统计显著但生物学无意义"的反向陷阱。
- [ ] 增加第 2 题（例如重复给药试验的**恢复期可逆性报告**或**TK 参数计算**），
      让 bench 覆盖"判定类"与"计算类"两种题型。

---

## 9. 红线

本仓库的数据与题目均为**用生成器合成**的模拟研究（`build-tools/make_study_db.py`），
不包含任何真实委托方的数据。请勿把平台/雇主下发的题目包、真实试验数据、内部 SOP
或判分器上传到公开仓库 —— 那是保密与知识产权问题，与题目质量无关。
