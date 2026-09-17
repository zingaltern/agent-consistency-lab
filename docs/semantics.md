# 运行时语义（v1 + W9 增补）

本文件是 runtime 的**承诺清单**。代码、测试与后续实验结论都以本文件为口径；
任何实现改动导致承诺变化，必须先改这里。

**变更记录**（"W1 冻结"说的是 v1 的骨架承诺；此后每次改动都记在这里）：

| 时间 | 改动 | 性质 | 落点 |
|---|---|---|---|
| W1 | 初版：事件日志权威、折叠状态、checkpoint 协议、7 条不变量 | — | 全文 |
| W3 | 审批绑定四约束、outbox 与探针对账 | 扩大 | §2.5、§4.1 |
| W4 | 视图/压缩/卸载/预算与缓存纪律 | 扩大 | §2.6 |
| W9（A/B 包） | INV-008 事件哈希链 | **扩大**（新增"什么算日志被篡改"的定义） | §2.4.1 |
| W9 | artifact 回收口径（引用枚举 + 派生缓存可重建） | 扩大 | §2.6 |
| W9 | "模型属于测量外部"（承诺以 harness 接口为界、录制 usage 权威） | 澄清为主 + 一处新权威声明 | §2.7 |
| W9 | 参数级安全域（工具自述安全域 + 执行前一刻强制） | 扩大 | §4.0 |
| W9 | 租约最小实现（**校验入口**，写路径未接入） | 扩大 | §3 |
| W9（评审修复） | 上表措辞收窄：租约"只有显式调用时才生效"；sweep 的引用枚举与实现对齐 | 澄清 | §3、§2.6 |

---

## 1. 承诺

| 承诺 | 内容 | 由什么保证 |
|---|---|---|
| 执行语义 | **at-least-once**：中断/崩溃后恢复，未完成的步骤会重新执行 | 事件日志 + checkpoint 恢复算法 |
| 效果语义 | 启用 outbox 时：**至多一次（效果）仅当下游支持幂等键**，否则产生 `unknown` 进入对账；关闭 outbox 时该窗口会产生重复副作用（W2 实测 5/5） | 工具声明效果类别 + outbox 意图行 + 探针 |
| 日志语义 | **不重不漏**：同一事件永不重复写入，已提交事件永不丢失（kill -9 语义下） | 单写者事务 + append-only 触发器 + `UNIQUE(branch_id, seq)` |
| 状态语义 | 状态**不作为权威被持久化**（checkpoint 里的 channel 值只是快照）：权威只有事件日志，状态 = 日志的折叠 | `harness.state.reduce_events` |
| 视图语义 | 请求视图由事件日志**纯函数**重建；违反结构不变式时丢弃违规批次而不是发出非法请求 | `harness.context.ViewBuilder` |
| 成本语义 | 每次模型调用的 token / 缓存读写 / 成本都写进事件，可事后复算；预算超限即硬停 | `harness.llm` + `harness.budget` |
| 恢复语义 | 未闭合调用按 at-least-once 重跑；**有 pending 意图行时先探针对账，绝不盲目重跑** | 事件日志折叠 + outbox 意图行 + 探针 |
| checkpoint 语义 | checkpoint 是**边界快照与交叉校验**，不是恢复权威：恢复时校验它引用的事件确实存在，不一致就报 `checkpoint_log_mismatch` | `harness/loop.py::_checkpoint_cross_check` |

**明确不承诺**：不承诺 exactly-once 效果（任何声称此承诺的系统都在某处依赖下游幂等），
不承诺掉电不丢最后一个事务（见 §3），不承诺跨进程并发写（见 §2）。

**实测（详见 [w2](w2-crash-windows.md) / [w3](w3-report.md) / [w4](w4-report.md) 报告）**：
"效果已发生、记录未落盘"窗口是唯一会丢一致性的位置。关掉 outbox 时重复副作用 5/5；
开启 outbox 后 0/5——有按键读回时由探针确认（对账），无读回时收敛为恰好 1 行 unknown。

---

## 2. 存储与恢复语义

