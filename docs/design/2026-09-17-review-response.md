# 评审响应：W9 交付的 P0/P1/P2 处置（`fix/review-p0-p1`）

* 评审报告：[`2026-09-17-architecture-review.md`](2026-09-17-architecture-review.md)
  （2 个 P0 + 3 条阻塞性 P1 + 26 条 P2）
* 本次响应的原则：**每条都给出"修复前会怎样"**，凡涉及门禁的改动都做退化注入验证；
  与正确性/安全性相关的 P2 一并修，纯风格项按原文建议处理
* 口径：本文件里的数字均可由命令再生；用例数等演进类数字引用 claim id

---

## 1. 自纠自查：先确认评审说的是事实

| 评审结论 | 我的复现方式 | 结果 |
|---|---|---|
| P0-1 变异门禁在"什么都没跑出来"时绿 | `.venv/bin/python -m mutmut results`（清空 `mutants/`）→ 退出 0、stdout 为空；对照 `mutation_summary({}) → new_survivors=[] → return 0` 的代码路径 | **成立** |
| P0-2 README/HANDOFF 的数字与入库基线矛盾且门禁全绿 | `grep -n "1642\|628" README.md docs/HANDOFF.md` vs `reports/mutation_baseline.json` 的 `{killed:1031, survived:625, total:1670}` | **成立** |
| P1-1 租约没有生产接入点 | `grep -rn "SingleWriterLease\|guarded_write\|lease.require\|fold_lease" harness opsenv experiments scripts`（排除 `harness/lease.py` 自身）→ **零命中** | **成立** |
| P1-2 `sweep` 自称全库扫描、实则只扫一个 run 目录 | 读 `harness/artifacts.py`：docstring 写"不许只扫当前 run"，实现只打开 `run_dir/runtime.db` | **成立** |
| P1-3 `--skip-sensitivity` 把"没有证明"打印成"通过" | 读 `experiments/chaos_fuzz.py`：跳过分支硬编码 `{"sensitive": True}` | **成立** |

评审报告里"机制层可以合并"的判断我逐条抽查后同意：哈希链、schema v3 迁移、
ArgPolicy 闸门、cassette 三模式、OTLP、artifact sweep、租约、oracle、噪声人格的
边界与结构没有发现第二权威、没有削弱 append-only、没有自证指标。

## 2. P0（不修不能合）

| # | 处置 | 修复前会怎样（回归用例的口径） | 验证 |
|---|---|---|---|
| P0-1 | `scripts/mutation_check.py`：① `mutmut run` 退出码非 0 ⇒ 判定作废；② 变异体总数 0 ⇒ **失败**（"跑不出来"≠"没有盲区"） | 配置没生效 / 缓存被清 / collect error / OOM 都会让 nightly **静静地变绿**，而且基线里的 625 条幸存变异会被报告成"已被杀死" | 退化注入两次：结果集为空 → 退出 1；`mutmut run` 退出 3 → 退出 1。回归用例 `tests/test_mutation_gate.py::test_empty_result_set_is_a_failure_not_a_pass`、`::test_mutmut_non_zero_exit_voids_the_verdict` |
| P0-2 | README / HANDOFF 的变异数字对齐入库基线，并**指向 claim**；同时落地 P1-4 的结构性收口 | 文档写 1642/1000/628、基线是 1670/1031/625，而 `facts` 门禁 27/27 全绿——**A-M1 要消灭的缺陷在 A-M1 的交付里复发** | `.venv/bin/python scripts/check_facts.py --run verify` 27/27；`grep` 旧数字零命中 |

## 3. P1（合并前应修）

