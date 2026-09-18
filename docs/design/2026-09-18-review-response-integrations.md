# 评审响应：独立验证报告（2026-09-18，外围集成）的 P0/P1/P2 处置

* 被响应的报告：[`docs/independent-test-2026-09-18/report.md`](../independent-test-2026-09-18/report.md)
  （2 个 P0 + 3 条 P1 + 4 条 P2）
* 本次响应的原则：
  1. **先自纠自查**——逐条把指控复现一遍，确认它是不是事实，再谈处置；
  2. 凡涉及门禁的改动都做**退化注入验证**（改坏 → 变红 → 还原），不写"现在绿了"；
  3. 口径与行为更正与代码同批落地，不留"文档说 A、代码做 B"。
* 口径：本文件里的数字均可由命令再生；用例数、变异体数等演进类数字引用 claim id，不手写。
* 一句话结论：**报告的两条 P0 都成立**，而且 P0-1 的活体来源（mutmut 的 `segfault` 误判）
  根因已定位并修掉；P1 三条全部成立并已处置；P2 四条全部成立并已处置。
  报告的语义/功能面结论（A–E、G 全部未证伪）**没有被本次改动推翻**——
  唯一动到已通过验证的语义的是 P2-3（`approve` 顶层 `isError`），理由见 §4.3 与 §6.3。

---

## 1. 自纠自查：先确认报告说的是不是事实

| # | 报告的指控 | 我的复现方式 | 结果 |
|---|---|---|---|
| P0-1 | 门禁把"缺项"当好消息（判据结构上不可能变红） | 读判据：`new_survivors = [n for n in 本轮survived if n not in 基线survivors]`；缺项只会让这个列表**变短**。实跑见 §2.1 | **成立**（报告的判断方向是对的，交付汇报原文的判断是反的） |
| P0-2 | 门禁抓不到"新增的、完全没被测的代码" | 在 `harness/approval.py` 加一个无任何用例调用的函数 + 一条永不执行的分支，跑门禁 | **成立**：修复前 `no_tests 0 → 12`、**退出 0** |
| P1-1 | `--module` 传文件路径恒失败 | `.venv/bin/python -m mutmut run "harness/approval.py" --max-children 1` | **成立**：`AssertionError: Filtered for specific mutants, but nothing matches / Filter: ('harness/approval.py',)` |
| P1-2 | "2023 vs ~1110 不是本次改动引入"不成立 | `main`：`print-time-estimates` 列出 1670，基线计入 1670（**相等**）；tip：列出 ≈2022、计入 1110。差的构成从 `.meta` 读出：906 条 `-11`（`segfault`）+ 6 条 `-24`（`timeout`）= 912 | **成立**：差距出现在 tip，且**这条差距的构成是"没有判决"而不是"没被评估"** |
| P1-3 | mutmut 把真幸存变异误判成 `segfault` | 见 [`2026-09-18-mutation-segfault-investigation.md`](2026-09-18-mutation-segfault-investigation.md) §1–§2；`approval.py` 逐字未改而 3 条幸存变异集体变 `-11`，且报告已用手工 apply 证明其中一条是全绿的真幸存变异 | **成立，且根因已定位**（macOS 上父进程热过系统代理解析、fork 出的子进程再碰一次 → SIGSEGV） |
| P2-1 | schema 文案承诺"卸载为 artifact"，MCP 形态下不卸载也没文档说明 | `fetch_logs(lines=200)` → 23 243 字符原样内联，无 `digest`；`docs/integrations.md` §6 原文只写了"不下发 `read_artifact`" | **成立** |
| P2-2 | "参数不合 schema"是静默成功，与设计文档 C 的 R-C1 列举不符 | `handle_tools_call(env, "query_metrics", {"service": 12345})` 与 `{}` | **成立**：两者都 `status=executed`、`isError=False` |
| P2-3 | `approve` 顶层 `isError` 不反映 `executed[]` 内的失败 | 批准 `size=9999`（越出 ArgPolicy 安全域） | **成立**：顶层 `status=approved`、旧判据 `is_error_status(...)=False`、内层 `rejected/arg_policy_violation`、账本 0 行 |
| P2-4 | "405 passed"实为 404 passed + 1 skipped | 在 tip 的 worktree 里用**装了 `[otel]`** 的解释器跑同一条命令；再对照"不装 otel 则 `test_otel.py` 有一例 `importorskip`" | **部分成立**：数字随 extra 变，不是写错——见 §4 表与下方说明 |