### 2.1 扁平 append-only 日志

事件以 `(branch_id, seq)` 为主键，`seq` 在分支内从 0 连续递增。
`parent_id` 只是对话 lineage 的可选标注：**恢复与重放不得依赖递归查询**。

物理上禁止改写历史：`events` 表上的 `BEFORE UPDATE` / `BEFORE DELETE` 触发器
直接 `RAISE(ABORT)`。要修正历史，只能追加新事件并由视图层重新解释。

### 2.2 TREE_NODE 与 ARTIFACT

| kind | 内容 | 是否进入对话树/上下文视图 |
|---|---|---|
| `TREE_NODE` | user_message / agent_message / tool_call / tool_result / interrupt / resume | 是 |
| `ARTIFACT` | error / budget_update / lease / compaction / approval / state_update | 否 |

控制类事实（预算、租约、压缩、审批、错误）**禁止混入树**。历史事故模式：
带 `parent_id` 的控制事件参与树解析，导致整条活跃分支被 strand（OpenHands #4057）。

### 2.3 分叉（fork）

`create_branch(parent_branch_id, fork_event_id)` 不复制历史：
子分支的有效日志 = 父分支有效日志截断到 `fork_event_id` + 子分支自身事件；
子分支的 `seq` 从 fork 点 +1 继续。因此"共享前缀"是结构性事实，
父子分支永不互相污染。

### 2.4 提交协议（checkpoint / writes）

1. task 完成即写 `checkpoint_writes`（幂等 upsert，主键
   `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)`）；此时 checkpoint 未提交。
2. `checkpoints` **只在 super-step 边界提交**，记录该边界的全量 channel 快照。
3. 崩溃发生在边界之前：该 super-step 不被承认，但已完成 task 的 writes 仍在，
   恢复时被重放而非重跑。
4. **存储层协议**：`get_latest` → 重放其后 writes → 未完成 task 从头执行
   （`RecoveryPlan` 是该协议的可断言载体，见 `tests/test_checkpoint_protocol.py`）。

   ⚠️ 需要说清：runtime 的**实际**恢复路径不是上面这条，而是
   "事件日志折叠 + 未闭合调用重跑 + outbox 意图行对账/探针"（见 §2.5 与 §2.6）。
   checkpoint/writes 提供的是边界快照、审计留痕与交叉校验；把它们当作恢复权威
   会得出错误心智模型（评审据此判定为"文档承诺与实现背离"，已修正）。

writes 的保留索引（对齐 LangGraph 语义，负数供控制类使用）：
`-1 ERROR`、`-2 SCHEDULED`、`-3 INTERRUPT`、`-4 RESUME`；普通输出用 `>= 0`。

`metadata` 必须**原样保留未知键**（跨版本读取不丢字段）；运行时字段统一注入
在 `_runtime` 命名空间下。

### 2.4.1 事件哈希链（R-B3）

`events` 表带 `prev_hash` / `event_hash` 两列，构成**静态哈希链**：

```
record       = {event_id, run_id, branch_id, seq, kind, type, source, parent_id,
                payload, created_at}                 # 解析后的 payload，不含 trace/span
record_bytes = json.dumps(record, sort_keys=True, ensure_ascii=False)
event_hash   = sha256(prev_hash ‖ record_bytes)
prev_hash    = 同分支上一条的 event_hash
               分支首条：无父分支 ⇒ 常量 GENESIS_HASH；有父分支 ⇒ fork 点事件的 event_hash
```

**为什么需要它**：append-only 触发器挡住的是**数据库层面**的改写；链挡住的是**数据库之外**
的操作——换成一个更旧的副本、绕过触发器改一行、删掉中间一段再拼上。链不阻止篡改，
只让篡改**无法静默**。

**与既有承诺的关系**：这是**纵深防御**，不是新的权威。日志仍是唯一权威；
`(branch_id, seq)` 排序、分叉语义、触发器**一个都没变**。验证入口：