| # | 处置 | 说明 |
|---|---|---|
| P1-1 | `docs/semantics.md` §3 措辞收窄：本实现提供的是**校验入口**（`require`/`guarded_write`），**runtime 写路径尚未接入**；"未持有 ⇒ 可读失败"只在调用方显式调用时成立；下一波接入时需要重新过语义文档 | 承诺清单里不留可被误读成"已接入"的句子 |
| P1-2 | `referenced_digests` 改为接受**多个 run 目录**；`sweep(..., run_dirs)`；CLI 的 `--run-dir` 可重复；**共享 artifacts root 时 `--apply` 拒绝执行**（除非显式 `--force`）；docstring 改成事实；报告新增 `unreadable_payloads` / `shared_root` / `scanned_run_dirs`；解析失败的 payload **计数上报**而不是静默吞掉 | 修复前：`--run-dir A --artifacts-root SHARED --apply` 会删掉**只被 run B 引用**的对象。回归用例 `tests/test_artifact_sweep.py::test_apply_refuses_a_shared_root_without_force`、`::test_unreadable_payload_is_counted_not_swallowed` |
| P1-3 | **删除** `--skip-sensitivity`：一个只能关掉安全阀的旗标没有合法用途；敏感性自检现在是 verdict 的必要条件 | 修复前：同一份"findings=[]"既能被打成"失败"（默认）也能被打成"通过"（加旗标）——而作者自陈第 2 条把全部证明重量压在这个自检上 |
| P1-4 | `check_facts.py` 新增**引用位置校验**：`docs` 里每条 `路径#锚点` 必须指向真实存在的文件 + 逐字出现的锚点；72 条 claim 的 `docs` 全部改写成锚点形式并逐条核对 | 退化注入：把某条 claim 的锚点改成文件里不存在的片段 → 退出 1 并指出锚点。修复前：`docs` 是自由文本、不参与对账，README/HANDOFF 的数字漂了也无人守 |

## 4. P2（26 条）

| # | 处置 |
|---|---|
| P2-1 | 尾行 `event_hash` 为空 ⇒ **写入时报 `StoreError`**（不再静默挂 genesis/fork 点，把发现时机从"下次离线校验"提前到"写入时"）。回归用例 `test_appending_on_a_broken_tail_is_refused` |
| P2-2 | 迁移 docstring 里的测试名改正为 `test_migration_backfills_the_chain_and_recreates_triggers` |
| P2-3 | 新增"旧库含分叉分支"的迁移用例（补链必须按谱系顺序算）：`test_migration_handles_a_forked_legacy_branch` |
| P2-4 | 三个 checker 统一走 `_runtime_snapshot`：坏库一律给 `inv_log_readable`（不再有 checker 抛异常让 `audit_run` 崩掉）。用例 `test_all_three_checkers_report_an_unreadable_db` |
| P2-5 | 账本不存在 ⇒ 显式 `inv_ledger_present` + `facts.ledger_present=false`（裁判缺席不等于没有副作用）。用例 `test_missing_ledger_is_a_finding_not_an_empty_pass` |
| P2-6 | `unknown` 的免罪通道加上界：每键至多 1 行（W2 语义），每键行数进 `facts.unknown_rows_by_key`。用例 `test_unbounded_unknown_rows_are_caught` / `test_single_unknown_row_still_exonerates` |
| P2-7 | fuzz 的"被杀到"改为**依赖 marker 证据**：`killed_verified` 计入 summary，`kills_without_marker > 0` 即判红；报表增列 |
| P2-8 | SIGTERM 探测器的 `ok` 包含 `sigterm_sent` 与 `exit_code != 0`（进程提前退出即失败） |
| P2-9 | SIGSTOP 循环包在 `try/except BaseException` 里：任何中途失败都 best-effort `SIGCONT` + `kill()`，绝不把子进程留在冻结态 |
| P2-10 | claim 命令超时按"该条失败"记账（`TimeoutExpired` → 该 claim 的 record 带可读原因），不再整轮 traceback |
| P2-11 | `count_tests.py` 检查 pytest 退出码：collect error 即失败（不给一个偏小的计数） |
| P2-12 | 成本口径写准（`semantics.md` §2.7）：无预算账本时直接回放 `entry.cost_usd`（`or` 改成显式判断，"确实 0 成本"不再被当成缺值）；有预算账本时由账本按当前价目表记账；`price_version` 不一致 ⇒ `price_version_mismatch` 警告 |
| P2-13 | 一致性对账：`outcome_resume.json` 缺失 ⇒ 报错（不再两侧都缺判"一致"）；比较键表与公布的 `WHITELIST` **合并成同一份**（`OUTCOME_KEYS`），并把 `outcome.input_tokens` 等补进白名单 |
| P2-14 | 新增 CLI 用例走 `--otlp-endpoint` 分支（未监听端口 + OTLP/JSON，断言可读失败与非零退出） |
| P2-15 | 去掉 `export_otlp` 的死 `timeout`（改为真的传给 exporter）、`build_sdk_spans` 的死 `context`、`_load_sdk` 里未使用的 `SimpleSpanProcessor` |
| P2-16 | 删除死常量 `MUTMUT` |
| P2-17 | 快照纪律抽成唯一实现 `harness/store/snapshot.py`，`harness/audit_chain.py`、`opsenv/oracle.py`、`scripts/probe_tornwrite.py` 三处共用 |
| P2-18 | `audit_chain` 对 v2 库给出可读原因与下一步（"先跑一次 `setup()` 触发迁移"） |
| P2-19 | 双进程冒烟的恒真断言删掉，改成**真断言**："至少一个进程必须被拒绝" |
| P2-20 | 噪声混合比例的用例改走生产路径（`perturb_diagnosis` 的 `detail` 分类），不再复刻 stdlib 的一行 |
| P2-21 | usage 归一：认得出形态但缺关键段（`completion_tokens` / `output_tokens` / 输入侧字段）⇒ **报错**，不按 0 计费 |
| P2-22 | `orphan_count` 的保留原因写进 docstring（被 `tests/test_w4_components.py` 使用，sweep 已是替代路径） |
| P2-23 | `docs/semantics.md` 增加**变更记录**表（W1→W9 逐段：谁在哪个里程碑改了什么、性质是扩大还是澄清） |
| P2-24 | `nightly.yml` 的注释与步骤名对齐实现（`--timeout 1320`、组合×阶段×试次的真实口径）；`chaos_fuzz` 里的 `assert` 改为显式 `SystemExit`（`-O` 下 assert 会被剥掉） |
| P2-25 | `harness/lease.py` 的 `assert` 改为显式 `LeaseError` |
| P2-26 | 随 P1-3 一并处理 |

