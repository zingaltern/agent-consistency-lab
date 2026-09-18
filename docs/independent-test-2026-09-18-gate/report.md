# 独立验证报告（第二轮）：变异门禁的判据修复

**被测 commit：`ed8b3662fd5b0d1d96257c517b3d82480c3613fc`**（PR #16 tip；
base 链 #13/#14 → #15 → #16。`main` = `d53cf87` 是该 tip 的祖先 ⇒ 可快进合并）

* 日期：2026-09-18 / 19
* 验证者：独立测试 agent（只读仓库；全部产物在 `/tmp`；自写全部注入与驱动，未复用作者脚本）
* 验证对象：上一轮报告 `docs/independent-test-2026-09-18/report.md` 的两条 P0 是否真被修掉，
  以及新判据本身的完整性与可绕过性
* 环境：macOS 26.6.2（Build 25G83，darwin 25.6.0）arm64 / 10 核；mutmut 3.8.0
  * `/tmp/verify-gate`（Python 3.14.6，`.[dev,eval,mcp,otel]`）
  * `/tmp/venv311`（Python 3.11.15 = CI 版本，`.[dev,eval,mcp]`，**不装 otel**）
  * `/tmp/vg-inj-mut`、`/tmp/vg-inj-unmut`、`/tmp/vg-revert-conftest`、`/tmp/vg-pytest`、`/tmp/vg-m3`（各为独立克隆/工作树）

---

## 0. 一句话结论

**两条 P0 都真被修掉了，A 组三格（缺项 / 新增未覆盖代码 / 活体 segfault 回放）在我的独立注入下全部变红，
对照格全部保持绿**；未发现新的 P0。发现 **1 条 P1**（`--json-out` 在超时/中止路径不落盘，
与帮助文本承诺不符）与 8 条 P2。**这三个 PR 可以合**；P1/P2 建议随下一轮门禁改动一并处理，不作为合并阻塞。

---

## 1. 方法与证据链

### 1.1 我做了什么不同的事

* **黑盒优先**：先读判据实现（`scripts/mutation_check.py`）、基线 schema（`reports/mutation_baseline.json`）、
  `docs/testing.md` §3 的退化注入表与 `pyproject.toml` 的 mutmut 配置，**先冻结我自己的注入方案**，
  之后才读作者的设计/调查/交付文档与 PR 描述。
* **两条独立驱动**：
  1. **真 mutmut**（端到端）：在独立克隆里跑真实全量 run，看退出码与标签；
  2. **自写 mutmut 替身**（`/tmp/vg-shim/mutmut.py`，我自己写的，只实现 `run`/`results`/`show`）：
     把**构造出来的结果表**喂给**真的** `scripts/mutation_check.py`，从而把判据矩阵在几秒内穷举
     （14 个场景）。替身的输出格式按真 mutmut 3.8 的输出逐字对齐（`"    <name>: <status>"`），
     并用真 run 的 A1 场景做了交叉校验（同样两个标签、同样 2048 条）。
* **不复用作者的驱动**：`/tmp/vg-repro/` 下的四个脚本（`segv_repro.py`、`gate_matrix.py`、
  `proxy_impact.py`、`approve_iserror.py`）全部由我重写；注入夹具（`_reverify_probe_uncovered`、
  反转 conftest 两行）也是我自己写的最小改动。

### 1.2 证据链（每条都有最小复现命令，见 `commands.sh`）

| 方向 | 关键证据 |
|---|---|
| A1 缺项 | 冷缓存 + 只跑 1 条变异体 ⇒ 退出 1（`decided-to-inconclusive` 2030 + `inconclusive-grew` 2048）|
| A1 对照 | 结果表与基线逐条相同 ⇒ 退出 0 |
| A2 | 未注入全量 run 退出 0 / 在被变异模块注入无覆盖函数后退出 1（唯一红灯 `new-no-tests` 3 条）/ 同样代码注入未变异模块退出 0 |
| A3 | 把 3 条点名变异 replay 成 `segfault` ⇒ 退出 1，名单逐条对上 |
| B4 | 我的最小复现：父进程预热 → fork 子进程 SIGSEGV(11)；不预热 → 0。3.14.6 与 3.11.15 逐格相同；崩溃报告栈 = `get_proxies → SCDynamicStoreCopyProxiesWithOptions → _CFXPreferences → os_log_type_enabled → _os_log_preferences_refresh` |
| B5 | 修复版全量 run 的 `.meta`：`-11`=**0**、`-24`=**0**、2049/2049 全评估；把 conftest 两行改回去 ⇒ `approval.py` **16/29** 条 `-11`（含那 3 条点名幸存变异）|
| C8 | 未知状态名 `melted` ⇒ 退出 1 + `unknown-status`，且该条被计入不可见空间（19/2049），未静默丢弃 |
| C9 | `only_mutate` 删模块 ⇒ 退出 1 + `vanished-from-results` 29 条；整模块 `skipped`/全表 `not checked` ⇒ 退出 1；改名 ⇒ `new-survivors`+`vanished` 双标签 |
| D12 | 真 stdio 服务进程：`size=9999` ⇒ 顶层 `status=approved` 且 `isError=true`、note 指向 `executed[]`、账本 0 行；正对照 `size=32` ⇒ `isError=false`、账本 1 行 |
| D13/D14 | 基线 11 项自洽检查全过；`survivor_rate`/`invisible_share` 可由 counts 复算；`print-time-estimates` 行数 = 结果表条数 = 2049 |