* 增量：`harness/state.py::verify_chain`（只查新 seq，起点由调用方给）；
* 全量：`python -m harness.audit_chain --run-dir <dir>`（读库连 `-wal`/`-shm` 一起快照，
  报**第一个断点**，退出码 0/1/2 = 完整/断链/读不出来）。

**schema 迁移（v2 → v3）**：存量库一次性补链。回填必须 UPDATE，而 append-only 触发器
对任何 UPDATE 都 `RAISE(ABORT)`，因此迁移在**一个事务内**先 `DROP TRIGGER`、补链、
再**原样重建**触发器（payload 与排序一字未动）。迁移自测断言"迁移后触发器存在且
UPDATE 仍被拒"（`tests/test_hash_chain.py::test_migration_backfills_the_chain_and_recreates_triggers`）。

### 2.5 工具执行的落盘顺序（W3 起含 outbox 与审批）

一次工具执行按固定顺序处理（每一步都是"日志权威"的具体体现）：

1. 日志里已有结论 → 重放，不重跑
2. 补记 `tool_call` 事件（幂等）
3. 查既有意图行：`executed` → 重放；`pending` → 探针对账（或转 `unknown`）；
   `unknown` → 绝不自动重试
4. 审批门：未获批的需审批调用 → 发 `interrupt`，loop 停在 `WAITING_HUMAN`
5. 审批校验：拒绝 / 过期 / 参数 hash 不一致（TOCTOU）→ 拒绝执行并记录原因
6. outbox 预写 `intent(pending)`（仅非幂等写）
7. 执行副作用（外部系统）
8. `tool_result` 事件（**权威记录**）→ 闭合意图行 → 边界 checkpoint 提交

事件先于去重行的原因：这样"去重行已写、事件未写"这个**危险**微窗口不存在；
反向残留（事件在、去重行缺失）由日志权威兜住，最坏只是审计少一行。

### 2.6 上下文视图、压缩与缓存纪律（W4）

**视图是纯函数**：``ViewBuilder.build(events, dynamic) → View``，块序列分四段：

``prefix``（系统提示 + 工具清单，**永不变化**）→ ``history``（append-only 的对话与工具结果）
→ ``summary``（压缩摘要，追加在尾部）→ ``tail``（每步变化的运行时信息）。

动态信息**只允许进 ``tail``**：进了 prefix 等于每步重建前缀，缓存全灭。

**结构不变式**（违反即丢弃违规部分并记录，绝不发出会被供应商拒绝的请求）：

* 配对：tool_call 与 tool_result 一一对应；
* 批次原子：同一次模型响应里的多个调用要么全留要么全删；
* 观测唯一：同一调用至多一条结果。

**卸载**：工具结果超过 ``max_inline_tokens`` 时写入内容寻址 artifact（sha256），
上下文里只留摘要 + digest；模型用 ``read_artifact`` 按切片读回。
内容寻址保证同一结果两次渲染逐字相同，因此卸载不会破坏缓存前缀。

**压缩**：把最老的若干 turn group 换成一段摘要，以 ``compaction`` artifact 事件追加，
被替换的事件仍在日志里、只是不再进入视图。先写 artifact 再追加事件 ⇒ 崩溃要么
完整生效要么完全没发生。``compaction_id = sha256(被替换事件集合)`` 使重算可检测。

**注**：压缩一定会在替换点击穿前缀缓存——这是可测的代价（见 docs/w4-report.md），
所以策略是"先卸载、后压缩，阈值尽量高"。

**artifact 回收（R-B5）**：``harness.artifacts.sweep(dry_run=True)`` **只能由显式 CLI 调用**
（runtime loop 内绝不自动删——删除与"读到半个文件"之间没有事务可用）。引用枚举**全量扫描**
事件 payload + ``checkpoints.state_json`` + ``checkpoint_writes.payload_json`` 里的 ``digest``；
只被 checkpoint 引用的对象**禁止回收**。卸载产生的 artifact 是**派生缓存**：没被任何 payload
提到过的可以删——下一次渲染会用同样的内容算出同样的 digest 并重新落盘（内容寻址 ⇒ 幂等）。

