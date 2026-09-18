# 独立验证报告：设计文档 C 的外围集成交付

**被测 commit：`1d4b3f3710e15c629d34e7ab9fc65209a299ffbb`**（分支 `feat/integrations-boundaries`，四个堆叠分支的 tip）

* 验证日期：2026-09-18
* 验证者：独立测试 agent（与开发会话隔离；未修改被测仓库任何文件）
* 环境：macOS（darwin 25.6.0 arm64）／Python 3.14.6／`mcp` SDK **2.2.0**／pytest 9.1.1／mutmut 3.8.0
* 隔离副本：`/tmp/verify-mcp`（tip）、`/tmp/verify-main`（main，用于对照）
* 原始证据目录：`/tmp/ev/`（JSON／日志）、`/tmp/drv/`（自写驱动脚本）

---

## 0. 结论摘要（先读这段）

**这批 PR 的语义与功能面可以合；但变异门禁（`scripts/mutation_check.py`）目前守不住它自己声称守的东西，其中一条是 P0。合并前必须先修门禁的两处判据，或把它的承诺收窄成"防倒退"并在文档里写明它看不见什么。**

* **功能与治理语义（A/B/C/D/E 全部方向）：未发现一条被证伪。** 工具映射与 schema、只读对拍、审批三条路径、改参语义、错误 id 拒绝、elicitation 恒等、崩溃后从事件日志折叠、两格崩溃对照、可观测三条口径——全部用自写驱动复现通过（含"外部账本 0 行"与"恰好 1 行"两个硬指标）。
* **两条 P0 都在同一个地方：变异门禁的判据。**
  * **P0-1｜"缺项"被当成好消息。** 门禁只判"本轮 `survived` 里有没有新名字"。任何**从本轮结果里消失**的基线幸存变异（未评估、segfault、timeout、no-tests）都会落进 `fixed`，被打印成"已被杀死（好事，不判失败）"并**退出 0**。我构造了该场景（实测：180 条基线幸存变异全部缺失 → 输出"有 180 条…已被杀死" → 退出 0）。**这与交付汇报的判断方向相反**（汇报说"缺项会导致误报而不是漏报"，实测是**漏报**）。
  * **P0-2｜门禁抓不到"新增的、完全没被测的代码"。** 按 AGENTS.md 红线 5 做退化注入：在被变异模块里加一个**没有任何用例调用的函数**、再加一条**被覆盖函数里永不被执行的分支**，门禁**退出 0**（新增变异被判为 `no tests`，而门禁只看 `survived`）。同一实验的对照（等价但被覆盖的分支）也返回 0，说明这条路径整体不敏感。
* **P0-1 不是纸面风险，有活体实例：** `harness/store/checkpoints.py` 在 main 与 tip 之间**逐字未改**，但它的 94 条幸存变异里有 **76 条在 tip 上被 mutmut 判成 `segfault`（-11）**——而 `segfault` 不计入门禁的任何计数。`harness/approval.py`（同样逐字未改）的 3 条幸存变异在 tip 上**恰好是同一批名字**被判成 segfault。我抽验了其中一个：手工应用该变异体后**整套 387 条默认用例全绿**——它是一条**真幸存变异**，被 mutmut 误判成崩溃。
* **一句话的可操作结论：** 语义交付可信，门禁不可信。门禁要么修（把"消失的基线变异"与"`no tests` 新增"都变成红灯），要么把 `reports/mutation_baseline.json` 与文档里的"幸存率"降格为参考值、不再声称它是防盲区的门。

---

## 1. 方法与证据链

纪律：先读规格（`docs/semantics.md`、`docs/design/2026-09-18-integrations.md`、`docs/integrations.md`、
`integrations/observability.md`、README 外围集成小节、源码与 `--help`）。**读汇报与测试的时点如实说明**：
A／B／C 三组的结论在读完规格后即冻结并跑完（当时未读交付汇报）；D／E 在读过交付汇报之后完成，
但结论只取实测值；F 组的两条核心主张（"缺项会导致误报而不是漏报"、"2023 vs ~1110 不是本次改动引入"）
本身就是交付汇报里的句子，**无法先冻结**——因此这两条我以"构造反例 + 实测输出"的方式处理，而不是以读代码的方式。

独立性的具体做法（**没有复用作者的任何测试代码路径**）：

| 手段 | 实现 | 位置 |
|---|---|---|
| MCP 客户端驱动 | 自己写：官方 SDK 的 `stdio_client` + `ClientSession`，按需声明／不声明 elicitation | `/tmp/drv/mcpctl.py` |
| 外部账本读取 | 自己写：`world.db` 连 `-wal`/`-shm` 一起快照后再读 | `/tmp/drv/ledger.py` |
| 事件日志读取 | 自己写：同样快照 `runtime.db` 后按 `seq` 取事件 | `/tmp/drv/*.py` 内 |
| 崩溃对照 | 自己写两格驱动（只让 `--outbox` 不同），并用 `/bin/sh` 记退出码 | `/tmp/drv/c_crash.py` |
| 依赖方向 | 自己写 AST 扫描 + 子进程反向断言 | `/tmp/drv/e_ast.py`、`/tmp/drv/e_reverse.py` |
| 一致性对拍 | 自己写：`Loop`（fakeworld 自带剧本）vs 真 MCP 服务进程，投影到与随机 id 无关的形状后逐位比较 | `/tmp/drv/f_parity.py` |
| 门禁退化注入 | 在 `/tmp` 副本里改被测代码／配置，跑门禁看是否变红，再还原 | 见 §5 |