**报告里我核过、但不必处置的条目**（也记在这里，免得下次又被当成待办）：

* **§2 补充观察（`scope=session` 的批准对"同工具 + 同参数 hash"可复用）**：与
  `docs/semantics.md` 的幂等键定义一致，报告自己也判定"不是缺陷"。**不处置**。
* **§2 F27 关于 `Loop.resume` 的措辞**：报告明确说"任务书里的转述与作者原文不同，
  按作者原文核，属实"。**作者的原文表述准确，不处置**。
* **§6 证据索引里的 `/tmp` 产物**：按仓库纪律不进版本库；可再生的部分本轮由
  `docs/design/2026-09-18-mutation-segfault-investigation.md` 的命令覆盖。

---

## 2. P0（不修不能合）

### 2.1 P0-1｜"缺项"被当成好消息

**处置**：`scripts/mutation_check.py` 改成**全状态记账 + 六条红灯条件**，
其中三条直接堵住这条路径：

| 条件 | 判据 | 堵住的"消失方式" |
|---|---|---|
| 2 | 基线里是判定类（`killed`/`survived`）的变异体，本轮变成**非判定类** | 基线幸存者变成 `segfault`/`timeout`/`no tests`/`suspicious`/`not checked`… |
| 3 | 基线里的变异体本轮**完全缺失** | 未评估、缓存缺项、整条改名 |
| 6 | **无结论集合较基线增长** | 有新名字进入不可见空间（即使数量持平） |

外加：出现 mutmut 未归类的状态名 ⇒ 判失败（fail-closed），不允许新一轮"静默丢数据"。

**修复前会怎样**：任何一条基线幸存变异从结果表里消失 → 落进 `fixed` → 打印
"已被杀死（好事，不判失败）" → **退出 0**。活体场景就是 `checkpoints.py` 那 76 条。

**验证（实跑，非单元测试）**：

```bash
# V1：构造"180 条基线幸存变异全部缺失"（报告 §3 的原始场景）
.venv/bin/python scripts/mutation_check.py --module "harness.approval.*" \
    --baseline /tmp/…/baseline_plus180.json --timeout 600
# → 退出 1；[vanished-from-results] 180 条，逐条打印名字

# V3：把修复前的真实 segfault 判决 replay 回结果表（819 条落进当前变异体集合的）
.venv/bin/python scripts/mutation_check.py --timeout 900
# → 退出 1；[decided-to-inconclusive] 819 条 + [inconclusive-grew] 819 条
#   名单里正是报告点名的 is_expired__mutmut_1 / __mutmut_4 / validates__mutmut_5
```

两格各自的对照（未注入、同一命令）都是**退出 0**。完整记录见调查文档 §4.1。

回归用例：`tests/test_mutation_gate.py` 的
`test_vanished_baseline_mutant_turns_the_gate_red`、
`test_baseline_killed_becoming_inconclusive_turns_the_gate_red`、
`test_baseline_survivor_becoming_segfault_turns_the_gate_red`、
`test_inconclusive_set_growth_turns_the_gate_red`、`test_unknown_status_turns_the_gate_red`，
外加正对照 `test_gate_is_green_when_nothing_changed`。

### 2.2 P0-2｜抓不到"新增的、完全没被测的代码"

**处置**：新增条件 4——本轮 `no tests` 里出现基线没有的名字 ⇒ 退出 1。
"未覆盖类"与"无结论类"一起计入**不可见空间**，每次运行都打印其规模，
`survivor_rate` 被明确标注**不是覆盖率**。

**修复前会怎样**：新增变异体被判为 `no tests`，而判据只看 `survived` ⇒ 覆盖率为零的新代码
不报红，与 AGENTS.md 红线 5 冲突。

**验证（实跑）**：在 `harness/approval.py` 末尾追加一个无任何用例调用的函数
（含一条永不执行的分支），跑 `scripts/mutation_check.py --module "harness.approval.*"`：