### 评审 §5 的两条专项建议

* **golden vector**：`tests/test_hash_chain.py::test_chain_bytes_are_pinned_by_a_golden_vector`
  把 `record_bytes` 的**字节串**与 `event_hash` 的值钉死（含 `1.5` 浮点、中文键、
  `sort_keys` 后的键序）。改这条向量等于宣布历史链全部失效——注释里写明了这一点。
* **"第一个断点"的定位**：链首"挂错地方"的判定从循环之后**移到循环之前**，
  不再与其它违规互斥（"首条挂错 + 后面还有断点"时也能先报首条）；
  未迁移库（事件没有哈希）走另一条更贴切的报错。

## 5. 验证（本次修复后的实测）

```
.venv/bin/pytest -o addopts= -p no:cacheprovider -q      → 全绿（用例数见 claim `tests-collected`）
.venv/bin/ruff check .                                    → All checks passed
.venv/bin/python scripts/check_facts.py                   → 72/72 通过（含新增的引用位置校验）
.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate → 退出 0（14/14）
.venv/bin/python -m experiments.crash_matrix --repeats 5  → 退出 0（16 格 as-predicted、70 次注入）
.venv/bin/python scripts/mutation_check.py                → 退出 0（无新增幸存变异）
.venv/bin/python -m experiments.chaos_fuzz --repeats 30 --seed 20260917 → verdict green（180 次注入全部有 marker 证据）
.venv/bin/python scripts/replay_consistency.py            → identical: true
```

三条退化注入（本轮新增/修改的门禁）：

| 门禁 | 注入 | 结果 |
|---|---|---|
| `mutation` | 让结果集为空 | 退出 1（"没跑起来"≠"没有盲区"） |
| `mutation` | `mutmut run` 退出 3 | 退出 1（判定作废，不读缓存） |
| `facts`（引用位置） | 锚点改成文件里不存在的片段 | 退出 1 并指出锚点 |

## 6. 仍未处置 / 需要所有者拍板的

1. **评审 §8 的待确认项**：变异基线在 CI runner（Python 3.12、4 并行）上是否与本地一致——
   建议合并后手动 dispatch 一次 nightly `mutation`；若幸存清单不同，在 runner 环境
   `--update-baseline` 并在 PR 记录原因（本次修复额外把 `docs/` 与 `README.md` 加进了
   mutmut 的 `also_copy`，否则 facts 门的引用校验会在沙箱里失败）。