---

## 2. 各方向结论表

（下表 "✓" = 与任务书要求的期望一致；"✗" = 不一致）

### A. 两条 P0 是否真被修掉

| # | 注入 | 期望 | 实测 | 结论 |
|---|---|---|---|---|
| A1a | 真 mutmut、冷缓存、`--module '<一条变异体名>'`（2 048 条基线变异从结果里消失） | 退出 1 + 列出缺失条目 | 退出 **1**；`全状态记账：survived=1 not checked=2048`；红灯 `decided-to-inconclusive` 2030 条 + `inconclusive-grew` 2048 条；不可见空间 2048/2049 = 100.0% | ✓（标签是 `decided-to-inconclusive`/`inconclusive-grew` 而非 `vanished-from-results`——原因见 §7 的差异说明：mutmut 会把这些条目**列出来**并标成 `not checked`，不是从表里消失） |
| A1b | 真缺项（用替身把 3 条基线幸存变异**从结果表里删掉**） | 退出 1 + `vanished-from-results` | 退出 **1**；`[vanished-from-results] 3 条`，逐条列名 | ✓ |
| A1c | 对照：结果表与基线逐条相同 | 退出 0 | 退出 **0**，`没有新增幸存变异…盲区没有扩大` | ✓ |
| A2a | 对照：未注入的全量 run | 退出 0 | 退出 **0**；`killed=1332 survived=699 no_tests=18`；不可见空间 18/2049 = 0.9% | ✓ |
| A2b | 在被变异模块 `harness/approval.py` 追加一个**无任何用例调用**的函数（含一条永不执行的分支），跑全量 | 退出 1 + `new-no-tests` | 退出 **1**；**唯一**红灯 `[new-no-tests] 3 条`（`harness.approval.x__reverify_probe_uncovered__mutmut_{1,2,3}`）；总数 2049 → 2052 | ✓（这正是上一轮 P0-2 的反例场景） |
| A2c | 把**同样的代码**加在**未变异模块** `harness/ids.py`，跑全量 | 不受影响或另给可读说明 | 退出 **0**；数字与基线**完全一致**（1331/700/18） | ✓（门禁静默通过——这是 `only_mutate` 定义的作用范围，见 P2-6） |
| A3 | 把上一轮报告点名的 3 条真幸存变异 replay 成 `segfault` | 退出 1 + `decided-to-inconclusive`/`inconclusive-grew`，名单含那 3 条 | 退出 **1**；`[decided-to-inconclusive] 3 条` + `[inconclusive-grew] 3 条`，名单逐条为 `is_expired__mutmut_1` / `is_expired__mutmut_4` / `validates__mutmut_5` | ✓ |

### B. 根因与修法

| # | 结论 | 证据 |
|---|---|---|
| B4 | **独立复现成立**：崩溃需要"父进程先热过 `getproxies()`"这一条 | `python segv_repro.py --hot` → `[child] 被信号杀死: signal=11`；不带 `--hot` → `[child] getproxies() 返回 3 项 —— 没崩`。3.14.6 与 3.11.15 两版逐格相同。本机崩溃报告（`Python-2026-09-18-233047.ips`）故障栈：`_os_log_preferences_refresh ← os_log_type_enabled ← CFPrefsSearchListSource ← _CFXPreferences ← SCDynamicStoreCopyProxiesWithOptions ← get_proxies` |
| B5 | **修法真的关掉了它，且可证伪** | 修复版全量 run 的 `mutants/**/*.meta`：`{0: 699, 1: 1332, 33: 18}`，`-11`=0、`-24`=0、`None`=0（2049/2049 全部评估）。把 `tests/conftest.py` 的两行 `no_proxy`/`NO_PROXY` 去掉后同一范围（`harness.approval.*`）重跑：`{1: 13, -11: 16}` —— **16 条 SIGSEGV**，其中恰含 `is_expired__mutmut_1`、`is_expired__mutmut_4`、`validates__mutmut_5`（即基线里的 3 条真幸存变异） |
| B6 | **影响面**:修法把"回环请求走代理"改成"直连"，**不是**"没有任何用例的实际 HTTP 行为改变" | 我的四格实测（`proxy_impact.py`）：无代理变量 → 直连；**设 `http_proxy` 且不设 `no_proxy` → 请求被交给代理（代理命中 1 次、直连 0 次）**；`no_proxy=*` → 直连；`no_proxy=127.0.0.1` → 直连。仓库内 grep 无任何依赖代理配置的用例断言；用例只连回环地址 ⇒ 该改变是**期望语义**，但调查文档 §3 的措辞与 conftest 自己的注释（"会把到 127.0.0.1 的请求交给代理——那是运气"）**自相矛盾**，见 P2-2 |
| B7 | **产品路径无同族风险**：`harness/ opsenv/ experiments/ integrations/ scripts/` 里 `multiprocessing`/`os.fork` **0 命中**，进程启动全部走 `subprocess`（16 处）⇒ fork 后立即 exec，不继承"热的 ObjC 状态" | grep 全量扫描；`getproxies()` 触发点只有 `harness/live_transport.py:103` 与 `harness/otel.py:152`（都被 subprocess 或测试进程包裹） |
| B7b | **修在 `tests/conftest.py` 是否足够**：足够——它是任何加载本测试套件的 runner（裸 `mutmut run`、`pytest`、`pytest-xdist`）的必经入口，且产品进程不 fork | 同上；`conftest.py` 是唯一 conftest，位于 `tests/`，import 期执行 |