```
[mutation] 全状态记账：killed=1331 survived=700 no tests=28
[mutation] 不可见空间（无结论类 + 未覆盖类）= 28/2059 = 1.4% …
[mutation] 判定失败：1 条红灯条件成立
--- [new-no-tests] 新增 `no tests` 变异体（= 新增的、完全没有用例覆盖的代码）：10 条
      harness.approval.x__gate_probe_uncovered__mutmut_1 … __mutmut_10
```

退出码 **1**。跑完已 `git checkout` 还原（`shasum -a 256 harness/approval.py` 与注入前一致）。
回归用例：`test_new_no_tests_turns_the_gate_red`。

---

## 3. P1（合并前应修）

### 3.1 P1-1｜`--module` 传文件路径恒失败

**处置**：新增 `normalize_module_filter()`：以 `.py` 结尾或含路径分隔符的参数按
`harness/approval.py → harness.approval.*` 翻译（mutmut 的位置参数是**变异体名** fnmatch），
其余原样透传；帮助文本写明两种写法，并顺带写清"它只影响哪些变异体被重新评估，
判定读的是全量结果表，`--baseline` 必须仍是全量基线"（这条是本次验证中发现的新约束：
我一开始拿切片基线做实验，结果所有未被切中的变异体都被报成"新增"）。

**验证**：`test_module_path_is_translated_to_a_mutant_name_glob`；
实跑 `--module harness/approval.py` 正常（见 §6.2 的门禁运行记录）。

### 3.2 P1-2｜"2023 vs ~1110 不是本次改动引入"不成立

**处置**：交付汇报的"遗留与建议"第 1 条**逐句更正**（原文的判断方向是反的），
并把差的构成写进去；`docs/testing.md` §2 与 `docs/development.md` 的门禁段落同步。

**更正后的口径**：mutmut **确实评估了** ~2022 条；其中 1110 条得到 `killed/survived/no_tests`
判决，另 **912 条（906 `segfault` + 6 `timeout`）没有任何判决**。
`main` 上不存在这个差距（列出 1670 = 计入 1670）。

再往前一步（本次实测）：修复后 `mutmut print-time-estimates` 列出 **2049**，结果表也是 **2049**，
**逐条相等**。⇒ "列出数 ≠ 评估数"这个提法本身就不成立——mutmut 列出多少就评估多少，
"1110 vs 2022"是"**有判决 vs 无判决**"的差，不是"列出 vs 评估"的差。
（差异只在 `harness/execution.py`：1157 → 1184 条；其余三个模块不变。
哪 27 条没有逐条核对——`mutants/*.meta` 是覆盖写的单文件，本轮第一次 `run` 就把它重写了。
这也是为什么入库基线必须逐条记状态，而不是只留一个总数。）

### 3.3 P1-3｜`segfault` 误判（P0-1 的活体来源）

**处置**：根因定位 + 修复 + 全量验证，独立成文：
[`2026-09-18-mutation-segfault-investigation.md`](2026-09-18-mutation-segfault-investigation.md)。

* 根因：`urllib.request.getproxies()` 在 macOS 上回落到 `_scproxy` →
  `SystemConfiguration` → `libsystem_trace`；**父进程用过一次之后再 fork 出的子进程里调它
  会 SIGSEGV**。mutmut 默认 `fork` 隔离，且主进程先跑完 clean tests（`ForkRunner.run_clean_tests`
  不 fork），于是"热"这个前提被满足。判成 `segfault` 的边界由**测试选择**决定，不由变异体语义决定。
* 修法：`tests/conftest.py` 在 import 期设 `no_proxy`/`NO_PROXY` = `*`，
  让 `getproxies()` 在环境变量层短路，永不碰 SystemConfiguration。
* 验证：全量重跑，`segfault` **906 → 0**、`timeout` **6 → 0**，
  幸存变异 180 → 700（其余回到 `killed`），不可见空间 45.1% → 0.9%。
* 报告点名的反例 `is_expired__mutmut_9` 现在由 mutmut 自己判成 `survived`，与手工验证一致。