所有"崩溃"都是真 SIGKILL：`harness/chaos.py` 的 `os.kill(getpid(), SIGKILL)`，退出码由 `/bin/sh` 记录为 137。

---

## 2. 各方向结论表

### A. MCP 工具服务（C-G1 / R-C1）

| # | 检查项 | 结论 | 证据要点 |
|---|---|---|---|
| A1 | `--list-tools` 与注册表一致；每个下发工具都带 schema | **通过** | `tool_count=6`（4 注册表 + 2 治理）；6 个工具全部带 `inputSchema`（`list_pending_approvals` 是刻意的空 `properties`，非"缺失"）；`read_artifact` **确认未下发**，且 `docs/integrations.md` §6.4 写明了原因 |
| A2 | 只读工具调用 vs 直接调 `harness` 对拍 | **通过** | 经真 stdio 会话调 `query_metrics` → `{"service":"payment","p99_ms":2100,"pool_wait_ms":1800,"replicas":4}`；进程内 `ToolExecutor.execute` 同一入参 → 逐字相同；`text` 块恰为 `structured_content` 的 JSON |
| A3 | 大结果行为 | **通过（有口径缺口，见 P2-1）** | `fetch_logs(lines=200)` 返回 **23 243 字符、200 条 entries**，**原样内联**：无 `digest`、无 `offloaded`、无 artifact 引用 → **不存在"卸成 artifact 却读不回来"的死路**。但工具 schema 的文案仍写着"大结果会被卸载为 artifact"，且**没有任何文档说明 MCP 形态下的大结果行为** |
| A4 | 失败语义可读（四种） | **通过（一种与 C 的列举不符，见 P2-2）** | 未注册工具 → `failed`/`unknown_tool`；工具抛异常 → `failed`/`tool_error:ValueError`（用 `lines="abc"` 触发，真实异常）；被 ArgPolicy 拒 → `rejected`/`arg_policy_violation`（**在批准之后**才拒，闸门在最后一刻，账本 0 行）；**参数不合 schema → 不报错，静默成功**（`service=12345` 被原样执行） |

### B. 治理语义（C-G2 / R-C2）

| # | 检查项 | 结论 | 证据要点 |
|---|---|---|---|
| B5 | 写操作停在审批门、账本 0 行、不阻塞不静默 | **通过** | `scale_pool` → `status=pending_approval`、`isError=false`、`run_status=waiting_human`；此刻外部账本 **0 行**；进程继续服务后续请求 |
| B6 | approve / reject / edit 三条路径 | **通过** | approve → 执行、账本 1 行、参数 `size=64`；reject → `executed=[]`、账本 **0 行**、调用闭合为 `rejected`；edit → 原调用闭合为 **`superseded`**（`superseded_by_edit`）、新 `tool_call_id` = `…__edit1`、**新幂等键**（`idem_ad72…` → `idem_0903…`）、新 approval 绑定新调用、账本落的是**改后的参数 `size=128`** |
| B7 | 拿不存在／别人的 `tool_call_id` 批准 | **通过** | 返回 `tool_call_id_mismatch` 并附当前待审批 id；账本仍 0 行；重复 approve 同一 id → `no_pending_interrupt`、账本仍 1 行 |
| B8 | 改参后的 TOCTOU | **通过** | 共用管线的 tamper 钩子：批准 `size=64`、执行前改写成 `512` → `rejected`/`args_sha256_mismatch`、账本 **0 行**；对照（不篡改）→ `executed`、账本 1 行。**MCP 形态下该窗口对客户端不可达**：服务没有 `--tamper` 注入面，且 `approve` 在同一请求内立即续跑、参数取自日志 |
| B9 | 不依赖 elicitation | **通过（比作者更强的一条）** | 客户端**声明** elicitation 与不声明，两次运行的整份回执（归一化随机 id 后）**逐字段相同**，且服务端 elicitation 请求数 **0**。作者只断言"从未被调用"，我验的是"两条路径产出相同" |
| B10 | 无第二权威（强杀后重启） | **通过** | 真 SIGKILL（退出码 **137**、`crash_marker.json` 有 `injection_kind/window/occurrence/pid`）→ 重启：待审批列表与崩溃前**同 id、同 `args_sha256`、同 `interrupt_id`、同 `interrupt_index`**（即从事件日志折叠而来）；`approve` 崩溃前提出的审批 → 执行、账本 1 行，**行为明确不静默**。run 目录里的文件只有 `ids.json`/`runtime.db`/`world.db`/`crash_marker.json`（外加我自己的 `/bin/sh` 包装写下的 `exit_code.txt`），**没有会话数据库或内存副本落盘** |

补充观察（非缺陷，建议登记）：`scope=session` 的批准对"同工具 + 同参数 hash"的后续调用可复用（这是既有语义）；
不同参数一律拒（实测 `rejected`）。第二次同参数调用会**再执行一次**并新增一行账本——因为幂等键含 `tool_call_id`，两次调用的 id 不同。这与 `docs/semantics.md` 的幂等键定义一致，不是缺陷。

