# 测试规范（Testing Guide）

> **约束对象**：测试 agent（独立 / 外部）与开发 agent 的自测。
> **一句话版本**：测试是承诺的可执行形式；**裁判必须是外部账本，不能是被测对象自己的日志**。

---

## 1. 测试分层与各自职责

| 层 | 位置 | 回答什么问题 | 谁写 |
|---|---|---|---|
| 单元 / 组件 | `tests/test_*.py` | 单个机制对不对（视图、压缩、预算、工具、审批…） | 开发 agent |
| 不变量 / 协议 | `tests/test_state.py`、`test_checkpoint_protocol.py` | 事件折叠的 7 条不变量、checkpoint/writes 提交协议 | 开发 agent |
| 回归（审计固化） | `tests/test_audit_regressions.py` | **曾经修过的 P0 不许复活**；每个用例 docstring 写明"修复前会怎样" | 开发 agent |
| 统计 / 门禁 | `tests/test_stats_and_gates.py` | 区间、配对、门禁本身的正确性 | 开发 agent |
| 崩溃矩阵 | `experiments/crash_matrix.py` | 命名窗口下的一致性结论（真 SIGKILL，外部账本裁决） | 开发 agent |
| 评测套件 | `python -m opsenv.suite --gate` | 四路线对照 + 可执行门禁（条数见 claim `suite-gate-count`） | 开发 agent |
| **独立黑盒测试** | 报告落 `docs/independent-test-<日期>/` | **机制承诺是否成立**（先读规格、后读答案） | **测试 agent（独立会话）** |

---

## 2. 必跑命令

```bash
# 快速档（每次提交前）
.venv/bin/pytest -o addopts= -p no:cacheprovider -q     # 用例数不手写：claim `tests-collected`
                                                       # （`scripts/count_tests.py` 再生）
                                                       # 时长按本机 3.14.6 实测：**约 15 秒**
                                                       # （这个数字按仓库纪律只用于本机参考，
                                                       #   不进 claim：机器不同就不同）
.venv/bin/ruff check .
.venv/bin/python scripts/check_facts.py --run verify    # 文档数字对账（轻 claim 集）
                                                       # 时长实测 **约 54 秒**（W11 起 W7 的阈值扫描
                                                       # `context_sweep --repeats 2` 也在这个集合里，
                                                       # 它独占约 38 秒；在此之前的 9 秒是旧口径）

# 完整档（合并涉及语义/评测的改动前）
.venv/bin/python -m experiments.crash_matrix --repeats 5         # 16 格全 as-predicted
.venv/bin/python -m experiments.context_cost                     # W4 成本对照（W7 成本数字的再生命令，见 §2 说明）
.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate  # 1536 次运行 + 全部门禁（条数见 claim `suite-gate-count`）
.venv/bin/python -m experiments.context_sweep --repeats 2        # 阈值扫描（本机实测约 38 秒；W7 数字的再生命令）

# 整量对账与重作业（CI 的 nightly 作业跑的就是这几条）
.venv/bin/python scripts/check_facts.py                 # 全部 claim（条数以 --list 为准；约 100 秒）
.venv/bin/python scripts/mutation_check.py              # 变异门禁（全状态记账 + 防倒退）
                                                       # 超时预算由 --timeout 给出（nightly 用 2400s = 40 分钟）
                                                       # 2026-09-18 修掉 segfault 误判后，原先"一进去就崩"的
                                                       # 那部分变异体会真的跑完测试：本机**空闲**冷跑实测
                                                       # **631.5 秒 ≈ 10.5 分钟**（带负载时读数会更高）
                                                       # ⚠️ 变异体总数为 0、或一条都没被判定，都判失败
                                                       # （"跑不起来"≠"没有盲区"）；超时/中止路径
                                                       # **也会写 `--json-out`**（nightly 要能上传它）
                                                       # ⚠️ 刷新基线（`--update-baseline`）时 `mutants/`
                                                       # 会被**自动整体移开**再跑全量：`mutmut run` 是增量的，
                                                       # 缓存还在就只重跑"函数哈希变了"的变异体，写出的基线
                                                       # 会混合旧判决（2026-09-19 实测过一次：14.7 秒"跑完"、
                                                       # `no tests` 从 18 虚增到 241）。要沿用旧缓存必须显式
                                                       # `--allow-incremental-refresh`（会大声警告）
                                                       # ⚠️ 每次运行都打印"不可见空间"的规模：
                                                       # survivor_rate 不是覆盖率
                                                       # ⚠️ 判据的适用边界（三条，别读过头）：
                                                       # ① 变异范围由 pyproject.toml [tool.mutmut].only_mutate
                                                       #    决定：注入到**未变异**模块的代码不参与变异，
                                                       #    门禁不会因此变红（那是范围外，不是漏检）；
                                                       # ② `mutmut print-time-estimates` 输出里的
                                                       #    `<no tests>` 是**耗时估计占位**，不是状态标签，
                                                       #    别读成"这么多条没有测试"；
                                                       # ③ 单条变异体的判决**不承诺稳定性**：
                                                       #    实测 `_replay_outcome__mutmut_37` 会随机器负载
                                                       #    在 survived ↔ killed 之间翻转（这次方向无害，
                                                       #    但反向翻转就是夜里误报红灯）。
.venv/bin/python -m experiments.chaos_fuzz --repeats 30 --seed 20260917   # 随机时刻 fuzz
.venv/bin/python scripts/probe_sigterm.py               # 谱系探测器（三档，各约 10–20 秒）
.venv/bin/python scripts/replay_consistency.py          # scripted ↔ replay 白名单一致性
```