### C. 新判据的完整性与可绕过性（14 格矩阵，见 `/tmp/vg-matrix/`）

| 场景 | 期望 | 实测退出码 | 命中的红灯标签 | 结论 |
|---|---|---|---|---|
| S0 结果表与基线逐条相同 | 0 | 0 | —（无） | ✓ |
| S1 真缺项（删掉 3 条基线幸存变异） | 1 | 1 | `vanished-from-results`(3) | ✓ |
| S2 活体回放 3 条点名变异 → `segfault` | 1 | 1 | `decided-to-inconclusive`(3) `inconclusive-grew`(3) | ✓ |
| S3 新增无覆盖代码（模拟注入夹具） | 1 | 1 | `new-no-tests`(4) | ✓ |
| **S4 未归类状态名 `melted`** | 1（fail-closed） | 1 | `unknown-status`(1) + `decided-to-inconclusive`(1) + `inconclusive-grew`(1)；该条被计入不可见空间 19/2049 | ✓（**未静默丢弃**） |
| S5 某模块全部变异体 `skipped` | 1 | 1 | `decided-to-inconclusive`(542) `inconclusive-grew`(542) | ✓ |
| S6 全表 `not checked` | 1 | 1 | `no-decided-mutants`(2049) + 上述两条 | ✓ |
| S7 把一条基线幸存变异改名 | 1（误报可接受、静默忽略不可接受） | 1 | `new-survivors`(1) `vanished-from-results`(1) | ✓ |
| S7b 同族改名且条数不变 | 1 | 1 | `new-survivors`(1) | ✓ |
| S8 结果表为空 | 1 | 1 | （提前返回路径，打印"没跑起来 ≠ 没有盲区"） | ✓ |
| S9 一条判定都没有（全 `no tests`） | 1 | 1 | `no-decided-mutants` + `decided-to-inconclusive`(2031) + `new-no-tests`(2031) | ✓ |
| S10 基线幸存变异被真杀掉 | 0（唯一允许的"好消息"） | 0 | — | ✓（设计如此） |
| S11 基线 `killed` → 本轮 `survived` | 1 | 1 | `new-survivors`(2) | ✓ |
| S12 全新模块出现幸存变异 | 1 | 1 | `new-survivors`(1) | ✓ |

真 mutmut 侧的补充格：

| # | 场景 | 实测 | 结论 |
|---|---|---|---|
| C9-1 | 从 `pyproject.toml [tool.mutmut].only_mutate` **删掉** `harness/approval.py`（保留旧 `.meta`/`.spans`） | `mutmut results --all=true` 从 2049 掉到 **2020**（approval 29 条消失，config 驱动、**不是**缓存驱动）；门禁退出 **1**，`[vanished-from-results] 29 条` | ✓ **不可静默缩小变异目标集** |
| C9-4 | `--module` 缩小范围后，判定读的是**整个缓存（当前配置生成的集合）**还是该模块 | A1 的 `--json-out` 里 `status_by_mutant` 有 **2049** 条（其中 2048 条 `not checked`）；`.meta` 里既有的状态在暖缓存下保留 ⇒ 读的是全量结果表。帮助文本"判定读的是全量结果表，`--baseline` 必须仍是全量基线"与实现一致 | ✓ |
| C10 | `--module harness/approval.py`（路径形态） | 打印 `→ 变异体名 glob harness.approval.*`；`AssertionError / nothing matches` **0 命中**；run 正常完成（57.8s）。注：该克隆的缓存被我前面的实验污染过，所以那一轮的**判决**不可用（`no-decided-mutants`），这里只取"断言消失 + run 跑得通"这一条 | ✓（上一轮 P1-1 的修复成立） |
| C10b | `--json-out` 是否在**失败路径**也写 | 判定失败路径：**写**（A1 的 `/tmp/a1-vanished.json` 存在，`counts.decided=1/invisible=2048`）。**超时/中止路径：不写**（`--timeout 1` → 退出 1、无文件） | 部分 ✓，见 **P1-1** |
| C10c | `mutmut run` 退出非 0 / 读结果失败 | 分别退出 **1** 并给可读原因（`本轮没有跑成功，判定作废` / `读结果失败`） | ✓ |
| C11 | `--summary-only` 的 `survivors` 字段类型 | **列表**（700 条），个数在 `survivor_count`（int 700）；stdout 与 `--json-out` 两路一致 | ✓（同名不同型的旧坑已修） |

