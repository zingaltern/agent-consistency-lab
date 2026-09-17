# 故障注入谱系（R-A2 / R-A3）：从 6 个采样点到整条时间轴

* 代码：`harness/chaos.py`（注入族）、`experiments/chaos_fuzz.py`（随机时刻驱动）、
  `opsenv/oracle.py`（外部账本判定）、`scripts/probe_*.py`（谱系探测器）
* 数据：`reports/chaos_fuzz_report.{md,json}`（一条命令再生）
* 状态：首版；结论的适用范围写在 §5

---

## 1. 为什么要把注入谱系拉开

命名崩溃窗口（`harness/chaos.py` 的 6 个）是**采样 6 个点**验证语义，不是验证整条时间轴：
它能回答"在这个位置崩会怎样"，不能回答"别的位置有没有一条我们没想到的重复路径"。
把谱系拉开有两层含义：

* **时刻**：从"第 N 次命中某代码位置"扩展到"墙钟定时"——覆盖命名窗口之外的位置；
* **物理**：从"kill -9"扩展到 SIGTERM（礼貌信号）、SIGSTOP（悬挂）、torn write（半事务）、
  账本外注入（runtime 不知道的外部事实）。这些正是语义文档 §3 里"文档声明不承诺"的区域——
  **不承诺 ≠ 没探测过**：边界同样需要有人证明"即使发生也不会静默腐烂"。

## 2. 时刻维度：随机时刻 SIGKILL fuzz

```bash
.venv/bin/python -m experiments.chaos_fuzz --repeats 30 --seed 20260917 \
    --json-out reports/chaos_fuzz_report.json --md-out reports/chaos_fuzz_report.md
```

设计要点（每一条都是踩过的坑或已知的失效模式）：

| 要点 | 做法 | 不这么做会怎样 |
|---|---|---|
| 注入族分离 | `--kill-after-ms` 是与 `CHAOS_WINDOWS` **独立**的新族；marker 记 `injection_kind` | 扩展 `CHAOS_WINDOWS` 语法会改动既有外部契约（见开放问题裁决 §一） |
| **窗口标定** | 先线扫描出"仍能杀死进程"的最大 K（本机当次：run 阶段 180ms / resume 阶段 35ms），再在其 15%–95% 采样 | 进程光启动就要几十毫秒；不标定就会全落在"早就退出了"的区域，**全绿只是因为没被杀到** |
| 对照面分类 | 每个保护组合显式声明 `expected_codes`：关 outbox 时重复是 W2 的已知语义 | 把已知语义当发现 ⇒ fuzz 变成噪声；反过来把发现当已知 ⇒ 假绿 |
| 判决与驱动分离 | 驱动可读 runtime 日志（决定跑 run/approve/resume），**判决只读外部账本** | 用被测对象的自述当裁决 = 自证 |
| CRN | 种子 = `seed + 组合序号×10 + 阶段序号`，不含 system/scenario 名 | 种子污染会产生伪影（W5 审计实测 p=0.0009） |

实测（`--repeats 30 --seed 20260917`，三次保护组合 × 两个注入阶段 = 180 次试验）：

* **180 次随机时刻注入全部落在标定窗口内并真的杀死了进程**；
* **新类违例 0 条**（四类不变量：日志未删改 / 调用闭合 / 账本会计 / 恢复收敛）；
* 敏感性自检**通过**：在"关 outbox + 命中窗口 2"的确定性配置上，oracle 报出了
  `inv_effect_accounting` —— 证明它的"全绿"不是因为瞎。

## 3. 物理维度：三档谱系探测器

```bash
.venv/bin/python scripts/probe_sigterm.py         --json-out /tmp/probe_sigterm.json
.venv/bin/python scripts/probe_tornwrite.py       --mode both --json-out /tmp/probe_tornwrite.json
.venv/bin/python scripts/probe_extra_ledger_row.py --json-out /tmp/probe_extra_ledger.json
```