`live` marker 的用例（`tests/test_live_model.py`）**默认不跑**（`addopts` 里过滤标记）：
它们是 live 路径的**反例**，不需要 key。CI 用 `pytest -m live` 单跑这一组；
真正的在线调用在本仓库永不执行。注意仓库里的规范命令写作 `-o addopts=`（覆盖 addopts），
那会连 live 组一起跑——它们仍然安全（测试自己清掉环境变量、绝不联网），只是不属于默认口径。

跑完必须核对**退出码**，不要只看输出文本：管道/重定向会吞掉退出码（历史事故：
JSON 序列化异常被管道吞掉，报告缺了一整块）。

---

## 3. 改动 → 测试义务（硬性）

1. **修一个缺陷 ⇒ 必须新增一个回归用例**，并写清"修复前这个用例会怎样失败"。
   参考 `tests/test_audit_regressions.py` 的 docstring 风格。
2. **新增一个机制 ⇒ 三件套**：
   * 正常路径用例；
   * 失败/崩溃路径用例（能注入则注入，不能注入则断言错误分类）；
   * 不变量或结构断言（该机制"无论如何都不该发生"的事）。
3. **新增/修改一条门禁 ⇒ 必须做退化注入验证**：故意把对应机制改坏，确认 CI 变红
   （退出码 1），再还原。**"没有坏事发生"不等于"机制在工作"**——历史教训：
   第一版门禁抓不到"静默丢弃破坏性动作"的退化，全绿但机制已死。

   W9 新增的三条门禁各自的退化注入做法（都已实测过一次）：

   | 门禁 | 怎么把它弄红 | 期望 |
   |---|---|---|
   | `facts`（文档数字） | 把某条 claim 的 `value` 改掉 | 退出 1 并报出"期望 X±tol，实测 Y（偏离 Z）" |
   | `mutation`（新增盲区） | 在被变异模块里加一个**没有任何用例调用**的函数（含一条永不被执行的分支） | 退出 1 并列出 `[new-no-tests]` 的变异体名 |
   | `mutation`（盲区从视野里消失） | 把基线里一条 `killed`/`survived` 的变异体改成不在结果表里（或让它被报成 `segfault`/`timeout`） | 退出 1 并分别报 `[vanished-from-results]` / `[decided-to-inconclusive]` + `[inconclusive-grew]` |
   | `mutation`（**没跑起来**） | 让 `mutmut run` 退出非 0，或让结果集为空 | 退出 1（"跑不出变异体"不是"没有盲区"；评审 P0-1） |
   | `facts`（引用位置） | 把某条 claim 的 `docs` 锚点改成文件里不存在的片段 | 退出 1 并指出哪个锚点找不到（评审 P1-4） |
   | `chaos-fuzz`（随机注入） | 把 oracle 的 `inv_effect_accounting` 判定注释掉 | 敏感性自检失败（"全绿"变成无意义），退出 1 |

   **W11 新增的四条门禁**（独立验证 2026-09-19 · P0-3 / P1-1；动机是旧门禁只断言
   "某个比率等于多少常数"，把**指标的定义**改掉可以让它们全绿而结论已经失效）：

   | 门禁 | 断言的关系 | 怎么把它弄红 | 期望 |
   |---|---|---|---|
   | `workflow.correct==workflow.sufficient` | 规则基线的充分率 ≡ 正确率（"取证充分性决定正确率上限"的定义式） | 让 `workflow` 的 `is_sufficient` 恒真（`sufficient` 与 `correct` 解耦） | 退出 1；只这一条红，`statistics` 与其余门禁不变 |
   | `gated_routes.novel_red_line==0` | 有审批门的路线一次都不执行"静态拒绝列表之外的破坏性动作" | 把"新动作"从 operator 与红线判据里摘掉（`executed_actions` 不再计入 `novel_red_line`） | 退出 1；只这一条红（harness / langgraph 各 3 次） |
   | `single_shot.novel_red_line>0` | 对称自检：场景集里必须**仍有**新动作被无门路线踩中（否则上一条是空真） | 让场景目录里不再出现"拒绝列表之外的动作" | 退出 1；只这一条红 |
   | `crn.evidence_routes_agree` | 三条读证据的路线在同一配对键上给出同一 (诊断, 动作) | 把 system 名写回随机种子（CRN 被破坏） | 退出 1；本机实测 211 个配对键不一致，**旧门禁全绿** |

   还原后基线是全绿。（四条各自的单元用例在 `tests/test_stats_and_gates.py`
   的"指标**定义**门禁与 CRN 门禁"一节，每条都带"改坏什么会让它红"。）

   **W11 补的三层 append-only 防线**（独立验证 2026-09-19 · P2-5；`harness/store/guard.py`）
   不是"门禁"，但同样是"改坏了必须有人报警"的机制，因此按同一条纪律做了退化注入
   （本机实测，注入后只跑 `tests/test_append_only_guard.py`，注入文件已还原）：

   | 层 | 怎么把它弄坏 | 期望 | 实测 |
   |---|---|---|---|
   | 连接层（authorizer） | 注释掉 `install_connection_guard` 里的 `conn.set_authorizer(...)` | 只有"连接层"两条用例变红，其余不变 | 2 failed / 15 passed；红的是 `test_dropping_a_guard_trigger_is_refused_on_the_store_connection`、`test_alter_table_rename_and_schema_writes_are_refused` |
   | 写前核查 | 删掉 `append_many()` 里的 `assert_guard_intact(self._conn)` | "别人拆了防线 ⇒ 下次追加 fail-closed"那条必红 | 1 failed / 16 passed；红的是 `test_appending_after_an_external_drop_fails_closed` |
   | 离线核查计入结论 | 把 `audit()` 的 `ok` 改回 `not violations`（防线不计入退出码） | 只有"防线被拆 ⇒ 退出码 1"的两条变红 | 2 failed / 15 passed；红的是 `test_verify_append_only_guard_detects_a_missing_trigger`、`test_audit_chain_reports_the_guard_and_its_exit_code` |

   三格都确认了"坏的正是那一条、别的还绿"——不然红的原因可能只是被无关用例带出来的。
   连线上的正对照见 claim `chain-mirror-clean-guard-ok` /
   `chain-mirror-tamper-breaks-the-guard`（真跑一遍 run → 干净副本防线 OK、被篡改的副本不 OK）。

   `mutation` 门禁的完整判据（六条红灯条件 + 未知状态 fail-closed）与**实跑过的三格
   退化注入记录**见 [`design/2026-09-18-mutation-segfault-investigation.md`](design/2026-09-18-mutation-segfault-investigation.md) §4.1；
   `tests/test_mutation_gate.py` 里每条条件都有一个"改坏必须变红"的用例，
   外加一格"状态与基线一致 ⇒ 退出 0"的正对照。
   门禁按**全状态记账**分三类（判定类 / 未覆盖类 / 无结论类），每次运行打印不可见空间的规模——
   修这条的背景是 2026-09-18 独立验证报告的 P0-1/P0-2（判据结构上不可能变红）。