### C. 崩溃与恢复（C-G3 / R-C3）

| # | 检查项 | 结论 | 证据要点 |
|---|---|---|---|
| C11 | 复现作者的两格对照 | **通过（两次独立运行结果一致）** | `with_outbox`：账本同一幂等键**恰好 1 行**、`reconstructed_from_probe=true`；`without_outbox`：**2 行**、`reconstructed_from_probe=false`。两格退出码均 137 |
| C12 | 单变量：只有 outbox 不同 | **通过（逐项 diff 过）** | 自写驱动显式列出两格全部配置项（`--outbox`/`--tool-idem`/`--probe`/崩溃期 `CHAOS_WINDOWS`/重启期 `CHAOS_WINDOWS`/cwd），**唯一差异项 = `--outbox`** |
| C13 | 崩溃证据齐全 + 可复现 | **通过** | 两格 `exit_code=137`；marker 的 `window=post_tool_effect_pre_record`、`occurrence=1`、`counts`、`pid` 齐全；作者脚本连跑两次 + 我的自写驱动一次，三次结论一致 |
| C14 | 恢复结论真的来自探针对账 | **通过** | 我的自写检查：崩溃后账本 1 行且**日志里 0 条 `tool_result`**（证明恢复段确实要干活）；重启后账本仍 1 行、`tool_result` 的 `result.reconstructed_from_probe=true`、意图行状态为 `executed` → 是**探针收敛**而不是"碰巧没重跑" |

### D. 可观测（C-G4 / R-C4）

| # | 检查项 | 结论 | 证据要点 |
|---|---|---|---|
| D15 | 本机接收器自测 | **通过** | `span_count=7`、`tool_span=2`、`model_span=3`、`derived_duration_span_count=3`、`derived_flag_matches_model_spans=true`、**`has_ledger_attributes=false`** |
| D16 | 三条口径 | **通过（含独立反向断言）** | ① 时长推导：`duration_is_derived` 恰好标在 3 个模型 span 上、不多不少（我按属性表逐 span 核对）；② 事后导入：`request_paths == ['/v1/traces']`，**恰好一次 POST**；③ **trace 里没有账本**：我不看作者的 `has_ledger_attributes`，而是把 7 个 span 的**全部属性键**列出来逐个查——没有任何键提到账本/次数/outbox/effects |
| D17 | 诚实性 | **通过** | `integrations/observability.md` §1 的表格逐条标注"已实测/本机未实测"，Jaeger 写明"本机没有 `docker`"、Grafana Cloud 写明"需要凭据与外网"；§5 明确"不要把 trace 当证据链"。未发现把未验证写成已验证 |

### E. 边界与文件管理（C-G5 / R-C5）

| # | 检查项 | 结论 | 证据要点 |
|---|---|---|---|
| E18 | 依赖方向（自写 AST + 反向） | **通过** | 自写 AST 扫描 `harness/ opsenv/ fakeworld/ experiments/ scripts/ examples/` 共 **59 个文件**，`integrations` 违规 **0**；子进程断言 `import harness.*` 之后 `sys.modules` 里 `mcp*`/`integrations*` 泄漏 **0** |
| E19 | 内核依赖 | **通过** | `[project].dependencies` 仍**只有 `pydantic>=2.7`**；`dev` 与 `eval` 都**不含** mcp（`mcp>=1.2,<3` 只在 `[mcp]`） |
| E20 | verify 时长与用例数 | **通过** | 同口径（`pytest -q`，默认 addopts）：main **341 passed / 1 skipped / 3 deselected，7.45 s** → tip **387 passed / 1 skipped / 17 deselected，9.23 s** = **+1.78 s**，与声称的 +1.8 s 一致、未超 5 s 预算。用例数 345 → 405 的 +60 **全部来自 6 个新增集成文件**（parity 7 / boundaries 10 / crash_demo 5 / mcp_mapping 23 / mcp_server 9 / observability 6），无一个既有文件改变用例数 |
| E21 | 措辞纪律 | **通过** | `grep -rn "平台\|服务化\|生产级\|高可用" integrations/ docs/integrations.md` → **0 命中**（我自己跑过） |
| E22 | `docs/semantics.md` 零改动 | **通过** | `git diff main..1d4b3f3 -- docs/semantics.md` **为空** |

### F. 高风险项（本清单最重要的部分）

