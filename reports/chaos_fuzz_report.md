# 随机时刻 SIGKILL fuzz 报告（R-A2）

* 运行：`python -m experiments.chaos_fuzz --repeats 30 --seed 20260917`
* seed：`20260917`（扰动只决定注入时刻；种子不含 system/scenario 名）
* 注入族：`time_hit`（墙钟定时 SIGKILL，与命名窗口语义独立、可同时给出）
* 判定：`opsenv/oracle.py` 的四类不变量，读 `runtime.db` + `world.db`（连 `-wal` 一起快照）

## 注入窗口标定

| 保护组合 | 注入阶段 | 标定出的可杀死窗口上限 |
|---|---|---|
| 全开(outbox+probe) | run | 180 ms |
| 全开(outbox+probe) | resume | 35 ms |
| outbox开-无读回 | run | 180 ms |
| outbox开-无读回 | resume | 35 ms |
| 全关(无outbox) | run | 180 ms |
| 全关(无outbox) | resume | 35 ms |

## 对照面与结果

| 保护组合 | 注入阶段 | 试次 | 实际被杀 | time_hit marker | 新类违例 | 已知类违例 | 终态 |
|---|---|---|---|---|---|---|---|
| 全开(outbox+probe) | run | 30 | 30 | 30 | 0 | 0 | waiting_human |
| 全开(outbox+probe) | resume | 30 | 30 | 30 | 0 | 0 | completed |
| outbox开-无读回 | run | 30 | 30 | 30 | 0 | 0 | waiting_human |
| outbox开-无读回 | resume | 30 | 30 | 30 | 0 | 0 | completed |
| 全关(无outbox) | run | 30 | 30 | 30 | 0 | 0 | waiting_human |
| 全关(无outbox) | resume | 30 | 30 | 30 | 0 | 0 | completed |

* 总试次 180，退出码非零 180 次，其中**有 `time_hit` marker 证据的 180 次**（无 marker 的 0 次按失败计），**新类违例 0 条**，已知类违例 0 条（后者是被对照面显式声明的语义）。
* **敏感性自检**（已知会重复的配置下 oracle 必须报红）：通过——实测 findings=['inv_effect_accounting']。

## 结论与边界

* **180 次有 marker 证据的随机时刻注入未发现命名窗口之外的新类违例**。按 rule of three，这个次数只把失败率上界压到约 2%——**不能**据此声称「覆盖了所有窗口」或「永不重复」。
* 注入时刻由 seed 决定且可复现；**落点**（进程被杀的代码位置）受机器时序影响，因此不同机器上同一 seed 不保证死在同一行——本报告只主张「注入时刻可复现」，不主张逐行复现。
* 标定窗口是**本机当次**测出来的：换机器或换负载需要重跑标定（报告里记下了当次窗口）。这是刻意保留的机器相关性——隐藏它会让「没被杀到」冒充「没发现问题」。
* `全关(无outbox)` 组合的重复副作用是 **W2 已实测的已知语义**（窗口 2），在本报告里被显式分类为「已知类」而不是新发现；把它算成发现会让 fuzz 变成噪声。
* **为什么「已知类违例」是 0**：窗口 2 是「效果已提交、`tool_result` 未写」的**微秒级**缝隙，墙钟定时注入几乎不可能恰好落在里面——所以它的存在性由敏感性自检里的**确定性窗口注入**（`CHAOS_WINDOWS=post_tool_effect_pre_record:1`）证明，而不是由随机时刻证明。随机注入这一侧主张的是相反的方向：**没有**在别处发现新类违例。
* 本口径只覆盖 kill -9；掉电、跨进程并发、租约仍不在范围内（见 docs/semantics.md §3）。