| 谱系 | 对齐的承诺/边界 | 实测结论 |
|---|---|---|
| SIGTERM | 语义文档 §3："只对 kill -9 语义成立" | worker **没有**注册 SIGTERM 处理器 ⇒ 立即终止（退出码 −15），与 SIGKILL 同类；停止瞬间账本 0 或 1 行，恢复后仍 ≤1 行/键。**不存在"优雅关闭会 flush 未落盘内容"的额外语义** |
| torn write（半事务截断） | §2.1 物理禁止改写历史 + §3 的边界 | 三档截断下 resume 都给出**显式失败**（非零退出）且**没有**新增副作用。**措辞修正**（独立测试 P2-4）：50% / 90% 两档主库**打不开**（`DatabaseError`）；尾部 −4KB 那档主库**能打开**但 `PRAGMA integrity_check` 报 `*** in database main ***`——"不可打开"只对前两档成立，结论（绝不静默续跑）三档一致 |
| SIGSTOP → SIGCONT | §3："进程被 kill 时 OS page cache 仍在" | 22 次真实冻结（每次 20ms）期间，账本快照只出现 {0, 1} 两种取值、每键 ≤1；恢复后终态与未冻结运行一致 ⇒ 悬挂不需要运行时新增机制，**这一点是被证明的，不是被假设的** |
| 账本外注入 | §1 恢复语义：日志里已有结论的调用只重放 | 往 `world.db` 手工补 1 行（外部键 / runtime 自己的幂等键两种变体）后再 resume：账本行数**不变**，run 留在终态、不被复活；same-key 变体会让 oracle 报 `inv_effect_accounting`（账本 2 行且无 unknown 呈报）——那是**预期内**的，正说明 oracle 对账本层面的重复敏感 |

**停机规则**（设计文档 A §R-A3）：SIGTERM 这一档的结论是"运行时不需要新代码才能回应"
（它本来就没有 graceful 路径，而 SIGTERM 的默认语义就是终止）。因此这里**不新增**
graceful flush 逻辑：那属于语义变更（要先改 `docs/semantics.md` 的承诺），
是仓库所有者的决定，不是探测器的。相关方向列为开放问题。

## 4. oracle 的四条不变量

| 不变量 | 判定 | 为什么这么定 |
|---|---|---|
| `inv_no_tamper` | 分支内 `seq` 从 0 连续、`event_id` 全局唯一 | 这两条是"日志被删改/漏写"的可判定痕迹（与外部审计的 b3 口径同源） |
| `inv_closed_calls` | 不存在"executed/failed 工具行却没有 `tool_result` 事件" | 只查这一个方向：loop 是"先写事件、后写行"，行在事件必在；反向缺失在关掉 outbox 时是正常形态 |
| `inv_effect_accounting` | 账本每个幂等键 ≤1 行，或 runtime 显式呈报了 unknown | ">1 行且无呈报" = 静默重复，正是要抓的东西 |
| `inv_recovery_terminates` | 有限次 resume 内到达合法终态（completed / failed / waiting_human） | waiting_human 是脚本计划内的合法驻留，不算"卡住" |
| （附加）`inv_log_readable` | 主库读不出来 = 显式发现 | 否则 torn write 会被判成"没有违规"，探测结论整个反过来 |

**读库纪律**：全部判定都连 `-wal`/`-shm` 一起快照后再读（外部审计实测踩过"只拷主库把效果读成 0"的坑）。

## 5. 边界（本结论不适用的范围）

1. **只覆盖 kill -9 与"礼貌信号"**：掉电（`synchronous=NORMAL` 下可能丢最后一个事务）、
   `fsync` 语义、页面级位翻转不在范围内（位翻转与 `-wal` 截断留在设计文档 A §5 待办）。
2. **样本量口径**：180 次注入全绿只把失败率上界压到约 2%（rule of three），
   **不能**读成"覆盖了所有窗口"或"永不重复"。
3. **落点不可逐行复现**：注入**时刻**由 seed 决定且可复现；进程被杀的**代码位置**受机器时序影响。
   报告只主张前者。
   **连带后果**：同一条命令在负载不同的机器上，"实际杀死进程的试次"会有几个的浮动
   （采样时刻可能落在进程已经跑完之后）。因此 claim 只钉**结构量**（试次数 180）与
   **不变量**（无 marker 的试次 = 0、新类违例 = 0、敏感性自检通过），
   有效注入次数由报告如实记录当次值，不做成硬 claim。
4. **注入窗口是机器相关的**：报告里记下了当次标定值（180ms / 35ms）。换机器或换负载要重新标定——
   这是刻意保留的机器相关性，隐藏它会让"没被杀到"冒充"没发现问题"。
5. **窗口 2 的微秒级缝隙**：随机时刻注入几乎不可能恰好落在"效果已提交、`tool_result` 未写"
   的那一段（它是微秒级），所以该窗口的存在性由**确定性窗口注入**（敏感性自检）证明，
   随机注入主张的是相反方向："别处没有新类违例"。
6. **不涉及并发**：单写者、单进程假设不变；租约与跨进程并发仍未实现（见 `docs/semantics.md` §3）。