| # | 检查项 | 结论 | 证据要点 |
|---|---|---|---|
| F23① | 重建基线是否必要 | **必要** | tip 的 180 条幸存变异里有 **162 条**的名字不在 main 的基线里（管线代码从 `harness.loop.xǁLoopǁ_*` 搬成 `harness.execution.xǁToolExecutorǁ_*`）→ 拿旧基线跑新代码会判出 162 条"新盲区"并**退出 1**。不重建就是假红 |
| F23② | 重建后仍能抓新盲区 | **未通过（P0-2）** | 按 AGENTS.md 红线 5 注入"测试覆盖不到的错逻辑"（一个无任何用例调用的新函数 + 一条永不被执行的分支）→ `mutation_check.py` **退出 0**、打印"没有新增幸存变异"。新增变异被判为 `no tests`（0 → 12 条），而门禁只比较 `survived` |
| F23③ | 存活率下降能否解释 | **部分解释，含一处被"顺掉"** | main：625/(1031+625)=**0.3774**（3 模块）；tip：180/(912+180)=**0.1648**（4 模块）。可解释的部分：管线搬进 `execution.py` 后覆盖变好（`loop.py` 幸存 528 → `execution.py` 161）。**不可解释的部分**：`harness/store/checkpoints.py` **逐字未改**，幸存数却 94 → 18，其中 **76 条被 mutmut 改判为 `segfault`** 而**不计入门禁任何计数**；`harness/approval.py` 逐字未改，3 条幸存变异（名字与 main 基线**完全相同**）在 tip 上全部变成 `segfault`。⇒ 存活率的下降里有相当一部分是**分类口径变化**，不是"测试变强" |
| F24 | "缺项"判误报还是忽略 | **忽略（P0-1，与作者声称相反）** | 构造场景：本轮结果只含 1 个模块的 26 条变异，基线里另外 **180 条幸存变异全部缺失** → 门禁输出"有 **180** 条基线里的幸存变异已被杀死（**好事，不判失败**）"→ **退出 0**。缺项**不可能**导致红灯（`new_survivors` 只会因此变短），只会导致静默绿 |
| F25 | 2023 vs ~1110 是否先于改动 | **不成立（P1）** | main：`print-time-estimates` 列出 **1670**，基线计入 **1670** → **相等，没有差距**。tip：列出 **≈2022**，基线只计入 **1110** → 差 **912**。我定位到差的构成：`.meta` 里 **906 条 `-11`（segfault）+ 6 条 `-24`（timeout）**，而 `mutation_summary` 的 `total` 只统计 `killed/survived/no_tests`。⇒ "只评估 ~1110 个"不准确（mutmut 实际评估了 ~2022 个），且**这个量级的差距在 main 上不存在** |
| F26 | 一致性证明独立复验 | **逐字相同** | 自写驱动：进程内 `Loop`（fakeworld 自带剧本）+ 真 MCP 服务进程跑同一场景（只读 + 需审批的写 + 批准）。把管线事件投影成与随机 id 无关的形状后，**7 条事件逐位相同**（含 `args_sha256`、`effect`、`interrupt_index`、approval 的 `decision/edited/approved_args/scope`、`resume.decision`、`tool_result` 的 `status/result`）；**外部账本逐行相同**；工具执行计数 2 与 MCP 侧 2 条 `tool_result` 吻合。**唯一差异是随机 id**（`tool_call_id`/幂等键/`interrupt_id`/run·branch id）——因为 MCP 服务建自己的 run，这是必然且已登记的 |
| F27 | 作者修的两个缺陷 + `Loop.resume` | **通过（一处措辞需更正）** | ① **`resume_open_calls` 异常穿透**：把修复改回去（去掉 `except InterruptSignal` 改为直接抛出）→ **2 条用例变红**（`test_second_write_queues_behind_the_current_interrupt`、`test_recover_on_start_reports_a_pending_interrupt_instead_of_duplicating`）→ 回归用例存在且**可证伪** ✓ ② **registry 单一来源**：守它的是结构扫描 + 委托 spy；我注入"把管线私有件搬回 `loop.py`"→ `test_pipeline_lives_only_in_the_execution_module` **变红并给出可读违规清单** ✓ ③ `Loop.resume`：**控制流确实变了**（函数体从内联循环改成 `self._exec.resume_open_calls(...)` 委托），但作者**原文**声称的是"`harness/loop.py` 的**公开方法签名**与**事件落盘顺序**逐字不变"——这两条我独立核过：`start/resume/approve` 签名**逐字相同**，事件顺序由 F26 与既有用例守住 ✓。**注意：任务书里"控制流逐字未变"的转述与作者原文不同，按作者原文核，属实。** |

### G. 非回归

| # | 命令 | 结果 | 与声称对照 |
|---|---|---|---|
| G28a | `pytest -o addopts= -p no:cacheprovider -q` | **404 passed, 1 skipped**（共 405 收集） | 汇报写"405 passed"；实为 404 + 1 skip（该 skip 在 main 上同样存在）→ **P2-4** |
| G28b | `pytest -m mcp`（默认 addopts） | **14 passed, 391 deselected** | 一致 ✓ |
| G28c | `ruff check .` | **All checks passed** | 一致 ✓ |
| G28d | `opsenv.suite --per-fault 8 --repeats 3 --gate` | **14/14 通过，退出 0** | 一致 ✓ |
| G28e | `experiments.crash_matrix --repeats 5` | **16 格 as-predicted、80 次运行、70 次真 SIGKILL、0 违例** | 一致 ✓ |
| G28f | `scripts/check_facts.py --run verify` | **38/38 通过** | 一致 ✓ |
| G28g | 缺 `[mcp]` extra 时 `pytest -m mcp` 应为 skip 而非 collect error | **通过** | 用例内部用 `importorskip`，且 `mcp` marker 已在 `pyproject.toml` 注册（避免空选集 exit 5）✓ |

---

## 3. 缺陷列表（含最小复现）

### P0-1｜变异门禁把"缺项"当好消息（静默漏检）