4. **修改任何影响 `reports/*` 的代码 ⇒ 重新生成产物并提交**，且确认差异只有计时噪声。
5. **测试数量写进文档时**，同时写出口径（`pytest -o addopts= -p no:cacheprovider -q` 的输出）。

---

## 4. 禁止事项（自证 / 作弊清单）

以下行为一律视为"结论不成立"，评审可直接判 P0：

1. **禁止用 runtime 自己的日志当裁判。** 副作用次数只认外部账本（run 目录的 `world.db`，
   且**必须连 `-wal`/`-shm` 一起快照**，否则会把"效果已发生"读成 0）。
2. **禁止 mock 掉关键机制**：崩溃注入必须是真 SIGKILL 子进程；外部账本、append-only 触发器、
   TOCTOU 复核都不得被 stub 替代。
3. **禁止用 ground truth 喂被判定的机制**：审批门读的拒绝列表不能同时是红线指标的来源
   （历史 P0：那是同义反复，橡皮图章消融下原形毕露）。
4. **禁止跨系统比较携带系统名相关的随机性**：随机种子不得含 system 名（CRN 规则）。
5. **禁止拿 holdout 调参**：dev 上选点，holdout 只用于最终确认；holdout 被"看过"之后
   必须在文档里声明并重新冻结。
