# W3 实验报告：审批绑定与 outbox 一致性

**日期**：2026-09-16 ｜ **代码**：`harness/approval.py`、`harness/loop.py`、`experiments/worker.py` ｜
**原始数据**：`reports/w3_crash_matrix.json`（14 格 × 5 次 = 70 次运行，其中 **65 次真的被 SIGKILL**——
篡改控制组按设计不注入崩溃；68 次恢复运行，20.1 秒）

> ⚠️ **历史快照**：本文是 W3 时的 14 格矩阵。W4 起扩到 16 格（新增 `during_compaction`
> 与长任务对照），后续数字以 `reports/w4_crash_matrix.md` 为准。

## 本轮新增的三块语义

1. **审批绑定**（`approval.py`）：批准 = (tool_call_id, approved_args_sha256, 过期, scope)。
   执行前用**实际参数**重算 hash 比对（堵 TOCTOU）；改参 = 原调用闭合为 `superseded`
   + 新 tool_call_id + 新幂等键 + 新批准（否则下游按键去重会拿旧参数的结果）。
2. **outbox**：非幂等写在**任何副作用之前**先落一行 `intent(pending)`，
   执行成功后闭合为 `executed`；失败/不可判读路径显式落地为 `unknown`。
3. **探针（ProbeFn）**：下游"按键读回"的接口。没有这个能力的下游，
   崩溃后的 pending 意图**只能**转 `unknown` 交人工对账——runtime 不假装它知道。

阶段协议：`run`（可能在审批门停下）→ `approve`（纯人工动作，只写事实不执行副作用）
→ `resume`（agent 工作，可能被 SIGKILL）→ `resume`（收尾）。

## 矩阵结果（全部 as-predicted）

| 格（窗口 × outbox × 下游幂等 × 探针 × 篡改） | 次数 | 效果数 | 重复出现 | unknown 行 | 探针对账 |
|---|---|---|---|---|---|
| post_tool_effect_pre_record, outbox=0, idem=0 | 5 | 2 | **5/5** | 0 | 0 |
| post_tool_effect_pre_record, outbox=0, idem=1 | 5 | 1 | 0/5 | 0 | 0 |
| post_tool_effect_pre_record, outbox=1, idem=0, probe=1 | 5 | 1 | 0/5 | 0 | **1** |
| post_tool_effect_pre_record, outbox=1, idem=0, probe=0 | 5 | 1 | 0/5 | **1** | 0 |
| post_tool_effect_pre_record, outbox=1, idem=1 | 5 | 1 | 0/5 | 0 | 0 |
| pre_tool_exec（outbox 0/1） | 5+5 | 1 | 0/5 | 0 | 0 |
| post_record_pre_commit（outbox 0/1） | 5+5 | 1 | 0/5 | 0 | 0 |
| post_approval_pre_exec（outbox 0/1） | 5+5 | 1 | 0/5 | 0 | 0 |
| after_resume（outbox 0/1） | 5+5 | 1 | 0/5 | 0 | 0 |
| (tamper) 审批后参数被改 | 5 | **0** | 0/5 | 0 | 0 |

## 结论

1. **在窗口 2 且下游不幂等的条件下，outbox 把重复副作用从 5/5 降到 0/5；有按键读回时
   恢复路径由探针确认（reconciled=1），无读回时收敛为恰好 1 行 unknown。**
   也就是说：outbox 没有消灭那个窗口，而是把"静默重复"变成了"可探测的重放"或
   "有界、显式的未知"。这两者 recovery 的语义完全不同：
   前者不需要人，后者必须人工对账（这正是代码里 `unknown` 三态存在的理由）。
2. **outbox 与下游幂等是两条互补的防线**：`outbox=0 idem=1` 与 `outbox=1 idem=0 probe=1`
   都达到效果 1，但前者靠下游 upsert、后者靠 runtime 对账——只有 outbox 行存在时，
   runtime 才知道自己面对的是"未知状态"而不是"未执行"。
3. **"批准后崩溃、恢复重跑"不需要重新审批**（窗口 `post_approval_pre_exec`：5/5 恰好一次）。
   实现：nonce 是审计标识而非一次性闩锁，单次性由 (tool_call_id, args_sha256) 绑定
   + 调用闭合隐式保证。若把 nonce 做成执行前消费的闩锁，这个窗口会把任务永久卡死——
   这是设计对比里最容易被面试官问到的一处取舍。
4. **TOCTOU 有实测护栏**：篡改组在批准之后改写参数（size 64→72），运行 5/5 拒绝执行、
   副作用 0、原因分类为 `args_sha256_mismatch`。
5. **改参即换键有了端到端证据**： 改参批准产生新调用 `tc_write_1__edit1`
   与**不同**的幂等键，账本里落的是改后的参数（size 32）。

## 失败复盘（本项目第二次真实翻车）

**风险点**：`_find_approval` 的 session 分支写完后，所有测试都会通过——因为这个分支
藏在 `a or b` 的第二个操作数里，而既有用例全是 once 批准（短路 + 短路），分支不可达。
**revolvi**：ruff 的 F821（undefined name `SCOPE_SESSION`）在 lint 时暴露；同时补了
两个双写场景的测试（`test_session_scope_authorizes_second_identical_call` /
`test_once_scope_requires_approval_for_each_call`），把该分支变成必经路径。
**教训**：短路掩盖的分支，测试覆盖不出来；要么用 lint（F821）要么用场景级用例，
不能指望"单测过了"等于"分支可达"。

## 边界

* 每格 n=5（0/5 的置信上界仍约 60%，rule of three）；W6 提到 30+ 并报 CI。
* `during_compaction` 窗口依赖 W4 的压缩实现，本轮尚未接入矩阵（W4 已接入，见 w4-report §三）。
* 探针假设下游支持"按幂等键读回"；没有这个能力的下游只能得到 `unknown`——
  **这不是缺陷而是边界**，矩阵里单列一格证明它的行为是"显式有界"而非"静默重复"。
* 批量写一半（Saga/补偿）仍未覆盖，计划并入 W5 场景集。
* 会话/前缀 scope 只有 `session`（同工具+同参数）实现；前缀匹配（codex 的
  approved-command-prefix 形态）未实现。