* **现象**：门禁只比较"本轮 `survived` 里有没有新名字"。**从本轮结果里消失的基线幸存变异**既不会触发红灯，也不会被质疑——它落进 `fixed`，被打印成"已被杀死（好事，不判失败）"，函数返回 0。
* **最小复现**（在 `/tmp` 副本里跑；`--module` 用变异体名 glob，见 P1-1）：

```bash
cd /tmp/verify-mcp
.venv/bin/python scripts/mutation_check.py --module "harness.approval.*" --timeout 600
# 输出（实测）：
# [mutation] killed=26 survived=0 no_tests=0 survivor_rate=0.000
# [mutation] 有 180 条基线里的幸存变异已被杀死（好事，不判失败）
# [mutation] 没有新增幸存变异：测试盲区没有扩大
# 退出码 = 0
```

  本轮只评估了 26 条变异，基线里另外 **180 条幸存变异全部缺失**，门禁**退出 0**。
* **判据方向**：`new_survivors = [n for n in 本轮survived if n not in 基线survivors]`——缺项只会让这个列表变短，**在结构上不可能导致红灯**。因此交付汇报"缺项会导致**误报**而不是漏报"的判断是反的。
* **活体实例**（不依赖 `--module`）：`harness/store/checkpoints.py` 逐字未改，76 条基线幸存变异在 tip 上被改判为 `segfault`；`harness/approval.py` 逐字未改，3 条幸存变异（名字与 main 基线完全相同）全部改判为 `segfault`。这些名字一旦从 `survived` 消失，门禁就再也看不见它们。
* **影响**：门禁声称守的是"新增代码把盲区扩大"，实际守的只是"新增代码里**已被覆盖**的部分有没有留下幸存变异"。基线里任何一条盲区只要"消失"，就会被当成修好了。
* **建议修法**：把"基线幸存变异里本轮**没有结论**（`not checked`/`segfault`/`timeout`/`no tests`/未生成）"单独列为一类并**判失败**（或至少判警告 + 明确打印计数），只有"确实被 killed"才算好消息。

### P0-2｜门禁抓不到"新增的、完全没被测的代码"（AGENTS.md 红线 5 的退化注入未通过）

* **现象**：在被变异模块里新增**没有任何用例覆盖**的逻辑，门禁退出 0。
* **最小复现**（退化注入，跑完已还原）：

```bash
cd /tmp/verify-mcp
# 1) 在 harness/approval.py 里加一个无任何用例调用的函数 _gate_probe_uncovered
#    并加一条永不被执行的分支（见 /tmp/drv/ 与报告 §1 的注入脚本）
.venv/bin/python scripts/mutation_check.py --module "harness.approval.*" --timeout 600
# 实测输出：
# [mutation] killed=27 survived=0 no_tests=12 survivor_rate=0.000
# [mutation] 没有新增幸存变异：测试盲区没有扩大
# 退出码 = 0   ← 红线 5 要求这里必须非 0
```

  新增的 12 条变异被判为 `no tests`，而 `mutation_summary` 只把 `survived` 当盲区，`no_tests` 只进 `total` 不进判据。
* **对照（排除"我的注入没被 mutmut 看见"）**：与同范围的未注入控制运行相比，`killed` 26 → 27、`no_tests` **0 → 12**——**证明注入的代码确实被生成并评估了变异体**，只是判据不看 `no_tests` 这一类。
* **影响**：AGENTS.md 红线 5"每条新门禁须做退化注入验证（把机制改坏 → CI 必须变红）"在本门禁上**不成立**。
* **建议修法**：把"本轮 `no tests` 里出现基线没有的新名字"与"基线 `no_tests` 集合之外的新增 `no_tests`"一并判失败；或至少要求"被变异模块里每个函数的 `no tests` 数量不得增长"。

### P1-1｜`mutation_check.py --module` 不可用（文档化选项恒失败）

* **现象**：`--module` 帮助写"只跑一个模块（默认跑配置里的三个）"，传的是**文件路径**（`DEFAULT_MODULES` 就是路径），但 mutmut 3.8 的 `run` 位置参数是**变异体名**（支持 fnmatch），路径匹配不到任何东西 → 断言失败。
* **最小复现**：

```bash
cd /tmp/verify-mcp
.venv/bin/python scripts/mutation_check.py --module harness/approval.py --timeout 120
# [mutation] `mutmut run` 退出码 1：本轮没有跑成功，判定作废。
#   AssertionError: Filtered for specific mutants, but nothing matches
#   Filter: ('harness/approval.py',)
# 退出码 = 1
```

* **影响**：fail-closed（不会静默通过），但本地迭代/缩小范围这条文档化路径**完全不可用**；`--module "harness.approval.*"`（变异体名 glob）才可用。
* **建议修法**：把 `--module` 的语义改成"变异体名 glob"并在帮助里写明，或在脚本里把路径翻译成 `harness.approval.*` 形态。

### P1-2｜"2023 vs ~1110 不是本次改动引入"不成立

* **现象**：作者称 `print-time-estimates` 列 2023 而 `run` 只评估 ~1110，且"这不是本次改动引入的"。实测：**main 上列出 1670、基线计入 1670，两者相等**；差距出现在 tip（列出 ≈2022、计入 1110）。
* **最小复现**：