### D. 语义与数字

| # | 结论 | 证据 |
|---|---|---|
| D12 | `approve` 顶层 `isError` 反直觉但成立，且**不是恒真** | 格 1（`size=9999`，ArgPolicy 拒）：`pending_approval`（isError=false）→ `approve` → 顶层 `status=approved`、`executed[0].status=rejected`、**顶层 `isError=true`**、`note` 指向 `executed[]`、外部账本 **0 行**。格 2（`size=32` 正对照）：`isError=false`、无 note、账本 **1 行**（单幂等键）。`tests/test_execution_parity.py` = **7 passed** |
| D13 | 基线自洽 | 11 项检查全过（`counts` ↔ `status_by_mutant` ↔ `survivors`/`no_tests`/`status_counts` 逐条一致；`decided=killed+survived`；`invisible=total-decided`）；`survivor_rate = 700/2031 = 0.344658 → 0.3447` ✓；`invisible_share = 18/2049 = 0.008785 → 0.0088` ✓；`mutmut print-time-estimates` 行数 = **2049** = 结果表条数 ✓（附注见 P2-5） |
| D14 | 不可见空间 | `18/2049 = 0.878% ≈ 0.9%` ✓；`no_tests` 计入**不可见**而非判定类（`decided=2031` 不含那 18 条）✓ |

### E. 非回归与文档

| # | 结论 |
|---|---|
| E15a | `pytest`（3.14.6 + `[otel]+[mcp]`）：全量 `-o addopts=` → **428 passed**；默认选择 → **410 passed, 18 deselected**；`-m mcp` → **15 passed** |
| E15b | `pytest`（3.11.15，无 `[otel]`）：全量 → **427 passed, 1 skipped**；默认选择 → **409 passed, 1 skipped, 18 deselected**；`-m mcp` → **15 passed** |
| E15c | `ruff check .` → **All checks passed**（ruff 0.16.8） |
| E15d | `tests/test_execution_parity.py` → **7 passed** |
| E15e | `python -m experiments.crash_matrix --repeats 5` → 退出 **0**；**16 格全部 as-predicted**、`真实 SIGKILL 70/80 次运行`（两个不注入的对照格除外）、`prediction-violated` **0 命中**；墙钟 33.4s |
| E15f | `python -m opsenv.suite --per-fault 8 --repeats 3 --gate` → 退出 **0**；64 场景 × 8 × 3 × 4 系统 = **1536 次运行**，**门禁 14/14 通过**；墙钟 9.3s |
| E16 | 旧数字（0.1648 / 912 / 45.1% / 1110 / 2022）**只**出现在三份 2026-09-18 的设计/交付/调查文档里，且都明确标注为"修复前一列"或"已作废"；README / `docs/testing.md` / `docs/development.md` 已回灌新口径（2049/1331/700、全状态记账、不可见空间、`survivor_rate` 不是覆盖率）；**`docs/semantics.md` 相对 main 零改动**（`git diff --stat main...ed8b366 -- docs/semantics.md` 为空） |
| E17 | P2-4 的**结构**成立：`tests/test_otel.py` 装 `[otel]` → 6 passed；不装 → **5 passed, 1 skipped**（正是那一例 `importorskip`）。**绝对数须按 commit 读**：`1d4b3f3` 的全量收集数我实测为 **405**（与文档一致），tip 为 **428**；见 P2-3 |
| E18 | 预算一致：`--timeout 2400`（40 分钟）< `timeout-minutes: 45` ⇒ 门禁自己的超时先触发、作业上限是兜底；**超时确实判失败**（端到端 `--timeout 1` → 退出 1 + 可读消息）。本地全量墙钟（空闲、`--max-children 4`、冷缓存）= **631.5s（10 分 31 秒）**，退出 0，判决与基线逐条一致（见 P2-7） |
| E19 | claim 93 条 = verify **44** + nightly 49（`reports/documented-facts.json`）；演进：main 73（verify 27）→ 审计 tip `e4a85dd` 87（verify **38**）→ tip 93（verify **44**）⇒ 任务书里的 "38 → 93 / verify 27 → 44" 三个数都能对上（38 是审计 tip 的 **verify 类**数，93 是 tip 的**总数**）。`facts` 作业（`--run verify`）空闲实测 **9.20s**（44/44 通过；CI 的 facts 作业没有紧预算，`ci.yml` 无 `timeout-minutes`，`nightly` 的两个 facts 作业是 30 分钟）|

---

## 3. 缺陷列表

### P0

**无。** A 组三项在我的独立注入下全部变红，未找到能让判据静默变绿的新路径
（`only_mutate` 缩范围、改名、全 `skipped`/`not checked`、未知状态、空结果集、`mutmut` 非零退出、
读结果失败，逐条实测都退出 1）。