2. **`reports/chaos_fuzz_report.json` 的体积**（180 条 trial 明细）：本次只做了
   "报告里不再把无 marker 的试次算成注入成功"，**没有**改入库策略——
   汇总入库 + 明细走 `--runs-out` 的拆分需要所有者定调（它是 claim 的输入）。
3. **INV-008 仍无运行时执行点**：链校验只有离线 CLI 与增量 API，
   "篡改无法静默"成立的前提是**有人跑校验**（运维前提，不是代码保证）。
4. **租约仍未接入写路径**：语义文档已按事实收窄措辞；接入是下一波的事，
   届时需要重新过一遍 `docs/semantics.md` §3 与 §7。


---

# 附：独立功能验证报告的处置（2026-09-17，第二份外部输入）

* 报告：[`../independent-test-2026-09-17/report.md`](../independent-test-2026-09-17/report.md)
  （被测对象是交付 tip `49f9f51`，即**本次修复之前**的状态）
* 口径：报告里的 P0-1、P1-3、P1-4、P2-1 与本文档 §2/§3 是同一批问题，
  已在 `fix/review-p0-p1` 的上一提交里修掉；下面只列**那一轮没覆盖**、本轮补修的条目

## 与本文档重合（上一提交已修）

| 报告条目 | 状态 |
|---|---|
| P0-1 变异门禁零产出仍绿 | 已修（`mutmut run` 非 0 即作废 + 变异体总数为 0 即失败 + 回归用例） |
| P1-3 GC 只扫单 run、共享根误删 | 已修（多 run 目录 + 共享 root 拒绝 `--apply` + 回归用例） |
| P1-4 租约承诺缺"无运行时接入点"限定 | 已修（semantics §3 措辞收窄） |
| P2-1 README/HANDOFF 变异数字与基线不符 | 已修（数字对齐 + 引用位置校验） |
| P2-3 oracle `inv_log_readable` 不可达 | 已修（三个 checker 统一走 `_runtime_snapshot`） |
| P2-10 `--skip-sensitivity` 伪造"通过" | 已修（旗标删除） |

## 本轮补修（上一轮未覆盖）

| 报告条目 | 修复前会怎样（回归用例的口径） | 处置与证据 |
|---|---|---|
| **P1-1** fuzz 的 verdict 不要求"注入真的命中" | 标定系统性偏大时 18 次试验**一次都没杀死进程**，报告仍打印"0 新类违例"并退出 0——而 `fault-spectrum.md` 自己把这种形态称为"最危险的一种假绿" | `compute_verdict` 纯函数化，加三条硬条件：命中率 ≥ 50%、**每格至少命中一次**、无 marker 的试次 = 0；报表打印命中率与失败理由。破坏复现（标定改 2000）→ 退出 1；健康路径 → 绿（4 条用例） |
| **P1-2** claim 不覆盖"文档正文 ↔ claim"这一跳 | 改 README 的"命中率 57.4% → 0%"为 87.4% ⇒ 门禁仍 27/27 通过 | 新增 `doc_literals` 字段（`路径#逐字片段`），23 条 claim 登记正文里被引用的数字片段；同一份校验函数复用。破坏复现 → 变红并指出片段（3 条用例） |
| **P2-2** semantics §7.5 与 §2.6 自相矛盾 | 承诺清单里同时写"GC 已实现"与"GC 未实现回收" | §7.5 改为已实现并指向 §2.6 的限制 |
| **P2-4** torn write 措辞不准 | 文档说"三种截断都让主库不可打开"，实测 50%/90% 不可打开、尾部 −4KB 那档**可打开**（integrity_check 报错） | `fault-spectrum.md` 按实测改写（结论"绝不静默续跑"三档一致） |
| **P2-5** 变异门禁的限时口径 | 文档写"15 分钟写死在脚本参数里"，实际 `--timeout 1320`（22 分钟） | `testing.md` 写明实际预算与"总数为 0 也判失败"；`nightly.yml` 注释上一轮已改 |
| **P2-6** replay 的"指纹不一致 ⇒ 警告"没有出口 | 警告只在 LLM client 里 append：worker 不打印、不落盘，事件日志里查不到 | worker 把 `replay_warnings` 写进 `outcome_<mode>.json` 与 stdout，并在 stderr 逐条打印（仍不判失败）。端到端验证：篡改 cassette 指纹 → outcome 里出现 1 条警告 |
| **P2-7** cassette 的**文本**不在任何校验面内 | 把录制文本换成等 token 数的另一句话 ⇒ replay 全绿、对账 `identical=true`，而篡改后的文本已落进事件日志 | 一致性对账的白名单/键表都加入 `agent_message`/`user_message` 的 `text`；`model-modes.md` §5 的表同步。验证：篡改文本 → 报 `events.texts` 差异（2 条用例） |
| **P2-8** `ArgPolicy(allowed=[1])` 接受 `True` | `True == 1` 让布尔值"等价穿透"数值白名单（forbidden 侧同源，方向相反） | `_matches`：布尔只与布尔相等（双向）。验证：`allowed=[1]` 拒 `True`、`forbidden=[1]` 放行 `True`、布尔白名单拒数字（2 条用例） |
| **P2-9** `SweepReport.never_deleted` 是死字段 | 报告读者会以为它表示"永不回收清单" | 删除该字段 |
| **P2-11** 交付报告"316 passed"用词偏 | 实际是 `collected 316`（默认集 312 passed + 1 skipped，live 主题 3 条单跑） | 交付报告该行改写为准确口径 |
| **P2-12** `check_facts` 默认输出目录固定 `/tmp/facts` | 同机两条会话并发跑会互相 `unlink` 对方产物，表现为"命令未写出 …"的误报 | 默认改成 `/tmp/facts-<随机后缀>`（每次运行独立），`--outdir` 仍可显式固定 |

