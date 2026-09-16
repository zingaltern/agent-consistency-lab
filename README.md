# agent-consistency-lab

面向**崩溃一致性与治理语义**的 Agent runtime 实验台。

不是一个"Agent 应用"，也不是 LangGraph 的替代品。它是一个**被测对象 + 测量仪器**：
用精简自研内核把"崩溃恢复、审批绑定、幂等边界、上下文压缩"这些语义做出来，
用命名的崩溃注入点把它们测出来，并与 LangGraph 的同任务等价图做对照。

运维故障处置（IT 运维场景）只是验证壳，可替换。

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
| W8 | 长文 ×3 + 文档 + demo | 未开始 |

## 已产出的实测结论

**W2**（[w2-crash-windows.md](docs/w2-crash-windows.md)）：
"效果已发生、记录未落盘"是唯一产生重复副作用的窗口，下游不幂等时 5/5 复现，
且 runtime 侧去重开关在该窗口完全无效。

**W4**（[w4-report.md](docs/w4-report.md)，同一条长任务 10 次调用）：

* **缓存纪律值 2 倍成本**：运行时信息进前缀 ⇒ 命中率 57.5% → 0%、成本/调用 +105%；
* **卸载是唯一"纯赚"的杠杆**：token/调用 -66%、成本/调用 -62%，且不依赖模型调用；
* **压缩是拿钱买可用性**：能跑完（7 次压缩、0 溢出），但成本/调用 $0.01197 反而最高，
  命中率只有 6.7%——每次压缩都从替换点击穿前缀缓存；
* 崩溃矩阵新增窗口 5：压缩中 SIGKILL 后引用完整、事件不重复替换、副作用仍恰好一次。

**W3**（[w3-report.md](docs/w3-report.md)，70 次崩溃 + 70 次恢复，14 格）：

* 同一窗口加 **outbox（副作用前预写意图）+ 按键读回**：重复副作用 5/5 → **0/5**，
  恢复路径由探针确认（不重跑）；
* 没有读回能力时：0 重复，但收敛为**恰好 1 行 unknown** 待人工对账——
  即把"静默重复"变成"显式有界的未知"；
* **TOCTOU 有实测护栏**：批准之后执行之前改写参数，5/5 拒绝执行、0 副作用；
* **改参即换键**有端到端证据：改参批准 → 新 tool_call_id → 不同幂等键，账本落的是改后参数。

**W5**（[w5-report.md](docs/w5-report.md)，64 场景 × 4 系统 × 2 推理器 × 3 次 = 1536 次运行）：

* **取证充分性决定正确率上限**：只看 metrics+resources 的规则基线充分率 50%、
  正确率恰好也是 50%；按通道阶梯取证的 agent 路线充分率 100%、正确率 86–90%；
* **模型出错时架构决定后果**：无审批门的单次调用基线在 20.3% 的场景里
  **执行了破坏性动作**（含 2 次静态拒绝列表之外的新动作）；有审批门的两条路线 0.0%，
  代价是把 20–30% 的出错场景交给人并被拒绝；
* **准确率最高的架构同时是事故率最高的架构**（单次调用基线正确率 93.2%，但零拦截）；
* **两条 agent 路线能力等价**（步数 3.38 vs 3.38、token 相同、正确率与红线率在噪声内一致），
  差别在崩溃一致性与"工具自述风险"这两层工程属性上。

**W6**（[w6-report.md](docs/w6-report.md)，统计口径 + 门禁）：

* **"谁更准"不可区分，"谁更安全"统计显著**：harness − single_shot 的诊断正确率
  +0.005（95% CI [−0.099, +0.104]，配对 192 组），红线执行率 −0.203（CI [−0.260, −0.146]）；
* **代理指标要校准**：`取证充分性` 只对规则基线有效（κ=1.0），对 agent 路线恒为真、
  没有区分力——**不能拿它当线上监控指标**；
* **门禁自己也要被验证**：第一版门禁抓不到"静默丢弃破坏性动作"的退化（指标完美、机制已死），
  补上对称自检（有 gate 的系统必须真的拦下过东西）后，退化的实现会让 CI 变红；
* holdout 与 dev 无落差（本轮无调参），考卷留到 W7 的阈值扫描用。

**W7**（[w7-report.md](docs/w7-report.md)，阈值扫描 6 档 × 8 变体 × 2 次）：压缩买的是完成率、不是省钱——
不压缩只有 33% 变体能跑完（成本 $0.066），压缩后 100% 完成但成本 +40%；压缩次数与缓存命中率严格反向
（0 次 68.0% → 6.67 次 7.0%）；**阈值应贴近上限（0.85–0.95），"早点压更安全"被数据否定**。

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
  events.py              事件模型（扁平日志、kind 分离、type 约束）
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
experiments/
  worker.py              run / approve / resume 的子进程入口
  crash_matrix.py        崩溃矩阵 runner（多阶段计划 + 真实 kill -9）
  context_cost.py        上下文成本实验（缓存纪律 / 卸载 / 压缩 三组对照）
  context_sweep.py       压缩阈值扫描（完成率 × 缓存命中 × 净成本，含自绘 SVG）
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
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
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

## 交接与复盘

**[docs/HANDOFF.md](docs/HANDOFF.md)** 是整个项目的"压缩上下文"：只放不可丢失的硬事实、
跨轮次口径规则、七轮结论索引、未决清单，以及八次失败的模式化教训。
新接手（或上下文被清空后重新开工）从这一份开始读，再按需展开到各轮报告。

## 如何复现全部结论

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,eval]"

.venv/bin/pytest -q                       # 214 个测试：不变量、协议、审批、异常、评测、门禁
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
