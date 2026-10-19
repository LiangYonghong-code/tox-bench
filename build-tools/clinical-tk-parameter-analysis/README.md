# 构建与自检工具（不进入任务包，也不进入镜像）

本目录服务于 `tasks/clinical-tk-parameter-analysis/`（task2）。所有脚本只在**作者侧**运行。

放在 `tasks/` 之外的仓库根 `build-tools/` 下，是为了避免整包提交时把真值模型参数和
标准解一起交出去：`make_study_db.py` 暴露生成数据用的真值 PK 模型（拿到它就能反推所有
"真值"），`build_golden.py` 等价于一份完整标准解。

`environment/Dockerfile` 只 `COPY study.db`，因此这些脚本本身也不会出现在被测
Agent 能看到的容器里。

> **目录名的约定**：本目录名必须与 `eval/tasks.yaml` 里该题的 `dir` 末段一致。
> 所有脚本都用 `os.path.basename(__file__ 的父目录)` 反推自己服务哪道题，
> 因此加一道题 = 加一个同名目录，不需要改任何路径常量。

## 文件

| 文件 | 作用 |
|---|---|
| `make_study_db.py` | 生成 `environment/study.db`，并顺手刷新 `tests/db.sha256` |
| `build_golden.py` | 从 study.db 生成金标准 `tests/tk_golden.json`（numpy 实现） |
| `check_discrimination.py` | 变异解自检：9 个变异实现逐个喂给真实判分器 |

## 重建流程

```bash
# 在仓库根目录执行
TASK=tasks/clinical-tk-parameter-analysis
TOOLS=build-tools/clinical-tk-parameter-analysis

# 1. 重建数据库（会覆盖 environment/study.db，并刷新 tests/db.sha256）
python $TOOLS/make_study_db.py

# 2. 重建金标准（依赖上一步的产物；两个参数都可省略，默认按目录位置推导）
python $TOOLS/build_golden.py
```

两步都是确定性的，不含随机数：任何人任何时候重建，得到的 `study.db` 与
`tk_golden.json` 都逐字段一致（已实测校验）。

> ⚠️ **重建 `study.db` 后必须接着重建金标准**，否则判分器会拿旧金标准比对新数据。
>
> `tests/db.sha256` 由 `make_study_db.py` 自动刷新，**不需要**再手工改判分器
> —— 那个哈希以前硬编码在 `test_outputs.py` 里，是"重建后必然忘记同步"的常见问题，
> 本次移植时已外移到 `tests/db.sha256`。

## 改完必跑的三道验证

任何一次改动（数据、题面、判分器、任一份实现）之后，三道都要跑，
**缺任何一道都可能让缺陷被漏掉**——2026-10 的一次判分偏差就发生在只跑前两道验证时：

| # | 验证 | 命令 | 期望 | 能发现什么 |
|---|---|---|---|---|
| 1 | 变异解自检 | `python $TOOLS/check_discrimination.py` | 全绿 | 多个缺陷互相掩盖；某条规则未被独立考到 |
| 2 | oracle / base 两侧对照 | `python eval/phase3_verify.py --run-id smoke --only 2 --local --oracle` 与 `--local` | `reward=1` / `reward=0` | 判分器太严（正确解也挂）或太松（空解也过） |
| 3 | **模型侧验证** | `python eval/phase2_agent.py --run-id <id> --only 2` + `phase3_verify.py` | agent 按题面实现应当 `reward=1` | **实现集体偏离题面**——前两道无法发现 |

三道都用 `--local`（无 Docker）即可，秒级出结果。需要**全保真**时去掉 `--local`：

```powershell
python eval/phase3_verify.py --run-id main --only 2 --oracle   # 期望 reward = 1
python eval/phase3_verify.py --run-id main --only 2 --base     # 期望 reward = 0
```

第 3 道的必要性：前两道验证的参与者（金标准、oracle、自检基线）全部出自同一套
规则理解。它们能证明"彼此一致"，证明不了"与题面一致"。只有让一个**没读过实现、
只读过题面**的 agent 去解题，才能发现规格与实现之间的裂痕。

代价也很低：单题一次运行通常在分钟级，成本可控。

## 变异解自检（每次改数据或改判分器后都该跑）

```bash
python $TOOLS/check_discrimination.py
```

它会构造 8 个"只破坏一条清洗规则、其余全部正确"的变异解（外加一个正确实现），
逐个喂给**真实的** `tests/test_outputs.py`，断言每个都必须被判失败；同时断言正确
实现与 `tk_golden.json` 逐字段一致。退出码 0 表示每条规则都被独立捕获。

这个脚本存在的意义：陷阱题最容易出现的问题是多个缺陷互相掩盖——漏掉规则 A 的解法
因为恰好做对了规则 B 而通过全部用例，仅靠审阅题面难以发现。**第一次审计（2026-10）**
就发现过一例：被拒 run 的 `run_id` 原本小于被接受的 run，导致"完全不看 accepted 标志、
只按 run_id 取最大"的解法仍然通过，规则 1 实际从未被独立考到。修复方式是把被拒
复测 run 的 `run_id` 排到被接受 run 之后（真实场景同样成立：QC 复核时拒收复测）。

