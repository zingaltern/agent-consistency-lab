# 运行时语义（v1，W1 冻结）

本文件是 runtime 的**承诺清单**。代码、测试与后续实验结论都以本文件为口径；
任何实现改动导致承诺变化，必须先改这里。

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

**预算**：``main / compaction / judge / tools`` 分桶，账本由 ``budget_update`` 事件构成，
跨进程恢复后重新折叠得到；dispatch 前检查、响应后复核，超限即 fatal。

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
跨进程并发需要上层租约：`lease` 事件类型已登记但**尚未实现**（没有生产者/消费者），
因此双进程跑同一 thread 目前是未定义行为——这一点在 README 与报告中口径一致。

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
| `lease` | artifact | `{owner, expires_at}` |
| `compaction` | artifact | `{compaction_id, replaces_event_ids, summary, artifact_ref, tokens_before, tokens_after, reason, round}` |
| `approval` | artifact | `{approval_id, tool_call_id, tool, requested_args_sha256, approved_args_sha256, decision, actor, nonce, issued_at, expires_at, scope, policy_version, edited, requested_args, approved_args}` |
| `state_update` | artifact | 类型已登记但**当前无生产者**（保留给未来的外部状态注入） |

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
2. 租约失效后的接管语义（lease 事件已定义，机制未实现）。
3. 批量写一半（Saga 补偿）待 W5 场景集覆盖。
4. `synchronous=FULL` 的掉电语义实验。
5. 压缩的长期形态：当前每次压缩产生一条并列摘要，未做"摘要的摘要"；artifact GC 只有
   `orphan_count` 暴露，未实现回收（W7 之后）。