**关于"幸存率从 0.1648 涨到 0.3447"**：这不是测试变差，而是原先有 45% 的变异体**根本没被判定**。
把不可见的变异体算进来之后的数才是可比的。所有引用处（README / HANDOFF 引用的是 claim id /
`docs/testing.md` / `docs/development.md` / 交付汇报）已同步。

---

## 4. P2（口径与文案）

| # | 指控 | 处置 | 位置 |
|---|---|---|---|
| P2-1 | 大结果在 MCP 形态下原样内联，但 schema 文案仍写"卸载为 artifact"，且无文档说明 | 新增一条口径：卸载发生在 `harness/context.py` 的**视图渲染层**，MCP 服务不走渲染层 ⇒ 原样内联；schema 文案与模型侧同源，**读法以 `docs/integrations.md` 为准**。没有去改写 schema（那会破坏"同源"） | `docs/integrations.md` §6.3 |
| P2-2 | 设计文档 C 的 R-C1 列举里有"参数不合 schema"，实现却静默成功 | R-C1 上补一句：**schema 不合不构成失败语义**，执行前唯一闸门是 ArgPolicy 与工具自身；并更正原文那句列举 | `docs/design/2026-09-18-integrations.md` §R-C1；实现口径 `docs/integrations.md` §6.2 |
| P2-3 | `approve` 顶层 `isError` 不反映 `executed[]` 内的失败 | **改行为**（见 §4.3 下方） | `integrations/mcp_server.py`、`docs/integrations.md` §4、`docs/design/2026-09-18-integrations.md` §R-C1 |
| P2-4 | "405 passed"实为 404 passed + 1 skipped | **部分成立**，见上表——数字随 extra 变，更正的是**口径**而不是数字本身 | `docs/design/2026-09-18-delivery-report-integrations.md` |

> P2-4 的处置与报告建议不同是有意的：报告给的是它自己环境里的读数（`/tmp/verify-mcp` 装的是
> `[dev,eval,mcp]`），而"405"在装了 `[otel]` 的环境里是准确的——同一个 commit `1d4b3f3`
> 的 worktree 用本仓库 `.venv`（含 otel）实跑就是 `405 passed, 0 skipped`。
> 把其中一个数换成另一个，只是把一种口径错误换成另一种；真正缺的是**把环境写进口径**。
> 处置：两种读数与各自环境并列写出，并声明用例数一律引用 claim `tests-collected`
> （`--collect-only` 计数，与环境 extra 无关）。

### 4.3 P2-3 的行为改动：为什么这不是回归

**改了什么**：新增 `call_is_error(payload)`——顶层结论是错误，**或** `executed[]` 里含
`failed`/`unknown`/`rejected` 时，协议层顶层 `isError` 置真；回执里加一句可读 `note`
（"执行结果在 `executed[]` 内"）。

**没改什么**（这是"不是回归"的关键）：

* 顶层 `status` 的取值集合与语义**逐字不变**：同意批准仍是 `"approved"`，
  拒绝仍是 `"rejected"`；
* `executed[]` 的结构与结论不变；
* 事件日志、外部账本、幂等键、审批绑定、ArgPolicy 闸门全部不变——
  该用例同时断言**账本 0 行**（一次真实检定的锚点）；
* `pending_approval` 仍然**不是**错误（它是治理层的正常中间态）。

**反例方向的证据（"修复前会怎样"）**：

```
顶层 status        : approved
内层 executed[0]   : rejected arg_policy_violation
旧判据（只看顶层） : False        ← 只看 isError 的客户端会以为写成功了
新判据 call_is_error: True
账本行数           : 0
```

**为什么必须改**：`isError` 是 MCP 协议层唯一的"这次调用成没成"信号。
报告说的是"信息可读，但强度依赖客户端读内层字段"——也就是说这条承诺在
**只看 `isError` 的客户端**上不成立。要么改行为，要么把"失败语义可读"的适用范围收窄到
"读 `executed[]` 的客户端"。所有者已决定改行为。

**正对照（防止 `isError` 变成恒真）**：执行成功时 `isError` 仍为 `False`、
且**不加** `note`（`test_successful_approval_has_no_failure_note`、
`test_successful_approval_stays_is_error_false`）。一个恒真的字段没有信息量。