### P1-1｜`--json-out` 在超时/中止路径不落盘，与帮助文本承诺不符

* **声称**：`--json-out` 帮助写"把本轮摘要写成 JSON 到这里（**判定失败时也会写**：nightly 要能上传它）"；
  `main()` 里的注释也写"放在判定之前写：判失败也有产物可看"。
* **实现**：写文件发生在 `run_mutmut()` **返回之后**。`run_mutmut()` 用 `SystemExit` 抛出超时；
  另外两条提前返回（`mutmut run` 非 0、`mutmut results` 非 0）同样在写文件之前。
* **最小复现**：

```bash
cd /tmp/vg-revert-conftest
.venv/bin/python scripts/mutation_check.py --timeout 1 --json-out /tmp/timeout-test.json; echo "EXIT=$?"
# [mutation] 超时失败：1 秒内没跑完（module=全部）。
#   这是**失败**而不是跳过：跑不完就无法判定「有没有新增盲区」。
# EXIT=1
ls /tmp/timeout-test.json         # → 不存在
.venv/bin/python scripts/mutation_check.py --module 'harness.loop.xǁ_Countersǁto_outcome__mutmut_28' \
   --timeout 900 --json-out /tmp/c9-drop-module.json; echo "EXIT=$?"   # → 1，且文件**存在**（判定失败路径）
```

* **影响**：判定结论仍成立（退出码非 0，fail-closed），但 nightly 的
  `actions/upload-artifact@v4`（`if: always()`）在**最需要看产物的那条路径**（跑不完/跑不起来）上拿不到任何 JSON。
  v4 默认 `if-no-files-found: warn`，所以不会让作业二次失败，只是没有任何可读产物。
* **建议**：把写文件挪到 `run_mutmut()` 之前（或 `finally`），或在帮助文本里写明"只有判定失败会写，
  超时/中止不写"。

### P2-1｜存在判决不可重现的变异体（判决随机器负载翻转）

* **现象**：`harness.execution.xǁToolExecutorǁ_replay_outcome__mutmut_37` 在**同一个 commit** 上，
  四次运行的判决不一致：

```
基线 reports/mutation_baseline.json                       : survived
/tmp/verify-gate 全量 run（3 个 run 并发 × 3 子进程，负载高）: killed   → killed=1332 survived=699
/tmp/vg-inj-unmut 全量 run（同条件并发）                    : survived  → killed=1331 survived=700
/tmp/vg-pytest 全量 run（--max-children 4，空闲）           : survived  → 1331/700
/tmp/vg-pytest 第二次全量 run（空闲，冷缓存）                 : survived  → 1331/700
```

* **相关性**：唯一翻转的那次是**高负载**下跑的 ⇒ 负载让某个对时间敏感的用例失败，变异体被判 `killed`。
* **最小复现**：`cd /tmp/verify-gate && .venv/bin/python -m mutmut results --all=true | grep _replay_outcome__mutmut_37`
  与 `reports/mutation_baseline.json` 对照（我那次是 `killed`，另外三次是 `survived`）。
* **影响**：这次翻转的方向（`survived → killed`）恰好是门禁**允许**的方向，所以没有误报；
  但判据把"`killed` → 本轮 `survived`"当新盲区，任何一条判决不稳定**反向**翻转都会**夜里误报红灯**。
  与上一轮的 P0 方向相反：这不是"静默绿"，是"假红"（会消耗维护者对红灯的信任）。
* **建议**：登记不稳定变异体清单，或在 CI 上对"判定类状态发生翻转"的条目做一次重跑确认。

### P2-2｜"没有任何用例的实际 HTTP 行为改变"与实测/自身注释矛盾

* 调查文档 §3 写："因为 `getproxies()` 在环境变量层短路，**没有任何用例的实际 HTTP 行为改变**"。
* 实测（我的 `proxy_impact.py`，四格）：在设置了 `http_proxy` 且未设 `no_proxy` 的环境里，
  `urlopen("http://127.0.0.1:PORT/")` **会被交给代理**（代理命中 1 次、直连 0 次）；加上 `no_proxy=*`
  之后才直连。⇒ 修法在这些机器上**确实把行为从"走代理"改成"直连"**。
* `tests/conftest.py` 的注释写对了这件事（"会把到 127.0.0.1 的请求交给代理——那是运气，不是设计"）。
  两份文本对同一件事的口径相反，**正确口径是 conftest 那份**（改变是期望语义，不是"没有改变"）。
* 影响：结论（修法正确、用例只连回环）不受影响；属口径不一致。

### P2-3｜交付汇报"每阶段共同门"里的 4 个数字是 M3 快照，与 tip 不符