**预算**：``main / compaction / judge / tools`` 分桶，账本由 ``budget_update`` 事件构成，
跨进程恢复后重新折叠得到；dispatch 前检查、响应后复核，超限即 fatal。

---

## 2.7 模型属于测量外部（R-B1）

**承诺以 harness 接口为界**：`docs/semantics.md` 的全部承诺（不重不漏、审批绑定、
崩溃恢复、预算硬停、效果至多一次）都以 `harness/model.py` 的 `Model` 协议与
`harness/llm.py` 的 `LLMClient` 协议为界——**模型是什么、跑在哪里、多贵，都不改变这些承诺**。
由此推出三条与模型接入有关的硬约束：

1. **三种模式语义等价**：`scripted`（默认，脚本模型）/ `record`（真实调用 + 录制）/
   `replay`（按录制回放）走的是**同一条 loop**。录制与回放只替换"模型那一侧"，
   不触碰审批、outbox、事件日志、checkpoint 的任何一行。
2. **录制的 usage 是权威**：真实供应商的服务端缓存账单无法本地复算，因此
   record/replay 模式的 **token 直接采用录制下来的 canonical 四段**
   （`prompt / completion / cache_read / cache_write`），不重算。
   `harness/cache.py` 的前缀缓存模型只服务于 scripted 模式（W4/W7 的成本结论都在那个口径下）。
   **成本的口径分两种**（评审 P2-12 要求写准）：没有预算账本时直接回放录制里的
   `cost_usd`；**接了预算账本时成本由账本按当前价目表记账**（预算必须以账本为准，
   否则"花掉多少"会出现两个数）；录制与当前的 `price_version` 不一致时，
   replay 会给一条 `price_version_mismatch` 警告。
3. **崩溃语义不变**：`CHAOS_WINDOWS` 与 `--kill-after-ms` 两种注入在三种模式下都可用；
   录制/回放不新增崩溃窗口，也不改变既有的 at-least-once 与对账语义。

**明确不承诺**：不做模型质量评测、不做供应商路由/重试/降级/缓存网关；
live 路径永远是显式的、非默认的（无 `--budget-usd` 拒绝启动 live 录制）。

**OTLP 导出（R-B4）**：``harness.trace --otlp-endpoint URL`` 才启用真实导出；
不装 ``[otel]`` extra、不传该参数时，``import harness.trace`` 与全部既有行为不变
（OTel 的 import 全在函数体内）。endpoint 每次显式传入，不启动守护、不做重试队列。

---

## 3. 崩溃语义的边界（重要）

本项目的崩溃实验只对 **kill -9** 语义成立，理由与边界必须写清：

* SQLite 在本项目使用 `WAL + synchronous=NORMAL`：进程被 kill 时 OS page cache
  仍在，已提交事务不丢；但**掉电**可能丢失最后一个已提交事务（库不会损坏）。
* 因此："50 次 kill+resume 全部通过"只能证明 kill 语义下的行为，且按
  rule of three，50 次通过只把失败率上界压到约 6%，不是"100% 无重复"。
* 真实掉电语义需要 `synchronous=FULL` + fsync 计时实验，本项目不做，列为
  未来工作。

并发边界：存储层是**单写者**模型（进程内互斥 + `BEGIN IMMEDIATE`）。
跨进程并发需要上层租约：`lease` 事件类型自 R-B6 起有**最小实现**（`harness/lease.py`：
acquire / renew / require，权威 = 日志里最后一条 `lease` 事件的折叠物）。

租约是**入场条件**，不是新的权威，也不是并发承诺。**注意口径**：本实现提供的是
**校验入口**（`harness/lease.py` 的 `require` / `guarded_write`）；
**runtime 的写路径尚未接入它**——也就是说"未持有 ⇒ 可读失败"只在调用方显式调用时成立，
今天双进程写同一 run 仍然不被任何检查拦住（下一波把租约接进写路径时，需要重新过这份文档）。