**必跑的复验**（这条动了已通过独立验证的语义）：见 §6.3——
`pytest -m mcp` 与 `tests/test_execution_parity.py` 全部重跑通过。

---

## 5. 报告"无法判定项"的本轮进展

| 报告 §4 的未定项 | 本轮进展 |
|---|---|
| 1. mutmut `segfault` 判决的根因 | **已定位并修复**（§3.3 / 调查文档） |
| 2. Jaeger（路径②）与任意 OTLP/HTTP 后端（路径③）能否真连上 | **仍未实测**（本机无 Docker、无凭据）。文档继续标注"本机未实测"，本轮未改口径 |
| 3. 作者"三次 run 评估 1058/1114/1110"的可复现性 | **给出构成**：1110 是"有判决"的条数，另 912 条无判决（906 `segfault` + 6 `timeout`）。修复后同一口径下是 2031 有判决 / 18 无判决（全是 `no tests`） |
| 4. `scope=session` 批准复用是否是期望的安全边界 | **属于设计判断，不处置**；已确认与 `docs/semantics.md` 的幂等键定义一致 |

---

## 6. 验证记录（逐条实跑）

命令与结果（本机：macOS / Python 3.14.6 / mcp 2.2.0 / mutmut 3.8.0）：

1. `.venv/bin/pytest -o addopts= -p no:cacheprovider -q` → **428 passed**（本机 `.venv` 装了 `[otel]`）
2. `.venv/bin/pytest -m mcp -q` → **15 passed**；`-o addopts=` 口径下 `tests/test_mcp_server.py` 10 例
3. `.venv/bin/ruff check .` → **All checks passed**
4. `python -m opsenv.suite --per-fault 8 --repeats 3 --gate` → **14/14，退出 0**
5. `python -m experiments.crash_matrix --repeats 5` → **16 格 as-predicted / 80 次运行 / 70 次真 SIGKILL / 0 违例，退出 0**
6. `scripts/check_facts.py --run verify` → **44/44**
7. `scripts/mutation_check.py` → **退出 0**；`killed=1331 survived=700 no_tests=18`，
   不可见空间 **18/2049 = 0.9%**；`mutmut print-time-estimates` 列出 2049（与结果表相等）
8. 三条退化注入（V1/V2/V3）各自**退出 1**，对照组**退出 0**（§2.1/§2.2、调查文档 §4.1）
9. `pytest -m mcp` 与 `tests/test_execution_parity.py`（7 例）在 P2-3 改动后重跑通过（§4.3）
10. **Python 3.11**（`/tmp/venv311`，按 CI 版本建，装 `[dev,eval,mcp]`）：
    `pytest -o addopts=` → **427 passed, 1 skipped**（1 skip = `test_otel.py` 的
    `importorskip("opentelemetry.sdk")`，与 P2-4 的成因同一处）；
    `pytest -m mcp` → 15 passed；`ruff` → All checks passed；`check_facts --run verify` → 44/44
11. 最小复现在 **Python 3.11.15 与 3.14.6 上逐格相同**（`-11` 都只在"父进程热过"那一格出现），
    见调查文档 §5

**边界（本结论不适用的范围）**：

* 变异的 `segfault` 问题已在本机用 **Python 3.14.6 与 3.11.15** 两版各测一次（逐格相同），
  机理是 macOS 框架 + fork 语义；**CI（Linux）上是否存在同族问题未测**——
  `getproxies_macosx_sysconf` 只在 macOS 存在，所以预期没有，但这是推断。
* 变异门禁的判据只对**已登记基线的那四个模块**成立；`--module` 缩小范围时，
  判定的仍是全量结果表（见 §3.1 的新约束）。
* 本轮没有穷举"哪些消失方式会让门禁静默绿"——给的是判据结构上的闭合
  （判定类 / 未覆盖类 / 无结论类全覆盖 + 未知状态 fail-closed）。
* 可观测导出仍然只有路径①本机实测；路径②③的"能连上"未被本次改动触及。
* `2022 → 2049` 的 27 条差异没有逐条核对（`mutants/*.meta` 是覆盖写单文件，原始那份已被重写）。