第二次审计（2026-10）暴露的是另一半问题：**自检脚本本身也可能是错的一方**。
本目录内嵌的"正确实现"曾与 `build_golden.py`、`solution/solve.sh` 一起，把 AUMC 的
`t × C` 喂进了"线性上升 / 对数下降"的复合函数，而题面明确要求 AUMC 走纯线性。
于是三份实现彼此一致、一起偏离题面：

- 自检全绿（正确实现与金标准"逐字段一致"，因为它们错得一样）
- oracle 对照 16/16 全绿、base 对照 0/16 全绿，两侧都符合预期
- **题面与实现的矛盾完全隐形**

只有让真实 agent 按题面解题才暴露出来——它严格实现了纯线性，反而被判失败
（animal 101 的 `mrt_h` 偏差 1.144%）。修复方式：三处各拆出纯线性函数专供 AUMC、
重生成金标准，并新增硬编码锚点 `test_aumc_uses_pure_linear_trapezoid` 与变异解 `aumc_log`。

**方法论结论**：变异解自检能发现"多个缺陷互相掩盖"，但它无法发现"**自检者和被判者
共享同一个错误理解**"。这类缺陷只能靠外部独立实现（行业标准软件或真实 agent）暴露。

## 几份实现，以及它们为什么还不够独立

本题存在**三份**同一套计算规则的实现：

| | `build_golden.py` | `solution/solve.sh` | `check_discrimination.py` |
|---|---|---|---|
| 位置 | `build-tools/` | 任务包 `solution/` | `build-tools/` |
| 依赖 | numpy 向量化 | 纯标准库 | 纯标准库 |
| 回归 | 手写最小二乘（numpy） | 手写最小二乘（纯 Python） | 手写最小二乘（纯 Python） |
| 角色 | 金标准（oracle） | 参考解 | 自检的"正确实现"基线 |

前两者是**独立编码的两条路径**，互为交叉验证；`tests/test_outputs.py` 里另有若干
硬编码的人工核对锚点，用来挡住"几份实现共享同一个理解错误"的情况。
第三份是自检脚本内嵌的基线，用于判定"变异解是否真的被捕获"。

⚠️ **但交叉验证的强度取决于参与者的独立性，不取决于份数。**
2026-10 的判分偏差正是一个教训：三份实现出自同一作者、同一套规则理解，
于是三份一起错、彼此印证、自检全绿，而题面被集体违背。
**多写一份同源实现不会增加独立性——只会增加"看起来很可靠"的假象。**

真实项目里，oracle 这个角色应当由 **Phoenix WinNonlin / PKNCA** 等经过验证的行业标准
软件的输出来承担——那是与解题代码完全无关的第二个系统。此外还应当补一道
**模型侧验证**：让真实 agent 只按题面解题，看它能否通过。这次的 AUMC 缺陷
就是被模型侧验证抓出来的（agent 严格照题面实现，反而被判失败）。

## 数据集里埋了什么

6 类数据质量问题，每类都有对应的定向测试（偏差为实测值）：

| # | 动物 | 埋入的问题 | 若处理错误的后果 |
|---|---|---|---|
| 1 | 101 | 1h 有两条 accepted=1 的重复 run | AUC0-t 偏高 2.88% |
| 2 | 201 | 1h 复测 run 被拒收且 run_id 更大 | Cmax 偏高 240.2% |
| 3 | 全部 | 首末时点 BLQ | 202 的 AUC0-t 偏高 8.71% |
| 4 | 301 | 2h 实际 3.1h 采样，超窗 | AUC0-t 偏高 6.74% |
| 5 | 302 | 实际剂量 38 vs 名义 50 mg/kg | 该动物本不该出现 |
| 6 | 102 | >=6h 样本缺失 → 外推 76.8% | 4 个字段本应留空 |

## 移植到 tox-bench 时改了什么

本题最初来自一个独立原型仓库（旧版 harness 约定），
2026-10 移植进 `tox-bench` 时做了这些对齐（均为 harness 层面，**未改动数据、
题面与判分口径**）：

- `test_outputs.py`：`/app` 由 `TASK_APP_DIR` 环境变量决定，本机 `--local` 冒烟才跑得起来
- `DB_SHA256`：从判分器常量外移到 `tests/db.sha256`，由 `make_study_db.py` 自动刷新
- `test.sh`：改为"永远 `exit 0`，结果只写 `$LOG_DIR/reward.txt`"（唯一判据）
- `solve.sh`：认 `APP_DIR` 环境变量；heredoc 标记改为 `PY`
- `task.toml`：换成 `[task]/[environment]/[tests]/[phases]/[resources]` 新 schema
- `check_traps.py` → `check_discrimination.py`：改走 `eval/common.py` 的判分原语，
  按目录名选题（与 task1 的同名脚本约定一致）
- `build_study_db.py` → `make_study_db.py`：默认写进任务包并刷新 `db.sha256`
- 删掉了 `log/standard_solution.sh`（`solution/solve.sh` 的副本，tox-bench 无此约定，
  留着只会造成实现漂移）

源仓库 `eval/runs/` 下的历史运行与审计记录已原样归档到本地归档目录，
作为上文两次判分偏差的原始证据。
注意它们出自**旧版 harness**（`summary.json` 是裸数组、无 `task_fingerprint`、
镜像名是 `tbx/...`），因此不会被 Phase 4 认作可比基线 —— 只是留档。
确认不需要时可以整个目录删掉。