* 调用方显式校验时：持有有效租约可以写；未持有 / 已过期 / 被别人持有 ⇒ **可读失败**（绝不静默放行）；
* 校验在 checkpoint 事务**之外**（先证权、后写；把两者缠在一起会让"写租约也要持租约"变成自指）；
* **不做**过期接管（显式 acquire 才是接管方式）、不做分布式协调；`state_update` 仍是"已登记未实现"；
* 双进程同时写同一 run 的**实际后果**仍由外部账本裁决，租约只回答"这个进程有没有写权限"。

### 3.1 命名崩溃窗口

窗口是代码中的确定性注入点（`harness/chaos.py`），命中即 SIGKILL 自身进程。

| # | 窗口 | 位置 | 状态 |
|---|---|---|---|
| 1 | `pre_tool_exec` | 模型响应后 / 工具执行前 | 已实现并测（W2/W3） |
| 2 | `post_tool_effect_pre_record` | 工具成功后 / 任何记录前 | 已实现并测（W2/W3） |
| 3 | `post_record_pre_commit` | 记录后 / 边界 checkpoint 提交前 | 已实现并测（W2/W3） |
| 4 | `post_approval_pre_exec` | 审批通过后 / 执行前 | 已实现并测（W3） |
| 5 | `during_compaction` | 压缩进行中（artifact 已写、事件未追加） | 已实现并测（W4） |
| 6 | `after_resume` | 恢复之后再次崩溃 | 已实现并测（W3） |

窗口 2 是唯一产生重复副作用的位置，且只能由下游幂等或 outbox+探针兜住
（W3 实测：无 outbox 5/5 重复；outbox+读回 0/5 且探针确认；outbox 无读回 0/5
但留下 1 行 unknown 待人工对账）。

---

## 4. 事件目录（payload 规范形状）

| type | kind | payload |
|---|---|---|
| `user_message` | tree | `{text}` |
| `agent_message` | tree | `{text, final?, usage, view_fingerprint, context_tokens, cache_read_tokens, cache_write_tokens, cost_usd, cache_config_version, price_version}` |
| `tool_call` | tree | `{tool_call_id, tool, args, args_sha256, idempotency_key, effect}` |
| `tool_result` | tree | `{tool_call_id, status: executed\|failed\|unknown\|rejected\|superseded, result?, error_class?}` |
| `interrupt` | tree | `{interrupt_id, interrupt_index, tool_call_id, tool, args, args_sha256, reason}` |
| `resume` | tree | `{interrupt_id, interrupt_index, approval_id, decision, tool_call_id}` |
| `error` | artifact | `{error_class, message, fatal?: bool}` |
| `budget_update` | artifact | `{bucket, cost_usd, price_version, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, note}` |
| `lease` | artifact | `{owner, expires_at, acquired_at, lease_id}`（R-B6 起有生产者与消费者） |
| `compaction` | artifact | `{compaction_id, replaces_event_ids, summary, artifact_ref, tokens_before, tokens_after, reason, round}` |
| `approval` | artifact | `{approval_id, tool_call_id, tool, requested_args_sha256, approved_args_sha256, decision, actor, nonce, issued_at, expires_at, scope, policy_version, edited, requested_args, approved_args}` |
| `state_update` | artifact | 类型已登记但**当前无生产者**（保留给未来的外部状态注入） |

### 4.0 参数级安全域（R-B2）

工具可在注册时声明 ``arg_policy``（**一层字段** + ``allowed`` / ``forbidden`` / ``min`` / ``max``），
声明的是**工具自己的安全域**——与 ``requires_approval`` 同一哲学：工具自述风险，gate 执行它。
它不是一张外置黑名单（"谁有权定义危险"是工具契约问题，不是运维策略问题）。

执行点是**执行 handler 前的最后一刻**，输入是**实际参数**（不是审批记录里的 hash）：
审批比的是 ``args_sha256``，人并没有逐字段核对结构化参数，因此
"批准了越界参数"这条缝隙只能由策略档堵住。

语义（缺字段的处置是语义，不是细节）：

