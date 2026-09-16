# 开放问题裁决：设计文档 A §4 + B §6（含跨文档约束）

* 状态：已裁决（本文件是 A/B 两包开发的**第一个提交**，后续里程碑按此口径实现）
* 裁决依据：源码定位 + 一次可复现冒烟实验（脚本与产物在 `/tmp`，命令写在每条裁决里）
* 纪律：涉及"不得改变现有外部契约"时，以**不破坏既有契约**为优先；需要放宽语义的方向一律不做，
  写成开放问题上报（A §7 / B §8 边界纪律）

冒烟产物（本地，不入库）：`/tmp/openq-smoke/` —— `smoke_all.py`（驱动器）、
`report_sigstop.json`、`report_torn.json`、`report_ordinal.json`、`torn-full/`（截断副本）。

---

## 一、A §4.1｜`--kill-after-ms` 与 `CHAOS_WINDOWS` 的关系（跨文档裁决）

**结论：新增独立 flag `experiments.worker --kill-after-ms <ms>`；`CHAOS_WINDOWS` 的语法与语义一字不改。**
本裁决已回写 [B §7](2026-09-17-functional-wave1.md#7-与文档-a-的关系)（见本文件末节）。

依据（源码定位）：

1. `harness/chaos.py::parse_spec` 的语法是 `name[:occurrence]`，`WINDOWS` 是**封闭集合**
   （未知名字 `raise ValueError`）。把墙钟毫秒塞进同一字符串，必须改这个解析器——
   而 `CHAOS_WINDOWS` 是 HANDOFF §一 记录的外部契约（README 的"单次崩溃可手工复现"一节
   直接把它写进命令），改解析行为就是改契约。
2. 两种注入的**维度不同**：命名窗口是"第 N 次**命中**某代码位置"（进程内计数），
   墙钟定时与代码位置无关。合并到同一个 `dict[str, int]` 会让字段变成多态
   （名字 → 计数 or 毫秒），排查时无法区分。
3. 现有窗口注入的行为**零改变**是 R-A2 的硬要求；新 flag 只增加一条独立路径。

实现口径（供 A-M3 落地）：

* `Chaos` 增加独立的 `kill_after_ms` 参数（默认 `None` ⇒ 行为与今天完全一致），
  定时命中与窗口命中互不干扰；
* `crash_marker.json` 增加 `injection_kind: "window" | "time_hit"`；`time_hit` 时额外记
  `kill_after_ms`，`window` 时保持既有字段不变（只增字段，不改既有字段语义）；
* 冒烟：`CHAOS_WINDOWS` 与 `--kill-after-ms` 可同时给出，窗口注入仍按原语义触发。

---

## 二、A §4.2｜torn write 的变体选择

**结论：本期只做"主库（`runtime.db`）截断"一个变体，且截断点取三种（50% / 90% / 尾部 −4KB）；
`-wal` 截断与位翻转留在 A §5 待办，不进本期验收。**

冒烟证据（`report_torn.json`，源 run 为 `long_incident --long-steps 8` 完整跑完，`runtime.db` 344,064 字节）：

| 变体 | 截断后字节 | resume 退出码 | 打开库 | `PRAGMA integrity_check` | 外部账本（连 `-wal` 读） |
|---|---|---|---|---|---|
| 50% | 172,032 | 1（非零） | 失败 | `database disk image is malformed` | 1 行、单键最大 1 |
| 90% | 309,657 | 1（非零） | 失败 | 同上 | 1 行、单键最大 1 |
| 尾部 −4KB | 339,968 | 1（非零） | 失败 | 同上 | 1 行、单键最大 1 |

结论要点：

* 截断后的库**不可打开**，恢复路径给出的是**显式失败**（非零退出 + SQLite 的
  `DatabaseError`），并且**没有**发生任何新的副作用（账本行数不变）——
  这正是 R-A3 要求的"绝不静默按破损状态续跑"。
* 该失败目前是**未包装的 `sqlite3.DatabaseError` traceback**（`harness/store/sqlite_store.py`
  构造函数里的 `PRAGMA journal_mode=WAL` 抛出）。按 A-R3 的**停机规则**，不顺手加"修复"
  逻辑（那会引入新的运行时分支），探测器只做断言：退出码非零 + 账本不变 + 报错文本可读。
* 为什么不做 `-wal` 截断：`-wal` 是崩溃后**仍可能持有已提交事务**的那部分（外部审计
  实测踩过"只拷主库把效果读成 0"的坑，见 `docs/tester-prompt.md`），截断它测的是
  "读侧快照纪律"而不是"恢复路径"，与 R-A3 的最小探测器目标不同类；列为待办。

---

## 三、A §4.3｜SIGSTOP 后的 WAL 行为（先冒烟再写断言）

**结论（实测）：进程被真实冻结不会产生"可观测的中间态"；读侧（连 `-wal` 一起快照）只会看到
某个已提交版本。**

冒烟方法（`smoke_all.py::sigstop_smoke`，`report_sigstop.json`）：`run → approve → resume` 三阶段，
在 `resume` 阶段以 5ms 间隔反复 `SIGSTOP`、每次冻结 20ms 再 `SIGCONT`，**冻结期间**对
`runtime.db` 与 `world.db`（连 `-wal`/`-shm` 快照）各读一次。

实测结果（39 次成功冻结）：

| 观测量 | 冻结期间观测到的取值集合 | 终态 |
|---|---|---|
| 账本效果行数 | {0, 1}（**没有**其它值；且单键最大 ≤1） | 1 行、单键最大 1 |
| 事件日志 | 事件数 ∈ {39, 40, 41, 42}，每一次都快照到 `seq` 连续、`event_id` 唯一 | 42 事件、连续、唯一 |
| `tool_calls` 状态 | 全部 `executed`（无 `pending` 悬挂） | executed=9 |

要点：冻结期间出现 `effects=1` 的那几次，冻结点正落在"效果已提交、`tool_result` 事件未写"
的窗口里——恢复后仍然是**恰好 1 行**。因此探测器可以写成强断言：

* 冻结期间与终态的账本计数都满足"每键 ≤1 行"，且终态计数 = 冻结期间出现过的最大值；
* 冻结期间快照的事件日志始终 `seq` 连续、`event_id` 唯一。

（口径：这是**20ms 量级**的冻结，不是长时间挂起；也不覆盖"冻结跨机器重启"这类情形。）

---

## 四、A §4.4｜oracle 判定代码的归属

**结论：抽到新模块 `opsenv/oracle.py`；`experiments/chaos_fuzz.py` 与 `scripts/probe_*.py`
只做驱动与断言，判定逻辑一律调它。**

依据：

* 仓内**没有**既有审计脚本可复用——外部测试的 `b3_log_audit.py` 等只存在于对方的 `/tmp`
  工作目录（见 `docs/independent-test-2026-09-16/commands.sh`），仓库里没有任何 `audit` 模块；
  因此不存在"复用既有脚本依赖过重"的问题，只有"新写一份判定"。
* 判定属于**评测层**职责（"某次运行是否违反不变量"），而 `opsenv/` 已经是判据层
  （`opsenv/stats.py` 管统计、`opsenv/scenario.py` 管场景与 split），把 oracle 放这里
  与既有分层一致；`harness/` 是被测对象，不应包含"怎么判自己"的代码。
* 三个判定（`inv_no_tamper` / `inv_closed_calls` / `inv_effect_accounting`）只依赖
  `runtime.db`（事件、`tool_calls` 表）与 `world.db`（效果表），签名保持纯函数式
  （`(run_dir) -> list[Finding]`），便于 `tests/test_chaos_fuzz.py` 用合成数据单测。

---

## 五、B §6.1｜回放命中键：调用序号在 fork / 恢复下是否唯一

**结论：序号在**同一 run 目录内**不跳号也不重号（可作命中键），但**同一序号对应的提示词在
崩溃-恢复后可能合法地不同**；因此采用「序号命中 + 提示词归一化 hash 的**警告级**校验」，
不采用 `(序号, fingerprint)` 复合键。**

冒烟证据（`report_ordinal.json`）：同一条 `long_incident --long-steps 8` 轨迹跑两次，
第二次在 `resume` 阶段用 `CHAOS_WINDOWS=post_tool_effect_pre_record:1` 触发真实 SIGKILL
（`crash_marker.json` 记录 `counts={post_approval_pre_exec:1, pre_tool_exec:1, post_tool_effect_pre_record:1}`，
退出码 −9）后再 resume。两次运行的 10 次模型调用（`agent_message` 事件里的 `view_fingerprint`）：

```
baseline: c22535ed 27c123cc 0826a481 01cd0075 d1678706 2724d300 92a6880b 7ddbf5b3 1cb2790d 55a8b920
crashed : c22535ed 27c123cc 0826a481 01cd0075 d1678706 2724d300 92a6880b 7ddbf5b3 1cb2790d 26a8c203
                                                                                      ↑ 只有末次不同
```

论证：

1. **序号对齐**：调用序号由事件日志驱动——`Loop._drive` 的 `step` 来自
   `reduce_events`（`agent_message` 计数），而模型调用之后才写 `agent_message`。
   崩溃-恢复后重跑的调用落在**同一个 step** 上，因此序号既不跳号也不重号；
   实测两次运行的调用序列长度相同（10）、前 9 位逐位相同。
2. **提示词可能变**：末次不同是**恢复路径的合法结果**——窗口 2 崩溃时副作用已发生、
   记录未落盘，恢复走探针重建（`_resolve_pending` → `result` 带
   `reconstructed_from_probe: True`），模型看到的工具结果与未崩溃运行不同。
   这**不是** bug，是 at-least-once 语义的正常表现。
3. **因此**：
   * 命中键用序号（`record` 与 `replay` 都按 run 目录内的持久化计数递增，跨 run/approve/resume 连续）；
   * 未命中 ⇒ **可读错误**（报"缺第 N 次调用"并给出期望提示词摘要）；
   * 提示词 hash 不一致 ⇒ **警告级**（写入 `replay_warnings`），不判失败——若把它升级为
     硬错误，**恰恰会在崩溃恢复场景制造假 miss**（上表末次那一位），把正常语义误报成缺陷；
   * 复合键 `(序号, fingerprint 前缀)` 会引入同一个假 miss，且无法覆盖"恢复重建结果"这一类，
     故不采用，理由记录在 PR。
4. 已知不适用范围：**墙钟定时注入**（A-R2 的 `--kill-after-ms`）下，两次运行的冻结点不保证
   落在同一次调用边界，因此"同一序号 ⇒ 同一提示词"的对应关系只在**同一条崩溃轨迹**内成立；
   record/replay 的验收只要求"接受注入、机制不被破坏"，不要求跨轨迹逐调用一致。

---

## 六、B §6.2｜`event_hash` 的 payload 正则化

**结论：`record_bytes` 的唯一正确定义写进 `docs/semantics.md`（随 R-B3 落地），
与存储层的 payload 序列化保持一致：**

```
record = {event_id, run_id, branch_id, seq, kind, type, source, parent_id, payload, created_at}
record_bytes = json.dumps(record, sort_keys=True, ensure_ascii=False)
event_hash  = sha256(prev_hash ‖ record_bytes)          # prev_hash 为十六进制字符串；genesis 用常量
```

依据与理由：

* 存储层写 payload 用的就是 `json.dumps(payload, sort_keys=True, ensure_ascii=False)`
  （`harness/store/sqlite_store.py::append_many`）——链的输入沿用同一序列化，
  才能保证"同一逻辑事件在任何进程/任何机器上得到同一字节串"。
* **不**复用 `harness/tools.py::canonical_json`：它用 `separators=(",", ":")`，
  与存储层的字节表示不同；两套序列化并存时，"改动其中一套"会静默让历史链失效。
  `canonical_json` 已有 golden hash 测试（`tests/`），本期**不动它**。
* `payload` 用**解析后的 dict**而不是原始字符串：字符串里的空白差异不应该被当成篡改，
  而 `sort_keys` 保证同一 dict 的字节表示唯一。
* `trace_id` / `span_id` 与两个 hash 列**不进** record：前者是投影层标注（`harness/trace.py`
  明确"trace 是投影"），后者是链自身。
* fork 场景：子分支首个事件的 `prev_hash` 指向**分叉点事件**（祖先前缀截断点的尾部）的
  `event_hash`，与 `SqliteStore._next_seq_locked` / `effective_events` 的截断口径一致。

---

## 七、B §6.3｜`arg_policy` 的支持深度

**结论：M2 只承诺"一层字段 + 两种值域"，其余一律显式报错或列为扩展点。**

| 能力 | 本期 | 说明 |
|---|---|---|
| `{"field": <args 顶层键>, "allowed": [...]}` | ✅ | 实际参数该字段必须在 allowed 内 |
| `{"field": ..., "forbidden": [...]}` | ✅ | 命中即拒（与 allowed 同时给出时两条都要满足） |
| `{"field": ..., "min": x, "max": y}` | ✅ | 数值区间，含端点；非数值类型 ⇒ 拒绝 |
| 嵌套字段（`a.b`）、类型强转、正则、跨字段约束 | ❌ 扩展点 | 语义文档写明"未实现" |
| 策略定义非法（未知键、缺 `field`、既无 `allowed`/`forbidden` 又无区间、区间上下限类型不符） | ✅ 显式报错 | 在**工具注册期**校验，不留到执行期 |

执行点按对抗审查第 20 条：**执行 handler 前的最后一道门**，输入是实际 `request.args`
（不是审批记录里的 hash），因此"审批时 args 未含 forbidden 值、但实际执行含"这一档必须被拒。
哲学定位不变：策略由**工具自己声明**（自述安全域），gate 只执行，不是外部黑名单。

---

## 八、B §6.4｜`lease` 与 checkpoint 提交协议的时序

**结论：租约的权威仍是事件日志（`lease` artifact 事件），不新增第二权威表；
校验发生在 checkpoint 事务**之外**（先证权、后写），过期接管不做。**

```
   worker 进程                          runtime.db (events)              checkpoint 事务
   ─────────────────────────────────────────────────────────────────────────────────
   acquire(owner, ttl)
     └─ append lease{owner, expires_at} ──▶ [lease 事件]   （单写者事务内，BEGIN IMMEDIATE）
   ┌──── 以下每个 super-step ────────────────────────────────────────────────────────┐
   │  validate(owner):
   │    fold 日志取最后一条 lease 事件
   │      ├─ 不存在 / owner 不符 / now >= expires_at ⇒ LeaseNotHeld（可读失败，停在此处）
   │      └─ 有效 ⇒ 放行
   │                                                       ┌─ BEGIN IMMEDIATE
   │  loop 正常写路径（事件 append / writes / checkpoint） ─┤  ……COMMIT
   │                                                       └─ （事务内不读租约表）
   └─────────────────────────────────────────────────────────────────────────────────┘
   renew(owner, ttl) ── 再追加一条 lease 事件（同样走单写者事务）
```

要点：

* **不引入第二权威**：租约状态 = 日志里最后一条 `lease` 事件的折叠物（`ArtifactEventType.LEASE`
  已登记，`docs/semantics.md` §4 已有 payload 形状 `{owner, expires_at}`）。
* **先证权后写**：校验在事务外完成，事务内不重复校验——两层边界清楚，
  避免"事务里读日志"把 checkpoint 提交协议的时序搅在一起。
* **不做**过期接管、分布式协调、多租户；`state_update` 保持"已登记未实现"。
* 措辞纪律：不写"从此支持并发"；租约只是**入场条件**，一致性仍由外部账本裁决。

---

## 九、B §6.5｜live 测试的 CI 摆放

**结论：`pyproject.toml` 先注册 `markers = ["live: 真实 API 调用，默认不跑；显式 -m live"]`，
"无 key 即失败"的反例测试放在**默认**测试集里（它不需要 key），CI 永不执行任何在线调用。**

依据：

* 不注册 marker 时 `pytest -m live` 在空选集下以 **exit 5（no tests ran）** 退出，
  会把"反例测试"误判成失败或漏跑（B 文档对抗审查第 15 条）。
* 反例测试的语义是"没有 key 时启动即给可读失败并非零退出"——**本身不需要 key**，
  因此它属于默认集（保证 CI 覆盖），而"真的有 key 时才跑"的那部分用 `-m live` 标记，
  并在 CI 里通过**不传 `-m live`** 而永不执行；仓库内不得出现 key 与录制样本。

---

## 十、跨文档约束的执行情况

* A §4.1 的裁决（新增 `--kill-after-ms`，不动 `CHAOS_WINDOWS`）**已回写**
  [B §7](2026-09-17-functional-wave1.md#7-与文档-a-的关系)：两包对"注入面"的假设一致——
  B-M1 的崩溃兼容验收按两条独立路径做（`CHAOS_WINDOWS` 原样复跑 + `--kill-after-ms` 新路径）。
* B §5 的验收数字自 A-M1 起登记为 claim（`reports/documented-facts.json`），
  由 `scripts/check_facts.py` 对账。