```bash
# main
cd /tmp/verify-main && .venv/bin/python -m mutmut print-time-estimates | grep -cE '^<'   # → 1670
.venv/bin/python -c "import json;print(json.load(open('reports/mutation_baseline.json'))['counts'])"
# → {'killed': 1031, 'survived': 625, 'no_tests': 14, 'total': 1670}
# tip
cd /tmp/verify-mcp && .venv/bin/python -m mutmut print-time-estimates | grep -cE '^<|^[0-9]'  # → ≈2022
.venv/bin/python -c "import json;print(json.load(open('reports/mutation_baseline.json'))['counts'])"
# → {'killed': 912, 'survived': 180, 'no_tests': 18, 'total': 1110}
```

* **差的构成（我定位到）**：`mutants/harness/*.meta` 的 `exit_code_by_key` 里 **906 条 `-11`（mutmut 状态 `segfault`）+ 6 条 `-24`（`timeout`）**，而 `mutation_summary` 的 `total = killed + survived + no_tests` 不含这两类。1110 + 912 = 2022。
* **更正措辞**：mutmut **确实评估了** ~2022 条；只是其中 912 条没得到 `killed/survived/no_tests` 判决。
* **影响**：基线"有效"这个结论依赖 P0-1/P0-2 的判据，而那两处不成立（见上）。

### P1-3｜mutmut 把真幸存变异误判成 `segfault`（P0-1 的活体来源）

* **现象**：约 **45%** 的变异体（tip：906/2022）得到 `segfault`（-11）判决，而这个判决**不计入门禁的任何计数**。抽验发现其中至少一部分是**误判**。
* **最小复现**（在 `/tmp` 副本里，跑完已还原）：

```bash
cd /tmp/verify-mcp
# 该变异体：is_expired 里 10**9 → 10**10（语义等价）
.venv/bin/python -m mutmut apply "harness.approval.xǁApprovalBindingǁis_expired__mutmut_9"
.venv/bin/python -m pytest -o addopts= -p no:cacheprovider -q -m "not live and not mcp"
# → 387 passed, 1 skipped   ← 它根本不是崩溃，是一条**真幸存变异**
.venv/bin/python -m mutmut results --all=true | grep is_expired__mutmut_9
# → harness.approval.xǁApprovalBindingǁis_expired__mutmut_9: segfault   ← 判决错误
```

  另一组更硬的证据：`harness/approval.py` 逐字未改，main 基线的 3 条幸存变异与 tip 上 3 条 `segfault` 的**名字完全相同**（`is_expired__mutmut_1`、`is_expired__mutmut_4`、`validates__mutmut_5`）；`checkpoints.py` 逐字未改，94 → 18 条幸存、76 条变 `segfault`。
* **根因未定位**（见 §4）。但"判决不可信"这一点由上面的反例直接证明，不依赖根因。
* **影响**：门禁的可见变异空间只有 ~54%（1110/2022），且**哪一条被丢掉取决于 mutmut 的分类**，不取决于测试是否真的覆盖了它。

### P2-1｜大结果在 MCP 形态下原样内联，而 schema 文案仍承诺"卸载为 artifact"

* **现象**：`fetch_logs` 的 `inputSchema` 里写着"大结果会被卸载为 artifact"（该 schema 与下发给真实模型的那份同源）。在 MCP 形态下**没有任何卸载**：`lines=200` 的结果 23 243 字符原样内联返回，无 `digest`。
* **最小复现**：见 `commands.sh` 的 `a_functional` 一节；关键输出 `entries_count=200`、`json_chars=23243`、`has_digest_key=false`。
* **为什么不是 P0**：没有产生"客户端拿到一个读不到的引用"的死路（因为压根没有引用）。
* **口径缺口**：`docs/integrations.md` §6 只说了"不下发 `read_artifact`"，**没有说明 MCP 形态下大结果会原样内联**（卸载发生在 `harness/context.py` 的视图渲染层，而 MCP 服务不走渲染层）。外部 agent 读到"会被卸载"的 schema 文案会预期错误。
* **建议**：在 `docs/integrations.md` §6 增一条"大结果在 MCP 形态下**不卸载**，原样返回"，或在下发 schema 时改写该描述（但那会破坏"schema 与模型侧同源"）。

### P2-2｜"参数不合 schema"不是可读错误，而是静默成功（与 C 的 R-C1 列举不符）

* **现象**：设计文档 C §R-C1 要求"未注册工具、**参数不合 schema**、被策略拒绝、工具异常，各自给出可读错误"。实测：`query_metrics(service=12345)`（schema 声明 string）、`query_metrics({})`（缺 required）都**执行成功**且 `isError=false`。
* **最小复现**：见 `commands.sh` 的 `a_functional` 一节（`schema_mismatch`、`missing_required` 两格）。
* **为什么不是 P0**：这是**有意收窄**，`docs/integrations.md` §6.2 明确写了"服务端不做第二套参数校验…schema 是下发给客户端的自述，不是第二道闸"，理由（避免与管线分叉）成立。属于"设计文档的列举与实现不符、且已在别处说明"。
* **建议**：在设计文档 C 的 R-C1 上补一句"schema 不合**不**构成失败语义（由 ArgPolicy/工具自身裁决）"，把两处口径对齐。

