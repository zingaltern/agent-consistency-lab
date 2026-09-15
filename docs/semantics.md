# 运行时语义（v1，W1 冻结）

本文件是 runtime 的**承诺清单**。代码、测试与后续实验结论都以本文件为口径；
任何实现改动导致承诺变化，必须先改这里。

---

## 1. 承诺

| 承诺 | 内容 | 由什么保证 |
|---|---|---|
| 执行语义 | **at-least-once**：中断/崩溃后恢复，未完成的步骤会重新执行 | 事件日志 + checkpoint 恢复算法 |
| 效果语义 | **至多一次（效果）仅当下游支持幂等键**；否则产生 `unknown`，进入对账 | 工具声明效果类别 + outbox（W3） |
| 日志语义 | **不重不漏**：同一事件永不重复写入，已提交事件永不丢失（kill -9 语义下） | 单写者事务 + append-only 触发器 + `UNIQUE(branch_id, seq)` |
| 状态语义 | 状态**从不被持久化为权威**，只有事件日志是权威；状态 = 日志的折叠 | `harness.state.reduce_events` |
| 恢复语义 | 已完成任务从 writes 重放（**不重跑副作用**），未完成任务从头重跑 | writes/checkpoint 分离 + `recovery_plan` |

**明确不承诺**：不承诺 exactly-once 效果（任何声称此承诺的系统都在某处依赖下游幂等），
不承诺掉电不丢最后一个事务（见 §3），不承诺跨进程并发写（见 §2）。

**W2 实测（n=5/格，共 60 次 SIGKILL + 60 次恢复，详见 [w2-crash-windows.md](w2-crash-windows.md)）**：
"效果已发生、记录未落盘"窗口下重复副作用 5/5，且 runtime 侧去重开关不改变结论——
该窗口只能由下游幂等兜住（下游幂等时 0/5）。

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
4. 恢复算法：`get_latest` → 重放其后 writes → 未完成 task 从头执行
   （`RecoveryPlan` 是该算法的可断言载体）。

writes 的保留索引（对齐 LangGraph 语义，负数供控制类使用）：
`-1 ERROR`、`-2 SCHEDULED`、`-3 INTERRUPT`、`-4 RESUME`；普通输出用 `>= 0`。

`metadata` 必须**原样保留未知键**（跨版本读取不丢字段）；运行时字段统一注入
在 `_runtime` 命名空间下。

### 2.5 工具执行的落盘顺序（有意为之）

一次工具执行按固定顺序落盘：

1. `tool_call` 事件（记录决策，此时副作用尚未发生）
2. 执行副作用（发生在外部系统）
3. `tool_result` 事件（**权威记录**）
4. `tool_calls` 去重行（辅助记录）
5. 边界 checkpoint 提交

事件先于去重行的原因：这样"去重行已写、事件未写"这个**危险**微窗口不存在；
反向残留（事件在、去重行缺失）由日志权威兜住，最坏只是审计少一行。
窗口 2（第 2 步之后、第 3 步之前）是唯一无法自愈的位置，见 §3。

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
跨进程并发需要上层租约（`lease` 事件，W3 落地）；在租约落地前，
双进程跑同一 thread 是未定义行为。

### 3.1 命名崩溃窗口

窗口是代码中的确定性注入点（`harness/chaos.py`），命中即 SIGKILL 自身进程。

| # | 窗口 | 位置 | 状态 |
|---|---|---|---|
| 1 | `pre_tool_exec` | 模型响应后 / 工具执行前 | 已实现并测（W2） |
| 2 | `post_tool_effect_pre_record` | 工具成功后 / 任何记录前 | 已实现并测（W2） |
| 3 | `post_record_pre_commit` | 记录后 / 边界 checkpoint 提交前 | 已实现并测（W2） |
| 4 | `post_approval_pre_exec` | 审批通过后 / 执行前 | W3 |
| 5 | `during_compaction` | 压缩进行中 | W4 |
| 6 | `after_resume` | 恢复之后再次崩溃 | 已埋点（W2），W3 并入矩阵 |

窗口 2 是唯一产生重复副作用的位置，且只能由下游幂等兜住（W2 实测 5/5 vs 0/5）。

---

## 4. 事件目录（payload 规范形状）

| type | kind | payload |
|---|---|---|
| `user_message` | tree | `{text}` |
| `agent_message` | tree | `{text, final?: bool, usage?: {...}}` |
| `tool_call` | tree | `{tool_call_id, tool, args, args_sha256, idempotency_key, effect}` |
| `tool_result` | tree | `{tool_call_id, status: executed\|failed\|unknown, result?, error_class?, artifact_ref?}` |
| `interrupt` | tree | `{interrupt_id, reason, request}` |
| `resume` | tree | `{interrupt_id, values}` |
| `error` | artifact | `{error_class, message, fatal?: bool}` |
| `budget_update` | artifact | `{bucket, tokens_in, tokens_out, cost_usd}` |
| `lease` | artifact | `{owner, expires_at}` |
| `compaction` | artifact | `{replaces_event_ids, summary, artifact_ref?}` |
| `approval` | artifact | `{approval_id, tool_call_id, decision, requested_args_sha256, approved_args_sha256, actor}` |
| `state_update` | artifact | `{patch}` |

---

## 5. 不变量

* INV-001 同一 `tool_call_id` 至多一个 tool_call
* INV-002 tool_result 必须匹配一个未闭合的 tool_call
* INV-003 同一 tool_call 至多一个 tool_result
* INV-004 interrupt 不得在未 resume 时重复出现
* INV-005 resume 必须对应一个未闭合的 interrupt
* INV-006 终态（completed/failed）之后不得再出现树节点

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

1. `resume_from_seq` 与 writes 重放的先后关系在"边界提交前又崩溃"时的幂等性
   证明（W2 用注入矩阵验证）。
2. unknown 集合的收敛条件（何时可判定"不可对账"）。
3. 租约失效后的接管语义（W3）。