| 文档里的数（M3 / `1d4b3f3`） | tip（`ed8b366`）实测 | 含义 |
|---|---|---|
| `pytest -o addopts= …` → 405 passed（装 otel） | **428 passed** | 全量收集数 |
| 同上、缺 otel → 404 passed, 1 skipped | **427 passed, 1 skipped** | 差的那 1 个 skip 结构不变 |
| `pytest -m mcp` → 14 passed | **15 passed** | `6706bd3` 新增一例（M3 时 17 deselected = 3 live + 14 mcp，tip 是 18 = 3 + 15） |
| `check_facts.py --run verify` → 38/38 | **44/44** | verify 类 claim 38 → 44 |

* 文档在更正块里**明确写了 commit（1d4b3f3）**，所以它是历史记录而不是错数；我实测 `1d4b3f3`
  的全量收集数确实是 **405**（`git worktree add /tmp/vg-m3 1d4b3f3` + `--collect-only`）。
* 风险：读者把这张表当"当前读数"直接复用就会对不上。建议在该表上直接标注"以下仅对 `1d4b3f3` 成立"。

### P2-4｜"超时判失败"的回归用例是源码字符串断言，不是行为用例

* `tests/test_mutation_gate.py::test_script_reports_timeout_as_failure` 只做两件事：
  `assert "这是**失败**而不是跳过" in source` 与 `assert "TimeoutExpired" in source`。
  把 `raise SystemExit(...)` 换成 `print(...)` 后这个用例**仍然通过**（字符串还在）。
* 行为本身是好的：我用 `--timeout 1` 端到端实测退出码 = **1** + 可读消息（见 P1-1 的复现）。
* 建议：加一格真行为用例（跑一个 1 秒超时或把 `subprocess.run` 打桩成超时），而不是 grep 源码文本。

### P2-5｜`print-time-estimates` 的 `<no tests>` 前缀是时间估计占位，不是状态

* 我实测：`mutmut print-time-estimates` 输出 **2049 行，全部以 `<no tests>` 开头**，
  而基线里的 `no tests` 真值只有 **18** 条。查 mutmut 源码（`__main__.py::print_time_estimates`）：
  真实含义是"该变异体的**预估耗时**为空 ⇒ 打印 `<no tests>`"，与 mutmut 的状态分类不是一回事。
* 文档把它当"列出数"用是**对的**（这正是当年"2023 vs 1110"要清的账），但没有一处写明这层含义，
  读者很容易把 2049 行读成"2049 条 no tests"。
* 建议：在引用处加一句"`<no tests>` 是耗时估计占位，不是状态；状态以 `status_by_mutant` 为准"。

### P2-6｜门禁的作用范围只覆盖 `only_mutate` 的四个模块（范围说明）

* 实测：把一段无任何用例覆盖的代码加进 `harness/ids.py`（在 `source_paths=["harness"]` 里、
  但**不在** `only_mutate` 里）⇒ 全量 run 退出 **0**，数字与基线逐条相同。
  加在 `harness/approval.py`（在 `only_mutate` 里）⇒ 退出 **1**。
* 这是设计如此（判据只在四个模块的变异体集合上对账），但 AGENTS.md 红线 5 要求的
  "新增机制必须做退化注入验证"在**范围外仍然靠人工**。建议在 `docs/testing.md` §3 的注入表里
  补一句范围说明，避免"以为变异门禁守住了所有新代码"。

### P2-7｜本地全量墙钟比文档声称慢（10 分 31 秒 vs 8 分 35 秒）

* 文档（`nightly.yml` 注释、调查文档 §4、`docs/testing.md` §2）：修复后一轮 **8 分 35 秒**（`--max-children 4`）。
* 我的实测（`/tmp/vg-pytest`，同机、同 Python 3.14.6、`--max-children 4`、冷缓存、**空闲机器**）：
  `[mutation] 用时 631.5s` = **10 分 31 秒**，退出 0，判决与基线一致（1331/700/18）。
  另外两次（都算干扰下的上界）：与我的 pytest 计时重叠 ≈746.3s；3 个 run 并发 ≈1355.7s（不可比）。
* 与声称相差 **1.23×**。唯一环境差异：我的 venv 装了 `[otel]+[mcp]`（CI 的 mutation 作业是 `[dev,eval]`，
  每轮测试子集因此多跑 `test_otel.py` 的那一例；`mcp` 组被 `-m not mcp` 排除）。
  ⇒ 这是"本机数字不可比"的一格，**不构成缺陷**，但文档把 8 分 35 秒写成实测值时未写环境。
* `--timeout 2400`（40 分钟）对我这份实测有 **3.8 倍**余量；CI runner 上够不够我无法在本机判定（见 §5）。

### P2-8｜`docs/testing.md` §2 的三处时长口径偏乐观

| 文档写的 | 我空闲机器上的实测 | 倍率 |
|---|---|---|
| `pytest -o addopts= -p no:cacheprovider -q` 注释"约 3–6 秒" | **13.83s**（428 passed）；默认选择（`-m 'not live and not mcp'`）**9.10s**（410 passed） | ~2–4× |
| `scripts/count_tests.py` 再生（同一注释） | **0.38s** | 比文档快 |
| `scripts/check_facts.py --run verify   # 约 5 秒` | **9.20s**（44/44 通过）；有 3 个全量 run 并发时 46.8s | 1.8× |