### P2-3｜`approve` 的顶层 `isError` 不反映 `executed[]` 内的失败

* **现象**：批准一个越出参数安全域的写（`size=9999`）时，顶层回执是 `status=approved`、`isError=false`，而内层 `executed[0].status=rejected`、`error_class=arg_policy_violation`。只看 `isError` 的客户端会以为写成功了。
* **最小复现**：`commands.sh` 的 `b_extra` 一节（`arg_policy` 格：`after_approve_is_error=false`、`after_approve_sc.executed[0].status=rejected`）。
* **影响**：信息**可读**（内层有完整结论，账本 0 行也证明没执行），所以不是 P0/P1；但"失败语义可读"的强度依赖客户端读内层字段。
* **建议**：顶层 `isError` 在 `executed[]` 含 `failed/unknown/rejected` 时置真，或在 `next` 提示里写明"结果在 executed[] 里"。

### P2-4｜"pytest 405 passed"实为 404 passed + 1 skipped

* **现象**：交付汇报的"每阶段共同门"写 `pytest -o addopts= -p no:cacheprovider -q → 405 passed`；实测 `404 passed, 1 skipped`（该 skip 在 main 上同样存在，不是新增）。
* **最小复现**：`commands.sh` 的 `g28` 一节。
* **影响**：数字口径，不影响结论。

---

## 4. 无法判定项

1. **mutmut `segfault` 判决的根因**。我能证明判决错误（反例：手工应用后整套用例通过），但没有定位到 mutmut 3.8 runner 内部为什么返回 -11。已知线索：mutmut 在 `mutants/` 目录内跑 pytest，且 `mutmut/utils/safe_setproctitle.py` 自带一条"fork 后调用 setproctitle 会让子进程 segfault"的注释；但该模块未被本项目使用，因此**线索不等于结论**。
2. **Jaeger（路径②）与任意 OTLP/HTTP 后端（路径③）**。本机无 `docker`、无凭据，**无法实测**。文档已如实标注"本机未实测"，我没有把它们当成已验证——这与文档一致，但**"能连上真实后端"这件事本身在本报告里仍是未验证**。
3. **作者"三次 run 评估 1058/1114/1110"的具体复现**。我只做了"main 全量列出/计入对账"与"tip 单模块多次运行"两件事，没有重跑 tip 的三次全量 run（单次全量成本过高）。因此"~1110 可复现"我**未独立确认**；我确认的是它的构成（1110 + 912 = 2022）。
4. **`scope=session` 批准复用的安全性**：实测"同工具 + 同参数可复用、不同参数被拒"符合文档，但"这是否是期望的安全边界"属于设计判断，不是我可以证伪的事实。

---

## 5. 未覆盖风险

* **平台与版本**：只在 macOS + Python 3.14.6 + `mcp` 2.2.0 上验证。`mcp` 1.x／3.x 未验（作者也只在汇报里点名 2.2.0）；Windows 未验（崩溃演示依赖 SIGKILL 与 `/bin/sh`）。
* **并发**：同一 run 被两个 MCP 进程同时驱动**未验**——`docs/integrations.md` §6.6 明确说"租约没有被接进写路径，后果由外部账本裁决"，这是登记过的边界而非缺陷，但"后果具体长什么样"我没有测。
* **真实模型路径**：本包与模型无关，我没有跑 `live` 用例（需要 API key，且本包声明不需要）。
* **门禁的其余盲区**：我没有穷举"哪些消失方式会让门禁静默绿"。P0-1 给出的是**判据结构**上的结论（缺项不可能变红），不是穷举。
* **性能**：只测了 verify 时长；`--list-tools`、崩溃演示、selftest 的耗时未做统计，也不应被读成性能结论（仓库自身纪律如此）。

---

## 6. 证据索引

原始产物全部留在 `/tmp`（不随 PR 提交）：

| 内容 | 路径 |
|---|---|
| 功能面（工具清单／只读对拍／大结果／失败语义） | `/tmp/ev/a_functional.json`、`/tmp/ev/a4_exc.json` |
| 治理语义（approve/reject/edit/错 id） | `/tmp/ev/b_governance.json` |
| 治理补充（重复 approve／排队／ArgPolicy／session 复用） | `/tmp/ev/b_extra.json` |
| 无第二权威（SIGKILL → 重启 → 续跑） | `/tmp/ev/b_restart.json` |
| TOCTOU | 本报告 §2 B8（`/tmp/drv/b_toctou.py`） |
| 崩溃两格（作者脚本 ×2） | `/tmp/ev/crash1.json`、`/tmp/ev/crash2.json` |
| 崩溃两格（我的自写驱动） | `/tmp/ev/c_crash.json` |
| 可观测 selftest | `/tmp/ev/d_sink.txt` |
| 边界（AST／反向 import／时长／措辞） | `/tmp/drv/e_ast.py`、`/tmp/drv/e_reverse.py`、`/tmp/ev/count_{main,tip}.txt` |
| 变异门禁：控制／注入／正对照 | `/tmp/ev/mut/gate_control2.txt`、`gate_injected.txt`、`gate_poscontrol.txt` |
| 变异门禁：`--module` 失败 | `/tmp/ev/mut/module_gate.txt` |
| 变异基线两份 + `.meta` 状态分布 | `/tmp/verify-main/reports/mutation_baseline.json`、`/tmp/verify-mcp/reports/mutation_baseline.json`、`/tmp/ev/mut/main_estimates.txt` |
| 一致性对拍 | `/tmp/ev/f_parity.json` |
| 回归门禁（ruff／opsenv／crash_matrix／check_facts） | `/tmp/ev/g28.txt`、`/tmp/ev/opsenv_full.txt`、`/tmp/ev/crash_matrix_full.txt` |
| 全部自写驱动脚本 | `/tmp/drv/*.py` |