## 报告点名"未覆盖"的那一组门禁：本轮补做了退化注入

报告 §5 的风险 0 说得很准：它逐条验过新门禁的假绿方向，但**没有**验
`opsenv.suite --gate`、`check_gates` 的 14 条、`crash_matrix` 的 `as_predicted`、
`audit_chain` 的退出码。我在隔离副本（`/tmp/acl-brk`，原仓库只读）里各做了一次破坏注入：

| 门禁 | 注入（把机制改坏） | 实测结果 |
|---|---|---|
| `opsenv.suite --gate` / `check_gates` 的机制门禁 | 把写工具的 `requires_approval=True` 改成 `False`（gate 不再开门） | **退出 1**，5 条门禁红：`harness.red_line[competent]==0`、`harness.red_line[weak]==0`、`harness.gated[weak]==1.0`（实测 0.000）、`harness.blocked[weak]>0`、配对 CI 上界 <0 |
| `crash_matrix` 的 `as_predicted` 判定 | 把格子里的 `--outbox` 强制改成 `off`（保护全关） | **退出 1**，`prediction_violations=4`（2 格 prediction-violated） |
| `harness.audit_chain` / `verify_chain` 的链校验 | `verify_chain` 不再重算哈希（直接采信记录值） | `scripts/chain_mirror_check.py` **退出 1**；`tests/test_hash_chain.py` 3 条红（mirror test、CLI 退出码、父分支被改） |

结论：这四条门禁在"机制被改坏"的方向上都会红，不属于"没跑起来也绿"的那一类。

## 报告给出的两条边界（已写进文档）

1. **哈希链的整段重算无法检测**（报告 §5.4）：改一条 + 从该条起重算全部哈希 ⇒ 校验通过。
   这是无外部锚点哈希链的固有性质；`semantics.md` §2.4.1 已如实写明
   "要让『无法静默』严格成立需要把最新 `event_hash` 锚到链之外，本包没做"。
2. **SIGTERM 探测器的自校正**（本轮自查发现，不在两份报告里）：上一轮按 P2-8 把
   `ok` 收紧成"必须证明信号发出过"之后，机器负载高时 resume 可能**在延迟之前跑完**，
   于是探测器随负载翻红——而 flaky 的门禁很快会被忽略。现在改为**延迟依次减半重试**
   （最多 4 次），只有所有尝试都没能发出信号才判失败（那是"测不到"，不是"碰巧绿"）；
   结论里记下实际落在哪个延迟上。
3. **oracle 的判定强度**：`inv_effect_accounting` 的"免罪通道"（unknown）与账本缺席
   这两条通道，上一轮已分别加上界与显式发现；报告 §5.4 的"整段重算"是链的边界，
   不是 oracle 的，两者分开记录。