* ``allowed`` / 区间：字段缺失 ⇒ **拒绝**（无法核对不能默认放行）；
* ``forbidden``：字段缺失 ⇒ 放行（没有危险值出现）；
* 值域越界 / 类型不符（区间策略收到非数值）⇒ 拒绝，``tool_result.status=rejected``、
  ``error_class=arg_policy_violation``，且**不写 outbox 意图行、不产生副作用**；
* 拒绝发生在 outbox 预写之前，因此被拦下的调用在日志里是"被拒绝"，不是"未知"。

本期**不承诺**：嵌套字段（``a.b``）、类型强转、正则、跨字段约束——列为扩展点。

### 4.1 审批绑定的四条约束（W3）

1. **绑定调用与参数 hash**：批准的是"某次调用 + 某组参数"，不是模糊的授权范围；
   执行前用实际参数重算 hash 并比对。
2. **改参即换调用**：改参批准时原调用闭合为 `superseded`，另起新 `tool_call_id`
   （因此是新幂等键），批准绑定到新调用与新参数。
3. **nonce 不是一次性闩锁**：它是这次批准的审计标识；单次性由
   (tool_call_id, args_sha256) 绑定 + 调用闭合隐式保证——因此"批准后崩溃、恢复重跑"
   不需要重新审批，重复执行由 outbox/幂等层拦截（W3 窗口 4 实测 5/5 恰好一次）。
4. **过期与拒绝是默认方向**：`expires_at` 必须显式给出；auto-approve 绝不默认开。
   `scope=once` 只授权绑定的那次调用；`scope=session` 复用同一（工具，参数 hash）。

---

## 5. 不变量

* INV-001 同一 `tool_call_id` 至多一个 tool_call
* INV-002 tool_result 必须匹配一个未闭合的 tool_call
* INV-003 同一 tool_call 至多一个 tool_result
* INV-004 interrupt 不得在未 resume 时重复出现
* INV-005 resume 必须对应一个未闭合的 interrupt
* INV-006 终态（completed/failed）之后不得再出现树节点
* INV-007 resume 携带的 `interrupt_index` 必须与未闭合 interrupt 的 index 一致
  （index 与 id 双重核验：只对 id 不打分的实现会把 resume 值接到错误的 interrupt 上）
* INV-008 事件哈希链可续且内容与哈希一致（R-B3）：`event_hash = sha256(prev_hash ‖ record_bytes)`，
  `prev_hash` 必须等于上一条（或 genesis / fork 点）的 `event_hash`
  （违反 = 外部篡改指纹；不阻止篡改，只让篡改无法静默）

不变量的违反以 `Violation` 显式返回，调用方决定告警/修复/中止；
`DerivedState.fingerprint()` 用于重放确定性断言（同一日志 → 同一指纹）。

---

## 6. 与 LangGraph 的差异（待 W5 对照后补实测）

| 维度 | 本项目 | LangGraph |
|---|---|---|
| 日志主模型 | 扁平 append-only + 派生 lineage | 扁平 checkpoint + 派生 lineage |
| 历史改写 | 数据库触发器物理禁止 | 无强制 |
| 恢复时副作用 | 明确 at-least-once + unknown 对账 | 文档要求"节点从头重跑、副作用须幂等" |
| interrupt 匹配 | index 语义 + `interrupt_id` 显式核验 | 位置索引匹配 |

（W5 用同一批场景跑等价图，补齐代码量、恢复范围、改参语义等实测项。）

---

## 7. 未决问题

1. unknown 集合的收敛条件（何时可判定"不可对账只剩人工"）——W3 给出了产生规则，
   收敛策略待 W5 场景接入后定义。
2. ~~租约失效后的接管语义~~：R-B6 已实现最小版（持有/续租/过期失活 + 可读失败），
   **过期接管仍是开放的**（本包只做显式 acquire）。
3. 批量写一半（Saga 补偿）待 W5 场景集覆盖。
4. `synchronous=FULL` 的掉电语义实验。
5. 压缩的长期形态：当前每次压缩产生一条并列摘要，未做"摘要的摘要"；artifact GC 只有
   `orphan_count` 暴露，未实现回收（W7 之后）。