---

## 7. 与作者声称的差异

| 作者声称 | 实测 | 判定 |
|---|---|---|
| MCP 客户端能列出并调用工具、写操作停在审批门、批准后执行 | 一致 | ✓ |
| 改参沿用 `__edit1` 语义（superseded + 新幂等键） | 一致（另验出账本落的是改后参数） | ✓ |
| elicitation 从不被调用 | 更强：声明与不声明两条路径**产出逐字段相同** | ✓ |
| 崩溃两格：1 次 vs 2 次，真 SIGKILL，`reconstructed_from_probe=true` | 一致（三次独立运行） | ✓ |
| 判分只认外部账本（连 `-wal` 快照） | 一致（我用自写快照复核） | ✓ |
| 三条口径 + 反向断言（trace 里没有账本） | 一致（我按属性键逐个核） | ✓ |
| 路径②③"本机未实测" | 一致 | ✓ |
| 依赖方向、内核依赖、措辞 0 命中、`semantics.md` 零改动 | 一致 | ✓ |
| verify 时长 +1.8 s | 一致（7.45 → 9.23 = +1.78 s） | ✓ |
| 用例 345 → 405，来源都是集成 | 一致（+60 全在 6 个新文件） | ✓ |
| `pytest … 405 passed` | 实为 **404 passed + 1 skipped** | P2-4 |
| "缺项会导致**误报**而不是漏报" | **相反**：缺项被静默忽略（实测 180 条 → 退出 0） | **P0-1** |
| "基线仍然有效" | 判据结构上不成立：消失的盲区永远看不见；新增未覆盖代码不报红 | **P0-1 / P0-2** |
| "2023 vs ~1110 不是本次改动引入的" | main 上列出 1670 = 计入 1670（无差距）；差距出现在 tip | **P1-2** |
| "三次 `mutmut run` 都只评估 ~1110 个" | 措辞不准：评估了 ~2022，其中 912 条无 `killed/survived/no_tests` 判决 | P1-2 |
| `--module` 选项（帮助里写"只跑一个模块"） | 传文件路径恒失败（mutmut 按变异体名过滤） | **P1-1** |
| 两处偏离（治理工具不带 `run_dir`、不下发 `read_artifact`）已登记 | 一致（§6.4/§6.5 有说明） | ✓ |
| `Loop.resume` 相关：作者原文是"公开方法签名与事件落盘顺序逐字不变" | 签名逐字相同 ✓、事件顺序由对拍与既有用例守住 ✓；**函数体控制流确实改了**（改为委托） | ✓（按作者原文） |

---

## 8. 结论摘要

**能不能合？** 语义与功能面（A–E、G）我**没有发现一条被证伪**，可以合。**变异门禁（F23②、F24）必须先修**，否则 AGENTS.md 红线 5 与"门禁守得住盲区"这两句话在事实层面不成立——建议在合并前二选一：

1. **修门禁判据**（推荐）：把"基线幸存变异里本轮没有结论（`not checked`/`segfault`/`timeout`/`no tests`/未生成）"判为失败；把"新增 `no tests`"判为失败；顺带修 `--module` 的参数语义。
2. **收窄承诺**：在 `reports/mutation_baseline.json` 的 `note`、`docs/testing.md` 与交付汇报里写明"本门禁只防**已覆盖代码**的盲区倒退；对新增的、完全没被测的代码不敏感；对 `segfault`/`timeout`/未评估的变异体不可见"，并撤掉"新增代码把盲区扩大 → 红"这类表述。

**必须先修的清单（按优先级）**

1. **P0-2**：门禁对"新增未覆盖代码"不报红 → 与 AGENTS.md 红线 5 直接冲突。
2. **P0-1 / P1-3**：门禁对"消失的基线幸存变异"静默绿；且 mutmut 的 `segfault` 判决会把**真幸存变异**（已用反例证明）扫进不可见区。
3. **P1-2 / P1-1**：汇报里"不一致先于改动存在"不成立；`--module` 恒失败。
4. **P2-1 ~ P2-4**：口径与文案（大结果不卸载、schema 不合不报错、`approve` 顶层 `isError`、405 vs 404+1skip）。

**我无法判定的**：mutmut `segfault` 的根因；路径②③能否真的连上真实后端（无 Docker/凭据）；作者"三次 run 1058/1114/1110"的可复现性（未跑全量）。

**一条正面结论值得单独说**：F26 的独立对拍（真 MCP 服务进程 vs 进程内 `Loop`）给出**逐位相同**的管线事件与逐行相同的外部账本——"两处调用点行为一致"这条主张经得起独立复验，这正是本次交付里最值钱的部分。