* 都不影响结论（没有任何机制依赖这些秒数），属口径不精确；但按本仓库"每个数字都要能再生"的纪律，
  建议把量级改写为"约 10 秒（本机空闲，3.14）"这类带口径的表述。
* 顺带一条正面证据：交付汇报的 CI 口径"`pytest -q` 由 7.6 s → 9.4 s"与我实测的默认选择 9.10s 同量级。

---

## 4. 无法判定项

1. **作者 V3 的"819 条 replay"数字**：把修复前的真实 `segfault` 判决集 replay 回结果表——那个集合来自
   已被覆盖写的 `mutants/*.meta`，作者也自陈"27 条差异无法逐条核对"。我只能验证**报告点名的 3 条**
   （A3 已变红），**819 这个数无法独立再生**；我能独立再生的等价证据是：把 conftest 两行去掉后
   真实产生 16 条 `-11`（含那 3 条）。
2. **`2022 → 2049` 那 27 条的逐条构成**：同上，原始 `.meta` 已不存在。
3. **Linux/CI 上的真实耗时**与"Linux 上不存在 `_scproxy` 同族风险"：本机无 Linux 环境，
   只有机理判断（`getproxies_macosx_sysconf` 仅 macOS 存在）。
4. **906 条 `-11` 的原始分布**：基线入库前的 `.meta` 已被重写，无法逐条复核。
5. **`no_proxy=*` 对"将来可能需要走代理的用例"的影响**：今天没有这样的用例（全仓 grep 无代理断言），
   属未来边界。

---

## 5. 未覆盖风险

1. **CI runner 上的墙钟未知**：本机空闲 10 分 31 秒（4 并行）⇒ 若 ubuntu-latest（2–4 vCPU）比本机慢 3.8 倍以上，
   40 分钟预算就会紧张。**这是"预算够不够"的唯一定量缺口**，且只有 CI 能回答（作者也这么写）。
   后果是**误报红灯**（退出 1），不是静默绿。
2. **基线可为 `--update-baseline` 整体刷新**：这是文档化的逃生门（要求 PR 里说明），
   技术上没有任何"比上一版更宽就拒绝"的守卫。属流程控制，不属技术缺陷。
3. **判据只按"名字 + 状态"对账**：基线没有存"每个变异体的变异内容/源码 hash"。
   同一函数体内的**等量改写**（常数改值、语句换序等既不增删变异体条数的改动）会让同一批名字
   指向**不同的**变异，而门禁看不出差别（可能静默绿）。属残余边界：一旦条数变化就会靠
   `vanished`/`new-survivors` 变红，所以要撞上这个窗口需要"改动恰好保持条数"。
4. **`mutants/` 与 `.mutmut-cache/` 均被 `.gitignore` 排除且未入库**，nightly 也无 cache 步骤
   ⇒ "陈旧缓存进 CI" 这条路径不存在（我实测的门禁读取范围依赖缓存，但缓存只在本地存在）。
5. **P2-1 的假红**：夜间随机误报会消耗维护者对红灯的信任（"狼来了"效应）。

---

## 6. 证据索引

| 产物（全部在 `/tmp`） | 内容 |
|---|---|
| `/tmp/a1-vanished.log` / `.json` | A1：真 mutmut 冷缓存单条 glob → 退出 1、2048 条 `not checked`、失败路径 JSON |
| `/tmp/vg-matrix/`（`matrix-summary.json` + 每格 `.log`/`.gate.json`/`.table.json`） | C 组 14 格判据矩阵（含 S4 未知状态、S7 改名、S10 正对照） |
| `/tmp/vg-shim/mutmut.py` | 我写的 mutmut 替身（喂任意结果表给真门禁） |
| `/tmp/vg-repro/gate_matrix.py` | 我写的 14 格矩阵驱动 |
| `/tmp/vg-repro/segv_repro.py` | B4 最小复现（`--hot` 对照） |
| `/tmp/vg-repro/proxy_impact.py` / `/tmp/vg-proxy-impact.json` | B6 代理影响四格 |
| `/tmp/vg-repro/approve_iserror.py` / `/tmp/vg-approve-result.txt` | D12 端到端两格（真 stdio 服务进程 + 外部账本） |
| `/tmp/run-pristine.log` / `.json` | A2a 未注入全量 run（退出 0；1332/699/18） |
| `/tmp/run-inj-mut.log` / `.json` | A2b 注入被变异模块（退出 1，`new-no-tests` 3 条） |
| `/tmp/run-inj-unmut.log` / `.json` | A2c 注入未变异模块（退出 0，1331/700/18） |
| `/tmp/revert-approval-run.log` + `/tmp/vg-revert-conftest/mutants/*.meta` | B5 反例：去掉 conftest 两行 → 16 条 `-11`（含 3 条点名） |
| `/tmp/vg-revert-conftest/mutants/**/*.meta`（另见 `/tmp/verify-gate/mutants/**/*.meta`） | B5 正例：修复版 `-11`=0、`-24`=0 |
| `/tmp/c9-drop-module.log` / `/tmp/c10-path-module.log` / `/tmp/fc-run.log` / `/tmp/fc-results.log` / `/tmp/timeout-test.log` | C9-1 / C10 / fail-closed / 超时路径 |
| `/tmp/summary-only.json` | C11 `--summary-only` 字段类型 |
| `/tmp/solo-run.log` / `/tmp/run-clean.log` / `/tmp/vg-pytest/mutants/**/*.meta` | 墙钟测量（重叠负载 746.3s / **空闲 631.5s**）+ 修复版 `.meta` |
| `/tmp/chain.out` | E15e/E15f：`crash_matrix --repeats 5`（16 格 as-predicted / SIGKILL 70/80 / 0 违例）与 `opsenv.suite --gate`（1536 次运行 / 14 门禁全通过）|
| `/tmp/facts-verify.json` | `check_facts --run verify` 44/44 通过的原始报告 |
| `/tmp/solo-run.timing` / `/tmp/clean-run.timing` | 墙钟起止时间戳 |
| `~/Library/Logs/DiagnosticReports/Python-2026-09-18-233047.ips` | B4 崩溃栈 |
| `/tmp/vg-m3`（git worktree @ `1d4b3f3`） | E17 用的 M3 收集数（405） |

