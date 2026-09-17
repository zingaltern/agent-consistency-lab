# agent-consistency-lab

[![ci](https://github.com/zingaltern/agent-consistency-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/zingaltern/agent-consistency-lab/actions/workflows/ci.yml)

面向**崩溃一致性与治理语义**的 Agent runtime 实验台。

不是一个"Agent 应用"，也不是 LangGraph 的替代品。它是一个**被测对象 + 测量仪器**：
用精简自研内核把"崩溃恢复、审批绑定、幂等边界、上下文压缩"这些语义做出来，
用命名的崩溃注入点把它们测出来，并与 LangGraph 的同任务等价图做对照。

运维故障处置（IT 运维场景）只是验证壳，可替换。

## 它长什么样

```
                    ┌─────────────────────────────────────────────┐
   scenario ────────▶│  评测层 opsenv/                              │
（64 个合成故障）      │  四通道证据 · 四条技术路线 · 配对统计 · 门禁    │
                    └──────────────┬──────────────────────────────┘
                                   │ 用公开 API 驱动
                    ┌──────────────▼──────────────────────────────┐
   tools ──────────▶│  内核 harness/                              │
   (读/写/审批)      │  loop 状态机 ── tools 注册表 ── context 视图  │
                    │       │              │            │          │
                    │       ▼              ▼            ▼          │
                    │  事件日志(权威)   权限/审批    压缩/卸载/预算    │
                    │       │                                        │
                    │       ├─▶ checkpoint/writes（快照 + 交叉校验） │
                    │       └─▶ trace 投影（span 树 / OTel 形状）    │
                    └───────────────────────────────────────────────┘
```

一条命令看它跑起来（告警 → 取证 → 审批 → 执行 → trace 回放，含消融对照）：

```bash
.venv/bin/python -m examples.ops_demo
```

## 状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| W1 | 事件日志 + state 折叠 + checkpoint/writes 提交协议 + 语义文档 v1 | 完成 |
| W2 | 最小 loop + 命名崩溃注入器 + 崩溃窗口矩阵（窗口 1-3） | 完成 |
| W3 | interrupt/resume + 审批绑定（TOCTOU）+ outbox + unknown 对账 | 完成 |
| W4 | 上下文视图 + 卸载 + 压缩 + 缓存纪律 + 预算 | 完成 |
| W5 | 运维壳 + 场景集（64 个）+ 三基线（含 LangGraph 对照） | 完成 |
| W6 | 分段汇报 + 统计口径（配对 CI）+ 门禁（含实验假设自检）+ CI | 完成 |
| W7 | 压缩阈值扫描（三维曲线）+ trace 收口 + 开源整理 | 完成 |
| W8 | 长文 ×3 + 文档 + demo | 完成 |
| W9 | 测试强化（claim 门禁 / fuzz / 噪声人格 / 变异基线）+ 功能扩展（三模式模型 / 参数级审批 / 哈希链 / OTLP / GC / 租约） | 完成 |

## 已产出的实测结论

**W2**（[w2-crash-windows.md](docs/w2-crash-windows.md)）：
"效果已发生、记录未落盘"是唯一产生重复副作用的窗口，下游不幂等时 5/5 复现，
且 runtime 侧去重开关在该窗口完全无效。

**W4**（[w4-report.md](docs/w4-report.md)，同一条长任务 10 次调用）：

* **缓存纪律值 2 倍成本**：运行时信息进前缀 ⇒ 命中率 57.4% → 0%、成本/调用 +105.1%；
* **卸载是唯一"纯赚"的杠杆**：token/调用 -66%、成本/调用 -61%，且不依赖模型调用；
* **压缩是拿钱买可用性**：能跑完（3 次压缩、0 溢出），但总成本/调用 $0.01436
  （主桶 $0.01078 + 压缩桶 $0.00357）反而最高，命中率只有 35.0%——每次压缩
  都从替换点击穿前缀缓存；
* 崩溃矩阵新增窗口 5：压缩中 SIGKILL 后引用完整、事件不重复替换、副作用仍恰好一次。

**W3**（[w3-report.md](docs/w3-report.md)，14 格 × 5 次 = 70 次运行，
其中 **65 次真实 SIGKILL**、68 次恢复运行）：

* 同一窗口加 **outbox（副作用前预写意图）+ 按键读回**：重复副作用 5/5 → **0/5**，
  恢复路径由探针确认（不重跑）；
* 没有读回能力时：0 重复，但收敛为**恰好 1 行 unknown** 待人工对账——
  即把"静默重复"变成"显式有界的未知"；
* **TOCTOU 有实测护栏**：批准之后执行之前改写参数，5/5 拒绝执行、0 副作用；
* **改参即换键**有端到端证据：改参批准 → 新 tool_call_id → 不同幂等键，账本落的是改后参数。

**W5**（[w5-report.md](docs/w5-report.md)，64 场景 × 4 系统 × 2 推理器 × 3 次 = 1536 次运行）：

* **取证充分性决定正确率上限**：只看 metrics+resources 的规则基线充分率 50%、
  正确率恰好也是 50%；按通道阶梯取证的 agent 路线充分率 100%、正确率随之到
  competent 90.1% / weak 61.5%；
* **模型出错时架构决定后果**：无审批门的单次调用基线在 20.8% 的 weak 场景里
  **执行了破坏性动作**（competent/weak 两档共 3 次静态拒绝列表之外的新动作）；
  有审批门的两条路线红线 0.0%，代价是把 54% 的出错场景交给人并被拒绝；
* **三条读证据的路线诊断完全同分**（CRN 种子修复后：90.1% / 61.5% 逐格相同，
  配对 CI 恰好 [0,0]）——**差异只在"出错时会不会变成事故"**；橡皮图章消融里
  有 gate 路线的事故率回到无门水平（4.2% / 20.8%），把关的是"人"不是"架构"；
* **两条 agent 路线能力等价**（步数 3.38 vs 3.38、token 相同、正确率与红线率逐格相同），
  差别在崩溃一致性与"工具自述风险"这两层工程属性上。

**W6**（[w6-report.md](docs/w6-report.md)，统计口径 + 门禁）：

* **"谁更准"不可区分到零差异，"谁更安全"统计显著**：CRN 种子下 harness − single_shot
  的诊断正确率 +0.000（95% CI [0.000, 0.000]，配对 192 组、0 个不一致对），
  红线执行率 −0.208（CI [−0.266, −0.151]，配对 192 组）；
* **代理指标要校准**：`取证充分性` 只对规则基线有效（κ=1.0），对 agent 路线恒为真、
  没有区分力——**不能拿它当线上监控指标**；
* **门禁自己也要被验证**：第一版门禁抓不到"静默丢弃破坏性动作"的退化（指标完美、机制已死），
  补上对称自检（有 gate 的系统必须真的拦下过东西）后，退化的实现会让 CI 变红；
* holdout 与 dev 无落差（本轮无调参），考卷留到 W7 的阈值扫描用。

**W7**（[w7-report.md](docs/w7-report.md)，阈值扫描 6 档 × 8 变体 × 2 次）：压缩买的是完成率、不是省钱——
不压缩只有 33% 变体能跑完（总成本 $0.066），压缩后 100% 完成但净成本 +79%
（$0.11834；主桶增量 +41%，压缩动作本身占 +38%——选中阈值那一行的数）；压缩次数与缓存命中率在聚合上反向、
细胞级存在非单调；**0.70 / 0.85 / 0.95 三档成本不可区分（程序按规则选中 0.70），
真正要避免的是 ≤0.50 与不压缩**——"早点压更安全"被数据否定。

**W9**（设计文档 A+B 交付，详见 [docs/fault-spectrum.md](docs/fault-spectrum.md)、
[docs/noisy-reasoner.md](docs/noisy-reasoner.md)、[docs/model-modes.md](docs/model-modes.md)、
[docs/governance-extras.md](docs/governance-extras.md)）：

* **文档里的数字从此有门禁**：72 条 claim（值 + 容差 + 再生命令 + JSON 路径 + 引用位置），
  `scripts/check_facts.py` 逐条重跑比对；verify 轻集 27/27 通过、CI job `facts`，
  整量集进 nightly。退化注入验证：把某条 claim 的值改掉 → 退出 1 并报出偏离；
* **随机时刻 SIGKILL fuzz**：180 次注入（30 seed × 3 保护组合 × 2 注入阶段）全部真的落在
  标定窗口内、**命名窗口之外 0 条新类违例**；敏感性自检证明 oracle 在已知会重复的配置上会报红；
* **故障谱系从 1 档扩到 4 档**：SIGTERM（无优雅 flush 语义）、torn write（显式失败、零新副作用）、
  SIGSTOP 悬挂（冻结期间账本只出现 {0,1} 两种取值）、账本外注入（不触发重跑）；
* **评分口径敏感性被激活**：噪声人格下 `strict` 66.7% < `cause_only` 77.6%（W5–W7 两者恒等），
  而机制门禁在噪声下**仍然全绿**（红线 0.000、写操作 100% 交人工）；
* **变异测试常设化**：1670 个变异体 / 1031 killed / 625 survived（claim `mutation-survivors`、
  `mutation-survivor-rate`；基线 `reports/mutation_baseline.json` 入库，防"盲区扩大"）；
* **模型接入三模式**：scripted / record / replay 走同一条 loop，scripted↔replay 白名单字段一致
  （token/cost/事件序列/tool_calls 结构；时间戳不比较）；live 永不进 CI（无 key 给可读失败）；
* **参数级审批**：工具自述安全域（`arg_policy`），闸门在**执行前一刻**用实际参数核对——
  堵住"审批只看了 hash、参数其实越界"这条缝隙（12 条用例）；
* **事件哈希链**：`event_hash = sha256(prev_hash ‖ record_bytes)`；mirror test 证明
  复制-篡改一行历史会被离线校验器指出第一个断点（`python -m harness.audit_chain`）；
* **OTLP / artifact GC / 租约**三处补齐，各自写明不承诺什么（默认行为零改变）。

### 独立审计发现的 P0（已修，含回归测试）

W4 后跑了一次五路只读审计（文档一致性 / 测试有效性含变异测试 / 实验可复现性 /
状态机正确性 / 工程卫生），发现的 P0 全部修复：

* **视图在审批路径自毁配对**：`interrupt`/`resume` 触发批次收束，"提议写 → 审批 → 执行"
  的调用与结果被整段丢出模型视图（每次审批都固定产生 2 条违规）；
* **分叉跨分支复用幂等键**：子分支会命中父分支的去重行，复用**另一组参数**的结果并跳过审批门；
* **工具异常穿透 loop**：run 永久 RUNNING、调用悬挂、每次 resume 重放副作用；
* **压缩可能越压越大**：摘要更长时仍写入，触发条件退化成"每步压缩"直至 overflow。

细节与修复对照表见 [w4-report §六](docs/w4-report.md)，回归用例见
`tests/test_audit_regressions.py`。

### 外部独立测试（2026-09-16，含结论）

按 [docs/tester-prompt.md](docs/tester-prompt.md) 的黑盒纪律跑的一轮**独立会话测试**
（与作者会话隔离，非真人专家）：先只读"规格"（README、承诺清单、源码与 `--help`），
完成冻结结论之后才读"答案"（报告、测试、posts）。结论：

* **0 个 P0**——没有一条机制性结论被证伪；`reports/*.json` 与外部重跑一致；
* 1 个 P1——`opsenv.suite --systems` 子集会直接崩溃而不是走门禁（已修复 + 回归用例）；
* 8 个 P2——README/HANDOFF 正文数字滞后于审计修复（已随本轮更新）。

报告与复现命令见 [`docs/independent-test-2026-09-16/`](docs/independent-test-2026-09-16/)。
它的 §9（无法判定项）与 §10（未覆盖风险）也一并保留——包括"读外部账本要连
`-wal` 一起快照"这类实测踩过的坑。

## 核心设计

先读 [`docs/semantics.md`](docs/semantics.md)：它是 runtime 的承诺清单，
代码与测试都以它为口径。三条最关键的设计约束：

1. **事件日志是唯一权威**：扁平 append-only（`(branch_id, seq)`），历史改写由
   SQLite 触发器物理禁止；状态永远是日志的折叠物。
2. **TREE_NODE 与 ARTIFACT 分离**：只有对话节点进上下文视图，预算/租约/审批/压缩
   等控制事实一律为 ARTIFACT，禁止混入树。
3. **checkpoint 与 writes 分离**：task 完成即写 writes，checkpoint 只在 super-step
   边界提交；恢复 = 最新 checkpoint + 重放 writes（不重跑副作用）+ 未完成 task 重跑。

## 布局

```
harness/
  events.py              事件模型（扁平日志、kind 分离、type 约束、事件哈希链的 record 定义）
  cassette.py            录制物 schema（tool_calls + canonical usage + provenance）B-M1
  live_transport.py      真实模型 transport（OpenAI 兼容，仅标准库）B-M1
  audit_chain.py         离线链校验 CLI（读库连 -wal 快照，报第一个断点）B-M2
  lease.py               单写者租约（acquire/renew/require，权威 = 日志折叠）B-M3
  otel.py                OTLP 导出桥（延迟 import，缺 extra 不影响既有行为）B-M3
  model.py               模型接口（loop 与 LLM client 共享）
  prompts.py             系统提示词（稳定前缀的主体）
  context.py             视图构建：四段布局 + 三条结构不变式 + 卸载
  cache.py               前缀缓存模型（读/写/普通 token）
  compaction.py          压缩：turn group 选择 + 摘要事件 + 确定性 id
  artifacts.py           内容寻址 artifact 存储 + read_artifact 工具
  budget.py              预算账本（分桶、事件持久化、硬停）
  llm.py                 唯一模型出口（窗口检查 + 缓存计费 + 预算记账）
  state.py               事件 → 派生状态折叠 + 不变量（INV-001..007）
  tools.py               效果声明、幂等键、参数规范化、探针 ProbeFn
  approval.py            审批绑定（nonce / 过期 / scope / 参数 hash / TOCTOU）
  chaos.py               命名崩溃窗口 + 确定性 SIGKILL
  loop.py                agent loop（super-step、审批门、outbox、恢复）
  ids.py                 ID 生成（uuid7 可用则用，否则 uuid4）
  tokens.py              token 估算 + 版本化价格表
  trace.py               事件日志 → span 树投影（OTel 形状导出 + 排障 CLI）
  store/
    schema.py            DDL、schema 版本与迁移、append-only 触发器
    sqlite_store.py      单写者事务、seq 分配、分叉解析
    checkpoints.py       checkpoint/writes 提交协议、恢复计划
    tool_calls.py        工具调用记录（运行时去重表 / outbox 意图行）
fakeworld/               受控仿真：副作用账本 + 仿真工具 + 脚本化模型（W2–W4 崩溃实验用）
opsenv/                  运维壳与场景集（W5）：故障分类学、四通道证据、四系统对照评测
  oracle.py              外部账本 oracle（四类不变量 + 主库可读性，fuzz/探测器的裁判）A-M3
experiments/
  worker.py              run / approve / resume 的子进程入口（含 --kill-after-ms / --model 三模式）
  chaos_fuzz.py          随机时刻 SIGKILL fuzz（标定窗口 + 外部账本判定）A-M3
  crash_matrix.py        崩溃矩阵 runner（多阶段计划 + 真实 kill -9）
  context_cost.py        上下文成本实验（缓存纪律 / 卸载 / 压缩 三组对照）
  context_sweep.py       压缩阈值扫描（完成率 × 缓存命中 × 净成本，含自绘 SVG）
scripts/
  check_facts.py         claim 对账入口：文档里的数字 ↔ 再生命令（CI job `facts`）
  count_tests.py         用例计数（演进类 claim 的来源，文档不手写绝对数）
  mutation_check.py      变异门禁（新增幸存变异 ⇒ 退出 1；基线见 reports/mutation_baseline.json）
  replay_consistency.py  scripted ↔ replay 的白名单一致性对账
  chain_mirror_check.py  哈希链 mirror check（干净库通过 / 篡改副本必须被指出断点）
  probe_sigterm.py       谱系探测器：SIGTERM（无优雅 flush 语义）
  probe_tornwrite.py     谱系探测器：torn write + SIGSTOP 悬挂
  probe_extra_ledger_row.py  谱系探测器：账本外注入不触发重跑
tests/                   不变量、协议、审批语义、loop 与矩阵小样本
docs/semantics.md        运行时语义（承诺清单）
docs/w2-crash-windows.md W2 崩溃矩阵实验报告
docs/w3-report.md        W3 审批与 outbox 一致性实验报告
docs/w4-report.md        W4 上下文工程与成本实验报告
docs/w5-report.md        W5 运维壳、场景集与三基线对照报告
docs/w6-report.md        W6 统计口径、分段汇报与门禁报告
docs/w7-report.md        W7 阈值扫描、trace 收口与开源整理
docs/HANDOFF.md          项目状态与交接（压缩上下文 + 复盘）
LICENSE                  MIT
examples/w1_tour.py      W1 演示（事件日志 / 分叉 / 恢复计划）
reports/                 自动生成的报告（*.md 表格 + 汇总 JSON；逐次运行明细
                         用 --runs-out 再生，不入库）
reports/documented-facts.json  文档引用的结论数字与再生命令的绑定（claim 清单）
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,eval]"
.venv/bin/pytest                                        # 全部测试（含矩阵小样本）

# 崩溃矩阵：6 窗口 × outbox × 下游幂等 × 探针 + 篡改/长任务控制组（16 格）
.venv/bin/python -m experiments.crash_matrix --repeats 5 \
    --json-out reports/w4_crash_matrix.json --md-out reports/w4_crash_matrix.md

# 上下文成本实验：缓存纪律 / 卸载 / 压缩 三组对照
.venv/bin/python -m experiments.context_cost \
    --md-out reports/w4_context_cost.md --json-out reports/w4_context_cost.json

# W5/W6 四系统对照评测（64 场景 × 4 系统 × 2 推理器 × 3 次，约 8 秒）
.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 \
    --md-out reports/w6_eval.md --json-out reports/w6_eval.json

# 带门禁（CI 用）：任一条不通过则非零退出
.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate
```

单次崩溃可手工复现：

```bash
.venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode run       # 停在审批门
.venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode approve   # 人工批准
CHAOS_WINDOWS="post_tool_effect_pre_record:1" \
    .venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode resume --tool-idem off
    # 进程被 SIGKILL（退出码 137）
.venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode resume --tool-idem off
```

## 文档索引

| 想了解什么 | 读哪份 |
|---|---|
| 项目状态与复盘（**从这开始**） | [docs/HANDOFF.md](docs/HANDOFF.md) |
| 运行时承诺清单（语义） | [docs/semantics.md](docs/semantics.md) |
| 三篇长文（崩溃语义 / 审批绑定 / 缓存会计） | [docs/posts/](docs/posts/) |
| 简历措辞与面试问答映射 | [docs/resume.md](docs/resume.md) |
| 各轮实验报告 | [docs/w2](docs/w2-crash-windows.md) · [w3](docs/w3-report.md) · [w4](docs/w4-report.md) · [w5](docs/w5-report.md) · [w6](docs/w6-report.md) · [w7](docs/w7-report.md) |
| 自动生成的报告表格与汇总数据 | [reports/](reports/) |
| **给技术面试官的 3–5 分钟项目介绍** | [docs/pitch-4min.md](docs/pitch-4min.md) |
| 交给外部测试人员的提示词 | [docs/tester-prompt.md](docs/tester-prompt.md) |
| 独立外部测试报告（2026-09-16） | [docs/independent-test-2026-09-16/](docs/independent-test-2026-09-16/) |
| 随机注入与故障谱系（fuzz / SIGTERM / torn write / 悬挂 / 账本外注入） | [docs/fault-spectrum.md](docs/fault-spectrum.md) |
| 噪声人格口径（激活评分口径敏感性） | [docs/noisy-reasoner.md](docs/noisy-reasoner.md) |
| 模型接入三模式（scripted / record / replay） | [docs/model-modes.md](docs/model-modes.md) |
| OTLP 导出 / artifact GC / 单写者租约 | [docs/governance-extras.md](docs/governance-extras.md) |
| 开放问题裁决（A §4 + B §6，含回写 B §7） | [docs/design/2026-09-17-open-questions-answered.md](docs/design/2026-09-17-open-questions-answered.md) |
| 下一波设计文档（待评审） | [docs/design/](docs/design/) |
| **开发规范（分支流程 / 合并门槛）** | [docs/development.md](docs/development.md) |
| **测试规范（必跑命令 / 自证作弊清单）** | [docs/testing.md](docs/testing.md) |
| 给 AI agent 的入口约束 | [AGENTS.md](AGENTS.md) |

## 交接与复盘

**[docs/HANDOFF.md](docs/HANDOFF.md)** 是整个项目的"压缩上下文"：只放不可丢失的硬事实、
跨轮次口径规则、七轮结论索引、未决清单，以及八次失败的模式化教训。
新接手（或上下文被清空后重新开工）从这一份开始读，再按需展开到各轮报告。

## 如何复现全部结论

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,eval]"

.venv/bin/pytest -q                       # 全部测试（用例数不手写：claim tests-collected
                                          # 由 scripts/count_tests.py 再生）
.venv/bin/ruff check .                    # lint

.venv/bin/python -m experiments.crash_matrix --repeats 5 \
    --json-out reports/w4_crash_matrix.json --md-out reports/w4_crash_matrix.md
    # → W2–W4：崩溃窗口矩阵（重复副作用只由下游幂等/outbox 决定）

.venv/bin/python -m experiments.context_cost \
    --md-out reports/w4_context_cost.md --json-out reports/w4_context_cost.json
    # → W4：缓存纪律 / 卸载 / 压缩 三组对照

.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate \
    --md-out reports/w6_eval.md --json-out reports/w6_eval.json --runs-out /tmp/runs.json
    # → W5/W6：四系统 × 64 场景 × 2 推理器；--gate 不通过则非零退出

.venv/bin/python -m experiments.context_sweep --repeats 2 \
    --md-out reports/w7_sweep.md --json-out reports/w7_sweep.json --svg-out reports/w7_sweep.svg
    # → W7：压缩阈值 → 完成率 / 缓存命中 / 净成本 三维曲线

.venv/bin/python -m harness.trace --run-dir <worker 的 run 目录>   # 排障：事件日志 → span 树

# W9 起的门禁与对账（一条命令回答"文档里的数字还对不对"）
.venv/bin/python scripts/check_facts.py --run verify   # 轻 claim 集（CI job facts）
.venv/bin/python scripts/check_facts.py                # 含整量 claim（约 100 秒）
.venv/bin/python scripts/mutation_check.py             # 变异门禁：新增幸存变异即红
.venv/bin/python -m experiments.chaos_fuzz --repeats 30 --seed 20260917   # 随机时刻 fuzz
.venv/bin/python scripts/replay_consistency.py         # scripted ↔ replay 一致性
.venv/bin/python -m harness.audit_chain --run-dir <run 目录>   # 事件哈希链全量校验
```

入库的是**报告表格与汇总 JSON**；逐次运行明细（`--runs-out`）不入库——体积大且可由上面
这些命令再生。仓库里 `reports/*.md` 就是各轮报告引用到的证据表。

## 边界声明

* 崩溃实验只对 **kill -9** 语义成立；掉电（`synchronous=NORMAL` 下可能丢最后一个
  事务）不在本轮范围内，见语义文档 §3。
* 统计口径：W2 每格 n=5，0/5 只把失败率上界压到约 60%（rule of three），
  **不能**据此声称"永不重复"；W6 会提到 30+ 次并报置信区间。
* 存储层是单写者模型，跨进程并发需要上层租约（W3）。
* 本仓库不出现"生产级"字样：它是一个实验台，所有指标以"相对基线的 paired 差值 +
  置信区间"报告，不以绝对值宣称达标。
* 租约（W9）**不是并发承诺**：它是"入场条件"，双进程同时写的后果仍由外部账本裁决；
  一次冒烟不是并发证据（见 `docs/governance-extras.md` §3）。
* fuzz 的"未发现新类违例"只把失败率上界压到约 2%（rule of three），**不能**读成
  "覆盖了所有窗口"；注入落点受机器时序影响，只有注入**时刻**可复现（见 §5 边界）。

## 参与开发（人类或 AI agent）

* [`AGENTS.md`](AGENTS.md)：所有 agent 会话的入口约束（红线清单 + 最短工作流）；
* [`docs/development.md`](docs/development.md)：分支流程、合并门槛、代码与文档纪律；
* [`docs/testing.md`](docs/testing.md)：必跑命令、改动→测试义务、自证作弊清单。

规矩只有一条：**分支开发，全绿验证，才允许合并进 `main`**；文档里的每个数字都要能被命令再生。