6. **禁止只报点估计**：单比例给 Wilson 区间；两路线差值给配对 bootstrap 区间与配对键；
   给出 MDE，小于 MDE 的差距必须写"不可区分"。
7. **禁止测试 agent 修改被测代码**：独立测试只读仓库，全部产物写 `/tmp`；
   要让测试通过而改代码 = 测试失效。
8. **禁止把"没测出错"表述成"不会出错"**：n=5 的 0/5 只能按 rule of three 报失败率上界。
   同理：fuzz 的 180 次注入全绿只把失败率上界压到约 2%，**不能**写成"覆盖了所有窗口"。
9. **禁止把租约读成并发承诺**：租约是"入场条件"，跨进程并发仍由外部账本裁决；
   一次双进程冒烟不是并发证据（`tests/test_lease.py` 的 docstring 写着这句话）。
10. **禁止让 artifact GC 进运行时**：回收只能由显式 CLI 做（默认 dry-run）；
    loop 内自动删等于凭空造一个崩溃窗口。

---

## 5. 数字与证据规范

* **每个进入文档的数字必须有：再生命令 + 分母口径 + 出处文件**。三者缺一不得写入文档。
  这条已由 CI 强制：claim 清单在 `reports/documented-facts.json`，对账入口
  `scripts/check_facts.py`（CI job `facts` 跑轻 claim 集，整量 claim 进 nightly）。
  **演进类数字（用例数等）一律引用 claim id，不在正文写绝对数。**
* 产物分两层：**汇总入库**（`reports/*.md`、`reports/*.json`）与**逐次明细不入库**
  （`reports/*_runs.json`，可由 `--runs-out` 再生）。
* 样本量口径要显式：W2–W4 每格 n=5（点估计，只用于语义验证）；W5–W7 每格 n=192（区间 + 配对差值）。
  **两套口径不得混引**。
* 崩溃矩阵的"注入次数"以 `crash_marker.json` 的存在为准；不注入的对照格要显式声明。

---

## 6. 独立测试 agent 作业流程（黑盒优先）

以 [`docs/tester-prompt.md`](tester-prompt.md) 为准，要点：

1. **先只读"规格"**：README、`docs/semantics.md`、`docs/HANDOFF.md`、全部源码与 `--help`。
2. **冻结结论**，然后才允许读"答案"：`docs/w2..w7`、`docs/posts/`、`reports/`、`tests/`、
   `docs/resume.md`、`docs/pitch-4min.md`。
3. 逐条对质，单独写"我的结论与作者声称的差异"一节（允许结论是"无差异"）。
4. 必测方向：可复现性 / 承诺逐条证伪 / 崩溃语义（外部账本）/ 审批语义 /
   评测口径绕过与退化注入 / 边界诚实性。
5. 缺陷分级：
   * **P0**：某条结论不成立（机制层面被证伪）；
   * **P1**：声称与实现不符，但结论仍成立；
   * **P2**：文档 / 口径与事实不符。
6. 报告结构参考 [`docs/independent-test-2026-09-16/report.md`](independent-test-2026-09-16/report.md)：
   方法与证据链 → 各方向结论表 → 缺陷列表（含最小复现）→ 无法判定项 →
   未覆盖风险 → 证据索引 → 与作者声称的差异。
7. 报告与复现脚本落 `docs/independent-test-<日期>/`，**最小复现命令必须可执行**。

---

## 7. 完成判定（绿灯标准）

一次改动可以合并，当且仅当：

```
[ ] 快速档全绿（pytest + ruff），退出码为 0
[ ] 语义相关改动的完整档全绿（矩阵逐格 as-predicted、门禁全过，条数见 claim `suite-gate-count`）
[ ] 新增/修复的每一条都有测试，且缺陷修复附了"修复前会怎样"的回归用例
[ ] 新门禁经过退化注入验证（改坏 → CI 变红 → 还原）
[ ] 文档中每个数字可被命令再生，所有引用处已同步
[ ] 边界章节存在，且写明了本结论不适用的范围
[ ] 独立测试（如适用）的报告已落库，P0 全部修复、P1 有处置结论
```