---

## 7. 与作者声称的差异

| # | 作者声称 | 我的实测 | 判定 |
|---|---|---|---|
| 1 | "修复后一轮 8 分 35 秒（`--max-children 4`）" | 空闲机器 **631.5s ≈ 10 分 31 秒**（同机同 Python，冷缓存，`--max-children 4`；我的 venv 多装 `[otel]`）；重叠负载时 746.3s | **不一致（1.23×）**（P2-7）；40 分钟预算对我这份实测仍有 ~3.8× 余量 |
| 2 | 调查文档 §3："没有任何用例的实际 HTTP 行为改变" | 配了 `http_proxy` 的机器上，回环请求从"走代理"变成"直连"（代理命中 1 → 0） | **不一致**（P2-2）；conftest 注释的写法才是对的 |
| 3 | V2 注入记录："`no tests` 10 条，退出 1" | 我的注入是 3 条（函数体更小），同样退出 1 且唯一红灯是 `new-no-tests` | 一致（条数随注入代码不同，非差异） |
| 4 | V1 注入是"往**基线**里塞 180 条幸存变异" | 我做的是**结果侧**缺项（真 run 只评估一条；替身里真删条目），两种都红 | 一致（我的两格覆盖面更大） |
| 5 | "超时仍然判失败（有对应用例）" | 行为成立（端到端退出 1）；但那个"用例"是源码字符串断言 | **部分不一致**（P2-4） |
| 6 | 交付汇报：405 / 404+1 / mcp 14 / facts 38（标注 commit `1d4b3f3`） | `1d4b3f3` 全量收集数确实是 405；tip 上是 428 / 427+1 / 15 / 44 | 一致（历史快照），但易被误读成当前值（P2-3） |
| 7 | A1 的标签是 `vanished-from-results`（任务书也这么期望） | 真 mutmut 场景下标签是 `decided-to-inconclusive`/`inconclusive-grew`，因为 mutmut 把未评估条目**列出来**标成 `not checked`；`vanished-from-results` 只在"结果表里真的没有这条"时出现（我用替身验证它会触发） | 一致（两条路径都红，标签取决于 mutmut 是"列出"还是"不列出"） |
| 8 | `--module` 帮助说"判定读的是全量结果表" | 实测读的是**当前配置生成的整个集合**（冷缓存下未评估的是 `not checked`，暖缓存下保留旧状态） | 一致（帮助文本与实现相符） |

---

## 8. 结论摘要

* **能不能合**：**能合**。A 组三项（缺项 / 新增未覆盖代码 / 活体 `segfault` 回放）在我的独立注入下
  **全部变红**，对照格全部保持绿；没有发现任何仍能静默变绿的新路径（14 格判据矩阵 + 5 格真 mutmut 场景
  + 3 格 fail-closed），也没有 P0。
* **必须先修什么**：没有阻塞项。建议（不阻塞合并）优先处理 **P1-1**（超时路径无 `--json-out`，
  nightly 在该路径拿不到产物）与 **P2-1**（不稳定变异体 `_replay_outcome__mutmut_37` 会造成夜间假红）。
* **我无法判定的**：V3 的 819 条 replay 与 `2022→2049` 那 27 条的逐条构成（原始 `.meta` 已被覆盖写）；
  Linux/CI 上的真实墙钟与同族 fork 风险（本机无 Linux）；906 条 `-11` 的原始分布。
* **最值得下一轮盯的**：CI runner 上 `--timeout 2400` 是否够用（本机 12 分 26 秒，若 runner 慢 3 倍以上会误报红灯），
  以及 P2-5（`print-time-estimates` 的 `<no tests>` 前缀）这类"数字对了但含义没写清"的口径坑。
