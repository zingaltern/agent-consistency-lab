# agent-consistency-lab

[![ci](https://github.com/zingaltern/agent-consistency-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/zingaltern/agent-consistency-lab/actions/workflows/ci.yml)

**面向崩溃一致性与治理语义的 Agent runtime 实验台。**

它不是一个 Agent 应用，也不是 LangGraph 的替代品。它是一个**被测对象 + 测量仪器**：
用自研的精简内核把"崩溃恢复、审批绑定、幂等边界、上下文压缩"这些语义真正做出来，
再用命名的崩溃注入点和四条技术路线的对照实验，把它们**测出来**。

> 一句话：它不负责让模型更聪明，而是负责让模型犯错、进程崩溃、成本超支的时候，
> 系统仍然**可控、可查、可复算**。

* **可控**：不可逆操作一律停在审批门，审批精确绑定到"这一次调用 + 这一组参数"；
* **可查**：崩溃后由**外部副作用账本**（不是程序自己的日志）裁决副作用发生了几次；
* **可复算**：文档里被引用的每个数字都绑定一条再生命令，改了代码忘了改文档，CI 会红。

不需要 API key 就能跑：默认使用确定性脚本模型；真实模型接入是可选的。

---

## 它能帮你回答什么

如果你在做 Agent 落地，下面这四个问题大概都躲不开——本仓库就是为它们准备的：

| 你会遇到的问题 | 这个仓库给出的东西 |
|---|---|
| 不可逆操作不敢交给模型 | 审批闸门 + 幂等键 + 参数级安全域；一套可移植的"把决定权交给人"的机制 |
| 长任务跑一半崩了，不知道做过没有 | 事务性 outbox + 探针对账：崩溃后不盲重试，收敛为"可探测的重放"或"一行待对账的未知" |
| token 与成本说不清花在哪 | 分桶预算账本 + 前缀缓存模型；卸载 / 压缩 / 缓存纪律的量化取舍 |
| "我们的 Agent 更安全"没法证明 | 64 个合成故障场景 × 4 条技术路线 × 2 类推理人格的消融评测，结论以配对差值 + 置信区间报告 |

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/python -m examples.ops_demo     # 一条命令跑通：告警 → 取证 → 审批 → 执行 → 回放
.venv/bin/pytest -q                       # 全部用例
```

`ops_demo` 会现场给你看两件事：一次完整处置流程的全链路时间线，以及同一场景下
"称职值班人 vs 橡皮图章"的对照——**程序完全一样，差别只在有没有人把关**。

## 亲手制造一次崩溃（五分钟）

这是理解本仓库最快的方式：同一个位置被真 `kill -9`，唯一变量是保护开关。

```bash
# ① 跑到审批门
.venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode run --tool-idem off
# ② 人工批准
.venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode approve --tool-idem off
# ③ 在"效果已发生、记录还没落盘"的瞬间强杀自己
CHAOS_WINDOWS="post_tool_effect_pre_record:1" \
    .venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode resume --tool-idem off
# ④ 恢复，然后看外部账本里这件事发生了几次
.venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode resume --tool-idem off
sqlite3 /tmp/demo/world.db "select count(*) from effects;"
```

默认（保护全开）账本是 **1**；把 `--outbox off` 加上再跑一遍，同一位置会变成 **2**。
`/tmp/demo/crash_marker.json` 能证明它确实死在指定窗口，而不是提前退出了。

## 核心设计（三条约束）

完整语义见 [`docs/semantics.md`](docs/semantics.md)——它是运行时的**承诺清单**，
代码、测试与结论都以它为准。

1. **事件日志是唯一权威**：扁平 append-only（`(branch_id, seq)`），历史改写由 SQLite
   触发器物理禁止；状态永远是日志的折叠物，不做第二份权威状态。
2. **TREE_NODE 与 ARTIFACT 分离**：只有对话节点进模型视图；预算、租约、审批、压缩等
   控制事实一律为 ARTIFACT，禁止混入树。
3. **checkpoint 与 writes 分离**：任务完成即写 writes，checkpoint 只在 super-step 边界提交；
   恢复 = 最新快照 + 重写记录（不重跑副作用）+ 未完成任务重跑。

```
┌──────────────────────────────────────────────────────────────┐
│ 评测层 opsenv/      64 个合成故障 · 四条路线对照 · 配对统计 · 门禁   │
├──────────────────────────────────────────────────────────────┤
│ 可观测 trace.py     事件日志 → span 树投影（OTel 形状导出）        │
├──────────────────────────────────────────────────────────────┤
│ 治理层             审批绑定 · 幂等边界 · 上下文工程 · 预算账本       │
├──────────────────────────────────────────────────────────────┤
│ 内核 harness/      事件日志（唯一权威）· checkpoint/writes · outbox │
└──────────────────────────────────────────────────────────────┘
```

## 已产出的实测结论

每个数字都能用 [`如何复现全部结论`](#如何复现全部结论) 里的命令重算；
下列数字同时受 `scripts/check_facts.py` 的对账门禁保护。

**W2**（[w2-crash-windows.md](docs/w2-crash-windows.md)）：
"效果已发生、记录未落盘"是唯一会产生重复副作用的窗口——下游不幂等时 5/5 复现，
且 runtime 侧的去重开关在该窗口**完全无效**。

**W3**（[w3-report.md](docs/w3-report.md)，16 格 × 5 次 = 80 次运行，其中 **70 次真实 SIGKILL**）：
加上 outbox（副作用前预写意图）+ 按键读回后，同一窗口的重复副作用 5/5 → **0/5**；
没有读回能力时收敛为**恰好 1 行 unknown** 交人工对账；批准后、执行前改写参数
5/5 被拒绝执行（TOCTOU 护栏）；改参批准会生成新调用与新幂等键。

**W4**（[w4-report.md](docs/w4-report.md)，同一条长任务 10 次调用）：

* **缓存纪律值 2 倍成本**：运行时信息进前缀 ⇒ 命中率 57.4% → 0%、成本/调用 +105.1%；
* **卸载是唯一"纯赚"的杠杆**：token/调用 -66%、成本/调用 -61%，且不依赖模型调用
  （内联侧是**没跑完那次的截断期均值**，口径见 [w4-report.md](docs/w4-report.md)，不能读成"更贵"）；
* **压缩是拿钱买可用性**：能跑完（3 次压缩、0 溢出），但总成本/调用 $0.01436
  （主桶 $0.01078431 + 压缩桶 $0.00357420；两个分量各自取整后相加会比总额少 $0.00001，
  这是四舍五入的末位差，不是两个数对不上）反而最高，命中率只有 35.0%——每次压缩都从替换点击穿前缀缓存。

**W5**（[w5-report.md](docs/w5-report.md)，64 场景 × 4 系统 × 2 推理器 × 3 次 = 1536 次运行）：

* **取证充分性决定正确率上限**：只看两个通道的规则基线充分率 50%、正确率恰好也是 50%；
  按通道阶梯取证的路线充分率 100%，正确率随之到 competent 90.1% / weak 61.5%；
* **模型出错时，架构决定后果**：无审批门的路线在 20.8% 的 weak 场景里
  **执行了破坏性动作**（含静态拒绝列表之外的新动作）；有审批门的两条路线为 0.0%；
* **三条读证据的路线诊断完全同分**（配对 CI 恰好 [0,0]）——差异只在"出错时会不会变成事故"；
  橡皮图章消融下，有门路线的事故率回到无门水平：把关的是**人**，不是架构。

**W6**（[w6-report.md](docs/w6-report.md)，统计口径 + 门禁）：

* **"谁更准"不可区分到零差异，"谁更安全"统计显著**：诊断正确率 +0.000（95% CI [0.000, 0.000]，配对 192 组、0 个不一致对），
  红线执行率 −0.208（CI [−0.266, −0.151]，配对 192 组）；
* **代理指标要校准**：`取证充分性` 只对规则基线有效（κ=1.0），对 agent 路线恒为真、没有区分力——
  不能拿它当线上监控指标；
* **门禁自己也要被验证**：故意让机制退化（静默丢弃破坏性动作）时，CI 必须变红。

**W7**（[w7-report.md](docs/w7-report.md)，压缩阈值 6 档 × 8 变体）：
压缩买的是完成率、不是省钱——不压缩只有 33% 变体能跑完，压缩后 100% 完成但净成本 +79%
（选定档 $0.118732；主桶增量 +41%，压缩动作本身占 +38%）；阈值 0.70 / 0.85 / 0.95 成本不可区分
（三档实测完全相同：$0.118732），真正要避免的是不压缩——"早点压更安全"被数据否定。

**W9**（[fault-spectrum.md](docs/fault-spectrum.md) · [noisy-reasoner.md](docs/noisy-reasoner.md) · [model-modes.md](docs/model-modes.md) · [governance-extras.md](docs/governance-extras.md)）：

* **随机时刻 SIGKILL fuzz**：180 次注入全部落在标定窗口内，命名窗口之外 **0 条新类违例**；
* **故障谱系从 1 档扩到 4 档**：SIGTERM、torn write、SIGSTOP 悬挂、账本外注入；
* **评分口径敏感性被激活**：噪声人格下 `strict` 66.7% < `cause_only` 77.6%（此前两者恒等）；
* **变异测试常设化**：2050 个变异体 / 1332 killed / 700 survived，基线入库防"盲区扩大"；
  门禁按**全状态记账**分三类，每次运行都打印"不可见空间"的规模——**存活率不是覆盖率**；
* **模型接入三模式**：scripted / record / replay 走同一条 loop；真实模型可选接入，
  且 live 调用永不进 CI；
* **事件哈希链**：复制一份历史再改一行，离线校验器会**指出第一个断点**；
* 参数级审批、OTLP 导出、artifact 回收、单写者租约各自补齐，并写明不承诺什么。

**W10**（[docs/integrations.md](docs/integrations.md)）：把治理层接成 **MCP 工具服务**
（本地 stdio、一进程一 run，调用走同一条七步执行管线），并提供一条可复现的崩溃演示
（真强杀服务进程 → 重启 → 续跑 → 外部账本恰好一次，含对照组）。

**W11**（[docs/independent-test-2026-09-19/report.md](docs/independent-test-2026-09-19/report.md)）：
一轮外部独立验证**证伪了自己的一条语义承诺**——`INSERT OR REPLACE` 能绕过 append-only
（SQLite 的隐式 DELETE 不触发 `BEFORE DELETE`，只需普通 DML），补第三条触发器升 schema v4；
并修掉 `harness/execution.py` 里与文档相反的落盘顺序（真 SIGKILL 会留下被自家 oracle 判违规的状态），
以及门禁只断言"比率是多少"、不断言**指标定义**这个结构性盲区（新增四条**关系**门禁）。

## 里程碑

工程节点按周推进，细节在 [`docs/HANDOFF.md`](docs/HANDOFF.md)（含复盘与踩坑记录）。

| 里程碑 | 内容 |
|---|---|
| W1–W4 | 事件日志与提交协议 → 最小 loop 与崩溃注入 → 审批绑定与 outbox → 上下文工程与成本 |
| W5–W8 | 场景集与四路线对照 → 统计口径与 CI 门禁 → 阈值扫描与 trace → 长文与 demo |
| W9 | 测试强化（数字对账门禁 / fuzz / 变异基线）+ 功能扩展（三模式模型 / 参数级审批 / 哈希链 / OTLP / GC / 租约） |
| W10 | 外围集成（MCP 工具服务 + 可观测落地）+ 变异门禁判据重写 |

## 目录结构

```
harness/        运行时内核：事件日志、loop、审批、上下文、预算、trace（唯一依赖 pydantic）
opsenv/         评测层：故障场景集、四通道证据、四系统对照、统计与门禁
fakeworld/      受控世界：外部副作用账本 + 仿真工具（崩溃实验的裁判）
experiments/    实验 runner：崩溃矩阵、上下文成本、阈值扫描、随机注入 fuzz
integrations/   外围集成（可选装）：MCP 工具服务、崩溃演示、本地 OTLP 接收器
scripts/        工具脚本：数字对账、用例计数、变异门禁、谱系探测器
tests/          测试：不变量、协议、审批语义、审计回归、集成
docs/           语义承诺清单、各轮报告、长文、设计文档、规范
reports/        自动生成的报告表格与汇总 JSON（逐次明细由 --runs-out 再生，不入库）
```

## 文档索引

| 想了解什么 | 读哪份 |
|---|---|
| 运行时承诺清单（语义，**先读这份**） | [docs/semantics.md](docs/semantics.md) |
| 项目状态、复盘与已知边界 | [docs/HANDOFF.md](docs/HANDOFF.md) |
| 各轮实验报告 | [w2](docs/w2-crash-windows.md) · [w3](docs/w3-report.md) · [w4](docs/w4-report.md) · [w5](docs/w5-report.md) · [w6](docs/w6-report.md) · [w7](docs/w7-report.md) |
| 三篇长文（崩溃语义 / 审批绑定 / 缓存会计） | [docs/posts/](docs/posts/) |
| 故障注入谱系与随机注入 | [docs/fault-spectrum.md](docs/fault-spectrum.md) |
| 模型接入三模式 | [docs/model-modes.md](docs/model-modes.md) |
| 外围集成（MCP / 可观测） | [docs/integrations.md](docs/integrations.md) |
| 独立验证报告（外部黑盒测试） | [docs/independent-test-2026-09-16/](docs/independent-test-2026-09-16/) · [docs/independent-test-2026-09-18/](docs/independent-test-2026-09-18/) · [docs/independent-test-2026-09-19/](docs/independent-test-2026-09-19/)（本轮：append-only 的 INSERT 旁路 + 落盘顺序 + 门禁指标定义覆盖面） |
| 开发规范 / 测试规范 | [docs/development.md](docs/development.md) · [docs/testing.md](docs/testing.md) |
| 给 AI agent 的入口约束 | [AGENTS.md](AGENTS.md) |

## 如何复现全部结论

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,eval]"

.venv/bin/pytest -q                       # 全部用例（用例数不手写：claim tests-collected
                                          # 由 scripts/count_tests.py 再生）
.venv/bin/ruff check .                    # lint

.venv/bin/python -m experiments.crash_matrix --repeats 5 \
    --json-out reports/w4_crash_matrix.json --md-out reports/w4_crash_matrix.md
    # → 崩溃窗口矩阵：重复副作用只由下游幂等 / outbox 决定

.venv/bin/python -m experiments.context_cost \
    --md-out reports/w4_context_cost.md --json-out reports/w4_context_cost.json
    # → 缓存纪律 / 卸载 / 压缩 三组对照

.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate \
    --md-out reports/w6_eval.md --json-out reports/w6_eval.json
    # → 四系统 × 64 场景 × 2 推理人格；--gate 任一条不通过则非零退出

.venv/bin/python -m experiments.context_sweep --repeats 2 \
    --md-out reports/w7_sweep.md --json-out reports/w7_sweep.json --svg-out reports/w7_sweep.svg
    # → 压缩阈值 → 完成率 / 缓存命中 / 净成本 三维曲线

# 文档里的数字还对不对（claim 对账；CI job `facts` 跑轻集）
.venv/bin/python scripts/check_facts.py --run verify
.venv/bin/python scripts/mutation_check.py             # 变异门禁：新增盲区即红
.venv/bin/python -m experiments.chaos_fuzz --repeats 30 --seed 20260917   # 随机时刻 fuzz
.venv/bin/python -m harness.trace --run-dir <run 目录>   # 排障：事件日志 → span 树
```

## 边界（本结论不适用的范围）

* 崩溃实验只对 **kill -9** 语义成立；掉电语义（`synchronous=NORMAL` 下可能丢最后一个事务）
  不在范围内，见语义文档 §3。
* 场景与推理人格都是**合成**的，没有真实流量；结论以"相对基线的配对差值 + 置信区间"报告，
  **不以绝对值宣称达标**，本仓库也不出现"生产级"这类措辞。
* 小样本（如每格 n=5 的 0/5）只把失败率上界压到约 60%（rule of three），**不能**读成"永不重复"。
* 存储层是单写者模型：租约是"入场条件"，不是并发承诺，跨进程同写的后果仍由外部账本裁决。
* fuzz 的"未发现新类违例"同样只压上界，**不能**读成"覆盖了所有窗口"。
* `integrations/` 的 MCP 服务是**本地单用户**形态：不承诺并发、多用户、远程访问与断线重连时序，
  也不承诺性能数字。

## 参与开发（人类或 AI agent）

* [`AGENTS.md`](AGENTS.md)：所有 agent 会话的入口约束（红线清单 + 最短工作流）；
* [`docs/development.md`](docs/development.md)：分支流程、合并门槛、代码与文档纪律；
* [`docs/testing.md`](docs/testing.md)：必跑命令、改动→测试义务、自证作弊清单。

规矩只有一条：**分支开发，全绿验证，才允许合并进 `main`**；文档里的每个数字都要能被命令再生。

## License

MIT，见 [LICENSE](LICENSE)。
