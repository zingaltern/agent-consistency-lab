# W2 实验报告：崩溃窗口矩阵

**日期**：2026-09-16 ｜ **代码**：`experiments/crash_matrix.py` ｜ **原始数据**：`reports/w2_crash_matrix.json`

## 方法

* 注入方式：代码中**命名的确定性窗口** + 命中即 `SIGKILL` 自身进程（无 cleanup、无 flush 兜底），
  崩溃前写 `crash_marker.json` 以证明"确实死在该窗口"。
* 裁判：外部**副作用账本**（独立 SQLite 文件，每次真实执行无条件追加一行）。runtime 的日志、
  checkpoint、去重表都可能因崩溃而不完整，只有账本能裁决"效果发生了几次"。
* 每次运行两阶段：`run`（注入崩溃）→ `resume`（新进程恢复，无注入）。
* 场景：读工具（`query_metrics`）×1 + 写工具（`scale_pool`）×1 + 终答；窗口指定第 2 次命中，
  即落在写工具上。
* 规模：3 窗口 × runtime 去重{on,off} × 下游幂等{on,off} × 5 次 = 60 次崩溃运行 + 60 次恢复运行，
  墙钟 8.4 秒。

## 结果

| 窗口 | runtime 去重 | 下游幂等 | 次数 | 标记命中 | 恢复成功 | 日志一致 | 出现重复副作用 | 单键最大效果数 | 与预期一致 |
|---|---|---|---|---|---|---|---|---|---|
| pre_tool_exec | off | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| pre_tool_exec | off | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| pre_tool_exec | on | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| pre_tool_exec | on | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_tool_effect_pre_record | off | off | 5 | 5/5 | 5/5 | 5/5 | **5/5** | **2** | as-predicted |
| post_tool_effect_pre_record | off | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_tool_effect_pre_record | on | off | 5 | 5/5 | 5/5 | 5/5 | **5/5** | **2** | as-predicted |
| post_tool_effect_pre_record | on | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_record_pre_commit | off | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_record_pre_commit | off | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_record_pre_commit | on | off | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |
| post_record_pre_commit | on | on | 5 | 5/5 | 5/5 | 5/5 | 0/5 | 1 | as-predicted |

## 结论（按"在 X 条件下 A 比 B 好/坏 Y"口径）

1. **在 `post_tool_effect_pre_record` 窗口下、下游不支持幂等键时，重复副作用 5/5 发生，
   单键最大效果数 2**；同一窗口下下游支持幂等键时 0/5 发生（单键最大 1）。
   即：该窗口的重复与否**完全由下游能力决定**，runtime 无法单方面兜住。
2. **runtime 侧的"后置记录 + 唯一键去重"在该窗口下无效**：12 格对比中，去重开关没有改变
   任何一格的结论（`5/5 → 5/5`、`0/5 → 0/5`）。原因很直接——崩溃点在记录之前，
   去重表里根本没有这一行可查。"加了幂等键就安全"的直觉在崩溃一致性上是错的。
3. **日志权威是有效的**：三个窗口下"日志不重不漏 + 不变量零违反"均为 5/5。
   `pre_tool_exec`（未执行）与 `post_record_pre_commit`（已记录未提交 checkpoint）都能恢复到
   恰好 1 次效果，说明"从最新 checkpoint 重放事件、未完成调用重跑"的恢复算法成立。
4. **孤儿 writes 不造成危害**：`post_record_pre_commit` 崩溃会留下指向未提交 checkpoint 的
   writes，恢复计划按已提交 checkpoint 读取，孤儿被忽略（见 `analyze()` 的 `orphan_writes` 计数）。

## 边界（必须与结论一起引用）

* **统计边界**：每格 n=5，0/5 只把失败率上界压到约 60%（rule of three），
  不足以声称"永不重复"。W6 会把 repeats 提到 30+ 并报置信区间。
* **崩溃语义边界**：只覆盖 `kill -9`（OS page cache 仍在）。掉电语义
  （`synchronous=FULL/NORMAL` 的差异）不在本轮范围。
* **并发边界**：单写者模型，未测多进程抢占同一 thread 的行为（W3 租约落地后再测）。
* **场景边界**：单工具单轮次；未覆盖批量写一半、压缩进行中、审批窗口（W3/W4）。

## 下一步（W3 的动机，由本报告的数据直接给出）

窗口 2 的问题不是"重试策略"能解的，只有两条路：**下游幂等**（已由本报告量化），
或者**把记录提前到执行之前**（outbox：intent(pending) → execute → result）。
后者要求恢复时能区分 `executed / failed / unknown`，并对 `unknown` 做 probe 对账而不是自动重跑。
W3 将实现 outbox + unknown 对账，并复用同一矩阵验证：
预期把 `post_tool_effect_pre_record × 下游不幂等` 这一格从"5/5 重复副作用"变成
"0/5 重复副作用 + N 条 unknown 待对账"。
