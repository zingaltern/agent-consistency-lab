# 独立测试报告：agent-consistency-lab（外部测试者）

* 被测版本：`ac651feb2fd94c932aeae6f49fef1218cc57d5ae`（HEAD，2026-09-16）
* 测试时间：2026-09-16 22:03–22:25（UTC+8）
* 环境：macOS arm64，Python 3.14.6（仓库自带 `.venv`），pydantic 2.x / pytest 8 / langgraph 1.2.11
* 测试纪律：仓库只读（我的任何命令都没有写入仓库，所有产物写 `/tmp`）。**工作区在测试窗口内有本会话之外的活动**（`git status --porcelain` 快照见 `logs/git_status_*.txt`）——我的全部结论锚定在被测提交 `ac651fe` 的内容上，不受工作区重排影响；README 命令默认写 `reports/`，本报告一律改为 `--md-out/--json-out /tmp/...`。
* 黑盒顺序：先只读"规格"（README、`docs/semantics.md`、`docs/HANDOFF.md`、全部源码与 `--help`），完成本报告的 1–9 节之后才读 `docs/w2..w7`、`docs/posts/`、`docs/resume.md`、`reports/`、`tests/`，随后单独补"与作者声称的差异"一节。

**结论概览**：A–F 六个方向全部执行完；**未发现 P0**（没有一条机制性结论被证伪：崩溃窗口语义、outbox/探针、审批绑定、append-only、门禁抓退化、区间覆盖率全部实测成立，且 `reports/*.json` 与我的重跑逐字一致）。发现 **P1 ×1**（评测 CLI 的 `--systems` 子集直接崩溃而不是走门禁，含 `--operator lazy` 的子集组合）与 **P2 ×8**（README/HANDOFF 正文的多组数字停留在两轮审计修复之前：W4/W5/W6/W7 统计数字、崩溃次数、门禁条数、代码行数；以及 posts/pitch 的两处小数字）。读完作者材料后可以确认：机制结论无冲突，**差异集中在"审计修复没有回灌到最外层文档"**（详见 §12）。

---

## 1. 方法与证据链

* 每条结论都附可复现命令与输出要点；完整命令清单见 `commands.sh`（同目录），原始日志在 `logs/`，原始 JSON 在 `raw/`。
* **副作用次数一律以外部账本 `world.db`（`effects` 表）为准**，从不采信 runtime 自己的日志；读取账本时连 `-wal` 一起快照（进程被 SIGKILL 后已提交效果可能仍在 WAL 里——这一点本身是审计注意事项，见 §9）。
* 崩溃注入用真实 `CHAOS_WINDOWS` + 子进程 SIGKILL（退出码 −9/137），与项目自身一致。

---

## 2. A. 可复现性（README「如何复现全部结论」全部命令）

| 命令 | 退出码 | 耗时 | 结果 |
|---|---|---|---|
| `pytest -q -o addopts= -p no:cacheprovider` | 0 | 3.06 s | **203 passed**（与 HANDOFF 的"203 tests"一致） |
| `ruff check . --no-cache` | 0 | 0.03 s | All checks passed |
| `opsenv.suite --per-fault 8 --repeats 3 --gate` | 0 | 7.29 s | 1536 次运行，14/14 门禁通过（README 称"约 8 秒"） |
| `experiments.crash_matrix --repeats 5` | 0 | 28.95 s | 16/16 格 as-predicted（80 个 run 目录、70 次真 SIGKILL） |
| `experiments.context_cost` | 0 | 2.34 s | 5 个配置全部产出 |
| `experiments.context_sweep --repeats 2` | 0 | 38.49 s | 80 个完整周期（dev 72 + holdout 8） |
| README「单次崩溃可手工复现」四步 | 0,0,**−9**,0 | — | 崩溃阶段退出码 −9（shell 137），留下 `crash_marker.json` |
| `examples.ops_demo` / `harness.trace --run-dir` | 0 / 0 | 0.18/0.09 s | 均可跑通 |

**确定性**：suite 连跑三次（其中一次 `PYTHONHASHSEED=0`），统计量与运行明细完全一致——评测层是确定性的，因此下文出现的所有"数字不符"都不是随机性造成的。

**数字与文档不符之处**（明细见 §10 缺陷表）：

| README/HANDOFF 声称 | 本次实测（HEAD，README 原命令） |
|---|---|
| W4 命中率 57.5% → 0%、成本/调用 +105% | 57.4% → 0.0%、+104.6%（方向与量级一致） |
| W4 压缩行："7 次压缩、0 溢出、$0.01197、命中率 6.7%" | **3 次压缩**、0 溢出、**$0.01431/调用**、**命中率 35.0%** |
| W5 单次调用基线"正确率 93.2%"、"执行破坏性动作 20.3%（含 2 次新动作）" | single_shot competent **90.1%**、weak 红线率 **20.8%**、新动作 **3 次** |
| W5 "agent 路线正确率 86–90%" | harness 90.1%（competent）/61.5%（weak） |
| W6 配对：正确率 +0.005 CI[−0.099,+0.104]；红线 −0.203 CI[−0.260,−0.146] | 正确率 **0.000 [0.000, 0.000]**；红线 **−0.208 [−0.266,−0.151]** |
| W7 "压缩后 100% 完成但成本 +40%"；"阈值应贴近上限 0.85–0.95" | 100% ✓；成本 **+79%**（计入压缩桶）；选点规则实测选出 **0.70** |
| HANDOFF "代码量 11,939 行" | `wc -l` 六目录全部 `.py` = **12,351** 行（非空 10,595；W8 提交上也是 12,351） |
| 其余（203 tests、64 场景、6 窗口、16 格、outbox/探针/篡改矩阵、门禁 14 条、κ=1.0） | 全部复现 ✓ |

---

## 3. B. 承诺验证（对 `docs/semantics.md` 逐条证伪）

| 承诺 | 测试 | 结果 |
|---|---|---|
| 历史改写被物理禁止（§2.1） | 对 run 目录 `runtime.db` 的**副本**直接 `UPDATE events` / `DELETE FROM events` / 插入重复 `(branch_id,seq)` | 全部被拒：`events is append-only: UPDATE/DELETE is forbidden`（触发器）、`UNIQUE constraint failed: events.branch_id, events.seq` ✓ |
| —（边界） | 先 `DROP TRIGGER` 再 UPDATE | **成功**——"物理禁止"的强度= schema 级；直接对库文件动手且有写权限者可绕过（建议在文档里写明该边界） |
| —（边界） | 插入 `seq=max+1000` 的洞 | **成功**——`seq` 连续性是写入者保证、不是数据库约束（与实现一致，文档表述可更精确） |
| 状态不作为权威持久化（§1/§2.4） | 崩溃于 `post_record_pre_commit` 的副本上分别：篡改 checkpoint `state_json` 为"completed/step 99"；指向不存在的事件；删除全部 checkpoint 行；连 `checkpoint_writes` 一起删 | 四种破坏下 resume 全部 `completed`、副作用仍 **恰好 1 次**、结果不受 checkpoint 内容影响；指向不存在事件时按文档报 `checkpoint_mismatches=1` ✓ |
| 不重不漏（§1） | 132 个 run 目录外部 SQL 审计：seq 连续、event_id 唯一、`reduce_events` 违规为空 | 0 例异常 ✓ |
| 事件先于去重行（§2.5） | 182 个 run 目录交叉核对：不存在"executed/failed 工具行但无 tool_result 事件"，也无"有结果无调用" | 0 例 ✓ |
| 视图是纯函数（§2.6） | 同一日志两次 `ViewBuilder.build`（含压缩目录） | 指纹与块序列逐字相同；动态块只出现在 `tail` ✓ |
| INV-001..007（§5） | 构造 7 类违规日志喂 `reduce_events` | 每类都返回对应 Violation code；终态为吸收态；`fingerprint()` 折叠两次一致、且对事件顺序敏感 ✓ |
| 压缩原子与引用完整（§2.6） | 4 个含压缩的 run：重算 `compaction_id = cmp_+sha256(sorted(replaces))[:16]`、校验 artifact 文件内容 sha256==digest、检查无事件被两次替换 | 逐条一致 ✓ |
| 预算超限即硬停（§1） | `--budget-usd 0.0005` | 第二次模型调用后写 `budget_exceeded(fatal=true)`，run 终态 `failed` ✓ |
| 成本可事后复算（§1） | E1a/E1b 事件里的 token 与 `cost_usd` 与 `harness/cache.py` 价格表口径一致；压缩桶单列（审计后的口径） | 一致 ✓ |

---

## 4. C. 崩溃语义（外部账本裁决）

**扩展矩阵（本项目 crash_matrix 未覆盖的交叉，自写驱动）**：6 个窗口 × {全保护关（outbox/idem 都关）、全保护开} × 3 次，外加窗口 2 的 `--dedup on/off` 对照与 `after_resume` 连崩两次。判定只看 `world.db`：

| 案例 | 结果 |
|---|---|
| 窗口 2（`post_tool_effect_pre_record`）保护关 | 每次 **2 行同键 effects**（5/5 + 3/3 重复） |
| 窗口 2 保护开（outbox+probe+idem） | 恰好 1 行，探针确认 |
| 窗口 2 `--dedup on` vs `--dedup off`（其余保护关） | **两种都重复**——"runtime 侧去重开关在该窗口完全无效"成立 |
| 其余 5 个窗口（保护开/关） | 每次恰好 1 行，日志连续，无重复 |
| `after_resume` 连崩两次再恢复 | 恰好 1 行 |
| `during_compaction`（长任务） | 恰好 1 行；压缩引用完整、无重复替换、artifact 可读 |

**窗口 2 崩溃瞬间的快照证据**（outbox 关时无去重行、事件也未写）：`events` 只有 `tool_call` 没有 `tool_result`，`tool_calls` 表无该调用行，账本已 1 行 → 恢复时盲重跑 → 重复。outbox 开时同样位置留下 `pending` 意图行 → 恢复时探针确认（`reconciled=1, executed=0`）或转 `unknown`（`--probe off` 时恰好 1 行 unknown、0 重复）。**与文档描述逐条一致。**

---

## 5. D. 审批语义

| 路径 | 命令 | 结果 |
|---|---|---|
| 篡改（批准 A 执行 B） | `--tamper on --approve-mode approve` | `rejected / args_sha256_mismatch`，账本 **0 行**（执行前按实际参数二次校验是代码行为 ✓） |
| 改参 | `--approve-mode edit` | 原调用 `superseded_by_edit`；新调用 `tc_write_1__edit1`、**新幂等键**；账本 payload = 改后参数（`size: 32`）✓ |
| 拒绝 | `--approve-mode reject` | `rejected / approval_rejected`，账本 0 行 ✓ |
| 过期 | API 以 `ttl_seconds=-5` 审批 | `rejected / approval_expired`，账本 0 行 ✓ |
| 批准后崩溃（窗口 4） | `post_approval_pre_exec:1` | 恢复**不需要重新审批**（interrupt/resume 各 1 次），恰好 1 次副作用 ✓（"nonce 不是一次性闩锁"成立） |
| `scope=session` | 两次同参写调用 + 一次 session 审批 | 只发 1 次 interrupt，第二次调用按 session 复用放行，两次执行各自新键，账本 2 行不同键 ✓ |

---

## 6. E. 评测口径

* **绕过尝试**：`--per-fault 1 --repeats 1 --gate` → 退出码 1，`min_runs_per_cell`、配对 CI、dev/holdout 三条门禁变红（拦住了 ✓）。**空数据**：`--per-fault 0 --gate` → 退出码 2 并明说"否则没有任何场景，门禁会空真通过"（防空真通过 ✓；`--systems harness --gate` 也拦，但方式见下）。
* **少给系统**：`--systems harness --gate` → 退出码 1，但**不是门禁拦下而是未处理异常**（见缺陷 D-1）。
* **退化注入（/tmp 副本，未改原仓库）**：
  * 控制组（未改副本）：`--gate` 退出 0 ✓。
  * 退化 1「静默丢弃破坏性动作」（在 `OpsPolicyModel._conclude` 里把 forbidden 动作换成"不采取行动"）：退出码 **1**，恰好 `harness.gated[weak]==1.0`（实测 0.792）与 `harness.blocked[weak]>0`（实测 0.000）两条**对称自检门禁**变红，其余门禁保持绿——"指标完美、机制已死"确实被抓住 ✓。
  * 退化 2「注册表去掉 requires_approval」：退出码 1，5 条门禁变红（两条红线门禁、两条"真的拦过"门禁、配对 CI 门禁）✓。
* **区间覆盖率（合成已知真值，蒙特卡洛）**：Wilson 名义 95% —— 实测 94.1%–97.4%（小 n 偏保守，符合预期）；配对 bootstrap 名义 95% —— 实测 94.0%–97.5%（n=192，n_boot=500）；全同对 → 零宽区间 [0,0]（正确）。
* **门禁与数字**：我这次跑出的 14 条门禁、κ 表（workflow κ=1.0；agent 路线代理无方差、不适用）、dev/holdout 分段（90.3% vs 89.6%）与 README 的定性结论一致；W6 的具体配对数字不一致（见缺陷 D-2）。

---

## 7. F. 边界与诚实性

* `lease` / `state_update` 事件类型**确无生产者**（全源码 grep），与文档"已登记未实现"一致 ✓。
* 存储 pragma：`SqliteStore` = WAL + `synchronous=NORMAL`；`World`（外部账本）= WAL + `synchronous=FULL`。与 docs §3 的口径一致 ✓。（注意：`PRAGMA synchronous` 是**连接级**的，直接对库文件查值会得到连接的默认值，不代表写入进程的设置——这是给未来审计者的一个坑。）
* 未发现任何模块偷偷承诺 exactly-once：全源码中"恰好一次"只出现在 crash_matrix 的**预期模型注释**里；运行时代码的重复防护走"日志权威 + 幂等键 + 探针/unknown"路线 ✓。
* 单写者边界：双进程同时 resume 同一 run dir 的冒烟测试（文档明确此为"未定义行为"）结果未损坏（16 事件连续、1 次副作用、两进程均退出 0）——**一次冒烟不能当作并发安全的证据**，只是说明文档对它不作承诺是正确的。
* 文档卫生：允许读的文件里"生产级"只出现在"禁止使用该词"的声明句中 ✓；6 个窗口、16 格、64 场景、1536 次运行、203 测试均可复现 ✓；行数声明不可复现（见缺陷 D-6）。

---

## 8. 缺陷列表

> 严重度定义（按提示词）：P0=某条结论不成立；P1=声称与实现不符但结论仍成立；P2=文档/口径与事实不符。
> 全部命令均可在干净环境按 `commands.sh` 复现；输出要点见 `logs/` 同名日志。

### D-1 【P1】`opsenv.suite` 用 `--systems` 子集时直接崩溃，而不是走门禁

* **最小复现**：
  ```bash
  cd "$REPO"
  .venv/bin/python -m opsenv.suite --systems harness --gate ; echo "exit=$?"
  # → Traceback ... File ".../opsenv/suite.py", line 264, in findings
  #   AttributeError: 'NoneType' object has no attribute 'novel_red_line'   exit=1
  .venv/bin/python -m opsenv.suite --systems workflow,single_shot --gate ; echo "exit=$?"
  # → AttributeError: 'NoneType' object has no attribute 'avg_cost_usd'     exit=1
  .venv/bin/python -m opsenv.suite --systems workflow --gate ; echo "exit=$?"
  # → AttributeError: 'NoneType' object has no attribute 'novel_red_line'   exit=1
  ```
* **期望 vs 实际**：`--systems` 是公开 flag，门禁里专门有 `all_cells_present`（"8 格齐备"）来对"格子缺失"给出**可读的失败**；实际是在渲染门禁表之前就抛未处理异常，没有任何报告产出（`--json-out` 也不会写）。退出码恰好也是 1，所以**在 CI 里会被误读成"门禁拦下了"**。
* **建议**：`findings()` 及各渲染分支对 `cell(...) is None` 做保护（跳过相关句子或显示"n/a"）；或在 `main()` 里对子集构造做显式校验。

### D-2 【P2】README 的 W6 配对统计数字在当前 HEAD 不可复现

* **最小复现**：
  ```bash
  .venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate --json-out /tmp/eval.json
  python3 -c "import json;d=json.load(open('/tmp/eval.json'));print(d['statistics']['paired_harness_vs_single_shot'])"
  ```
* **期望**（README W6 条目）：正确率 `+0.005（95% CI [−0.099, +0.104]）`；红线 `−0.203（CI [−0.260, −0.146]）`。
* **实际**：正确率 `point 0.000, CI [0.000, 0.000], n=192`（384 个配对里 0 个不一致对）；红线 `point −0.2083, CI [−0.2656, −0.1510]`。三次重跑（含 `PYTHONHASHSEED=0`）完全一致 → 非随机性。
* **影响**：定性结论不变（"谁更准"不可区分、"谁更安全"显著）；但 README 的数字是**唯一的**数字来源，读者按 README 复现会得到不同的数。
* **建议**：用当前代码重新生成 README 的 W5–W7 数字（或注明生成条件/提交号）。

### D-3 【P2】README 的 W5 数字（93.2% / 20.3% / 2 次新动作）与实测不符

* **最小复现**：同 D-2 的 `--runs-out /tmp/runs.json`，按 `system=profile` 聚合、`novel_red_line` 计数。
* **期望**：single_shot "正确率 93.2%"、"20.3% 的场景执行破坏性动作（含 2 次静态拒绝列表之外的新动作）"；agent 路线"正确率 86–90%"。
* **实际**：single_shot competent **90.1%**、weak 红线率 **20.8%**、新动作 **3 次**（1 competent + 2 weak）；harness/langgraph competent **90.1%**。
* **建议**：同 D-2。

### D-4 【P2】README 的 W4 压缩行数字（7 次压缩 / $0.01197 / 6.7% 命中率）与实测不符

* **最小复现**：
  ```bash
  .venv/bin/python -m experiments.context_cost --json-out /tmp/cost.json
  python3 -c "import json;print([r for r in json.load(open('/tmp/cost.json')) if r['name'].startswith('E2b')])"
  ```
* **期望**（README W4 条目）：同一条长任务"能跑完（7 次压缩、0 溢出），成本/调用 $0.01197，命中率只有 6.7%"。
* **实际**：E2b（大结果内联+小窗口+有压缩）**3 次压缩**、0 溢出、**$0.014307/调用**、**命中率 35.0%**。另：W4 第一行"57.5%"实测为 **57.4%**（`8427/14686`）。
* **疑似原因**：压缩默认阈值的历史变更——当前 `CompactionPolicy.trigger_fraction=0.85`；README 的 7 次/$0.01197/6.7% 与 W7 扫描里 **0.30 档**（6.67 次、6.97% 命中）高度吻合。若属"W4 时代默认值"，应在 README 注明或更新。
* **建议**：更新 README，或注明"该数字对应 trigger=0.30 的配置"。

### D-5 【P2】README 的 W7 "+40%" 与选点区间 0.85–0.95 与实测不符

* **最小复现**：
  ```bash
  .venv/bin/python -m experiments.context_sweep --repeats 2 --json-out /tmp/sweep.json
  # 比较 dev off（$0.06608，完成 4/12）与 0.70/0.95 档的总成本（主桶+压缩桶）
  ```
* **期望**：'压缩后 100% 完成但成本 +40%'；'阈值应贴近上限（0.85–0.95）'。
* **实际**：完成率 100% ✓；总成本 **+79%**（$0.11834 vs $0.06608；0.95 档相同）。**主桶**成本 `$0.09273/$0.06608 = +40.3%`——与"+40%"吻合，疑似旧口径漏计压缩桶（`experiments/context_cost.py` 里的注释自称曾有"低估 36%"的审计问题）。选点：按"先完成率再成本"规则实测选出 **0.70**（$0.118337，比 0.85/0.95 低 0.09%，在噪声内），并非 0.85–0.95。
* **影响**：方向性结论（压缩买完成率不省钱、不要早压）不受影响且被强化；但数字与选点表述需要与代码输出对齐。
* **建议**：更新 README 的 "+40%" 与阈值区间措辞（或说明 0.70/0.85/0.95 在噪声内不可区分）。

### D-6 【P2】HANDOFF 的代码量数字（11,939 行）不可复现

* **最小复现**：
  ```bash
  find harness fakeworld opsenv experiments tests examples -name '*.py' -exec cat {} + | wc -l
  # → 12351（在 HEAD 与 W8 提交 9485b21 上相同）
  ```
* **实际**：12,351 行（含空行）；非空 10,595 行。两种口径都不等于 11,939。口径未注明，无法判定原意。
* **建议**：注明统计口径，或更新数字。

### D-7 【P2】"80 次真实 SIGKILL"是超计数（实际 70 次；README 对 W3 的"70 次"同样超计数）

* **最小复现**：
  ```bash
  .venv/bin/python -m experiments.crash_matrix --repeats 5 --workroot /tmp/matrix
  find /tmp/matrix -name crash_marker.json | wc -l        # → 70
  ```
* **期望**（README「已产出的实测结论」、HANDOFF §一、docs/resume.md 版本 B、docs/pitch-4min.md 0:40 段）："6 个命名崩溃窗口 × 16 格 × 5 次 = **80 次真实 SIGKILL** + 80 次恢复"；README 的 W3 条目写"70 次崩溃"。
* **实际**：16 格 × 5 = **80 个 run 目录**，但注入崩溃的只有 **14 格**（`(tamper)` 与 `(long-baseline)` 两个对照格按设计不注入），实测 `crash_marker.json` = **70** 个；W3 那轮同理是 70 个 run / **65 次**真 SIGKILL（`docs/w3-report.md` 自己已写明 65，README 未采纳）。
* **建议**：改为"80 格次运行、70 次真实 SIGKILL"；README 的 W3 条目改为 65。

### D-8 【P2】两处零散小数字

* `docs/posts/03-cache-accounting.md` §二："缓存写 token **1,969**" —— 实测 **2,003**（`docs/w4-report.md` §二 也写 2003，post 与自家报告不一致）。复现：`context_cost --json-out` 后读 E1a 的 `cache_write`。
* `docs/pitch-4min.md` "二类是制度化"段："**十条门禁**" —— 实测 `--gate` 输出 **14 条**（W6 报告以 8 行分组呈现，代码里是 14 条）。
* **建议**：随 D-2~D-5 一并更新；这些数字建议改为从 `reports/*.json` 生成。

---

## 9. 无法判定的项

1. **HANDOFF 行数 11,939 的原始统计方法**：常见口径（wc -l 含/不含空行、含/不含 tests）都算不出该值；只能报"不可复现"，无法判定原意。
2. **"九个只读审计代理、7 个 P0"**（pitch §3:30、HANDOFF §六）：过程声明，仓库里只能核到修复后的代码与回归测试，代理数量无法核验。
3. **langgraph 侧的崩溃一致性**：opsenv 的 langgraph 对照用 `MemorySaver`，没有崩溃注入路径；"两条 agent 路线差别在崩溃一致性"这一点我只能验证 harness 侧（本项目其余证据也只覆盖 fakeworld/harness），**langgraph 侧无法用本仓库工具判定**。
4. **"工具自述风险"层的对比**：只做到代码级核对（harness 注册表 `requires_approval=True`、langgraph 用图级 `interrupt_before`、single_shot 无门）；W5 报告 §四 的措辞与代码一致，但没有可执行的对照实验。
5. **掉电语义**：明确不承诺，无从用现有工具测试（`synchronous=NORMAL` 的边界与文档一致）。
6. **5 次重复的"0/5"类结论的失败率上界**：文档自己已按 rule of three 标注（约 60%），无异议；但任何"永不重复"式的强表述都超出数据（文档没有这么说）。

> 读完成"答案"材料后，原先挂起的两个问题已可判定：**W4 "−62%" 的比较基准**是"卸载 vs 大结果内联（截断期均值 $0.00753）"这一对（实测 −61.5%，post ③ 的同表语境与之吻合）；**W4 压缩行数字**的旧值来源已被 w4-report §二.5 自己解释为"每步压缩"退化（见 §12）。

## 10. 未被覆盖的风险（外部测试者视角）

1. **README 数字与代码漂移没有门禁保护**：本轮 8 个 P2 里 7 个是同一模式（两轮审计修复后，`reports/`、`w*-report` §八、posts、pitch 都更新了，README/HANDOFF 正文没有）。建议给"README 数字"做一条和门禁一样的自检（例如把关键数字写成脚本可再生的字段）。
2. **`--systems` 子集的崩溃会被 CI 误认成门禁失败**：任何想用子集做快速回归的人会拿到伪阳性。修 D-1 之前不宜在 CI 里用 `--systems`。
3. **评测层的崩溃语义没有被对照验证**：四系统对照里只有 harness 路线接入了真实崩溃恢复（fakeworld 场景）；"两条 agent 路线差别在崩溃一致性"目前是**设计论证**而非评测结果。若对外声称"四系统消融包含崩溃一致性"，建议补一条 langgraph 崩溃注入实验，或在文档里明确该结论只来自 fakeworld 单侧证据。
4. **外部审计读取 `world.db` 的隐蔽坑**：进程被 SIGKILL 后，已提交的效果可能还在 `world.db-wal`；只拷贝主库文件会把"效果已发生"读成 0（本报告 §2 的第一次 B2 结果就踩了这个坑）。建议在 README/报告中给审计者写明"账本要连同 -wal/-shm 一起读"。
5. **窗口 2 的重复在保护全关时必然发生**（设计如此）；但崩溃矩阵"其他窗口恰好一次"的结论只对 **单写者** 成立——并发 resume（文档已声明未定义）下无任何防护，这一风险未被任何实验覆盖。
6. **阈值选点对噪声敏感**：0.70/0.85/0.95 的差在 0.1% 量级，选点规则会随若干次重复抖动；holdout 只跑了 2 档（off + 选中档）。若要把"应贴近上限"当结论，需要更大 n 或说明三者不可区分。

---

## 11. 证据索引

* 命令与退出码/耗时：`/tmp/acl-audit/commands.jsonl`、`logs/*.log`
* 原始数据：`raw/A3_eval.json`（1536 次）、`raw/A3_runs.json`（逐次明细）、`raw/A4_crash_matrix*.{json,md}`、`raw/A5_context_cost.json`、`raw/A6_sweep.json|md|svg`、`raw/C1_extended_matrix.json`、`raw/wal-snapshots/`（崩溃瞬间的账本快照）
* 复现脚本：`scripts/`（b1 追加禁改、b2 checkpoint 非权威、b3 全量日志审计、b4/b7 视图纯函数与压缩完整性、b5 不变量、c1 扩展崩溃矩阵、d1–d6 审批、e4 区间覆盖率、runlog.py 记录器）
* 退化副本：`copy-degraded-silentdrop/`（静默丢弃）、`copy-degraded-nogate/`（去审批），补丁点与断言见 `commands.sh`

## 12. 我的结论与作者声称的差异

> 本节按提示词要求，在完成 §1–§11 并把结论冻结之后，再读了 `docs/w2..w7`、`docs/posts/`、`docs/resume.md`、`docs/pitch-4min.md`、`reports/`、`tests/`、`.github/workflows/ci.yml`，逐条对质。

### 12.1 先说一致的：机制层我一条都没能推翻

作者材料里的**方向性结论**，我用自己的独立方法（外部账本、扩展矩阵、/tmp 副本退化注入、合成真值覆盖）全部复现，且复现方式不共享作者的判定代码：

* 崩溃语义：窗口 2 无 outbox 重复 5/5、outbox+读回 0/5 且探针确认、无读回恰好 1 行 unknown、"runtime 去重在该窗口无效"；其余 5 个窗口恰好一次（我另做了"全保护开"的组合，作者矩阵没测）✓
* 审批：TOCTOU 5/5 拒绝且 0 副作用、改参换键换调用（账本落改后参数）、nonce 非闩锁、session scope 复用 ✓
* 日志/状态：append-only 触发器物理拒改、checkpoint 内容被篡改/删除都不影响恢复（作者 w4 审计自己也纠正过"恢复权威"的表述）✓
* 压缩原子与引用完整、视图纯函数、INV-001..007、预算硬停 ✓
* 评测：门禁能抓"静默丢弃"（我注入退化确认退出码 1 且恰是 gated/blocked 两条）、橡皮图章消融 4.2%/20.8%、rule_full 100%/$0.00394、三条取证路线配对 CI 恰好 [0,0]、区间覆盖率名义 95%、κ=1.0、MDE 9.2% ✓
* **`reports/*.json` 与我的重跑一致**：`w6_eval.json` 的 statistics/cells 完全相同（仅 `avg_wall_ms` 计时噪声）、`w7_sweep.json` 与 `w4_context_cost.json` 字节级相同、`w4_crash_matrix` 的 md 逐字相同（JSON 仅 note 措辞不同）。"入库报告可由命令再生"这条是成立的。
* W7 报告里"细胞级非单调"的具体反例（variant-1：0.50 压 4 次命中 8.40%、0.30 压 5 次命中 8.91%）我逐格复算，**完全一致** ✓
* `docs/pitch-4min.md` 引用的新数字（90.1%/61.5%、20.8%、+79%、[0,0]）与我实测一致；`docs/resume.md` 的"不能写的词"确实没写，"必须交代的边界"我逐条验证为真 ✓

### 12.2 再说差异：全部集中在 README/HANDOFF 的正文数字

**已读材料内部的规律很清楚**：`docs/w5-report.md` §八、`docs/w6-report.md` §八、`docs/w7-report.md`（审计修复后重算）与 `reports/*.json`、`docs/posts/`、`docs/pitch-4min.md` 使用的都是**修正后的数字**；而 **README 与 HANDOFF 的正文仍引用审计前的旧数字**，且 README 恰恰是"如何复现全部结论"的入口。

| 断言 | 出处（旧） | 我的实测 / 修正后出处 | 判定 |
|---|---|---|---|
| 崩溃矩阵 "80 次真实 SIGKILL" | README、HANDOFF §一、resume、pitch | **70**（14 个注入格 × 5；`(tamper)`/`(long-baseline)` 不注入） | 超计数（D-7） |
| W3 "70 次崩溃" | README | 70 run / **65** 次 SIGKILL（w3-report 自己已写 65） | 超计数（D-7） |
| W4 压缩行："7 次压缩、$0.01197、命中率 6.7%" | README | 3 次、$0.01431、35.0%（w4-report §二 已更新，§二.5 解释了旧值来自"每步压缩"退化） | 旧数字（D-4） |
| W4 "命中率 57.5%" | README | 57.4%（w4-report 也写 57.4%） | 旧数字（D-4） |
| W4 "缓存写 token 1,969" | posts/03 | 2,003（w4-report 也写 2003） | 旧数字（D-8） |
| W5 "单次调用正确率 93.2%、20.3%、2 次新动作、agent 86–90%" | README、HANDOFF §四 | 90.1%、20.8%、3 次、90.1%（w5-report §八 已更新） | 旧数字（D-3） |
| W6 "正确率 +0.005 [−0.099,+0.104]；红线 −0.203 [−0.260,−0.146]" | README、HANDOFF §四 | 0.000 [0,0]；−0.208 [−0.266,−0.151]（w6-report §八、reports、pitch 都是新值） | 旧数字（D-2） |
| W7 "成本 +40%" | README | +79%（w7-report、posts/03、resume、pitch 都是 +79%） | 旧数字（D-5） |
| W7 "阈值应贴近上限 0.85–0.95" | README | 选点规则选出 0.70；w7-report 自己写"0.70/0.85/0.95 在成本上不可区分，程序选出 0.70 纯属刀尖上的并列" | 旧措辞（D-5） |
| W7 "压缩次数与缓存命中率严格反向" | README | w7-report §四 已改为"聚合反向、细胞级非单调"（我复算了那个反例，属实） | 旧措辞（D-5） |
| "十条门禁" | pitch | 14 条 | 旧数字（D-8） |
| "代码量 11,939 行" | HANDOFF §一 | 12,351（含空行）/10,595（非空），W8 提交上同值 | 不可复现（D-6） |
| `--systems` 子集行为 | 隐含在 W6 §八"缺格即红（由前置门禁拦住）" | 实际是未处理异常（findings() 假设四系统齐备）；`--gate` 仍非零退出 | 实现缺陷（D-1，作者材料未提及） |

### 12.3 对差异的解释（读材料之后才能下的判断）

* 这不是"结论造假"，而是**一次审计修复没有回灌到最外层文档**：W4.5/W7.5 两次审计把成本口径（补压缩桶）、种子（CRN）、计数（65 次 SIGKILL）都修正了，`reports/`、`w*-report` 正文与 §八、posts、pitch 都跟上了；README（W8 定稿）与 HANDOFF 的正文没跟上。两轮审计各自纠正了同类问题（如 W3 的 65 次），说明作者知道这个风险，但**没有把"README 数字"纳入任何自动检查**——这正是我建议补的门禁（例如把关键数字写成可再生的字段）。
* D-1（`--systems` 崩溃）与以上"文档滞后"不同：它是一个**真实的实现缺陷**，而且与 W6 §八 声称的门禁设计（"缺格即红"）直接相关——缺格时 `rate()` 的 0.0 兜底有了，但 `findings()`/渲染层的 None 兜底没有做全。它不影响现有 CI（CI 不传 `--systems`），但会让任何想用子集做快速回归的人拿到伪阳性。

### 12.4 作者材料里我无法验证的部分

* "五路 + 四路、共九个只读审计代理、抓到 7 个 P0"（pitch §3:30）——过程声明，仓库里只有修复后的代码与回归测试（`tests/test_audit_regressions.py` 存在），代理数量无法核。
* `tests/`（G 阶段才读）里我抽查了与我的发现相关的部分：没有覆盖 `--systems` 子集的用例（与 D-1 吻合）；`test_stats_and_gates.py` 覆盖了对称自检门禁；`test_sweep.py` 用合成数据锁定了选点规则（含"off 更便宜但完成率不达标"的样例）。未发现与我的实测冲突的期望。
* `docs/resume.md` 的面试问答（如"$0.005–0.012 一次任务"、"步数 3.4"）与我实测的量级一致（$0.0029–$0.0143/调用、步数 3.38）。

### 12.5 我认为作者材料没写、但外部测试者应该报告的三件事

1. **D-1 的 `--systems` 崩溃**（上述）。
2. **账本读取的 WAL 陷阱**：SIGKILL 之后 `world.db` 的主文件可能不含最后一次已提交效果；审计者必须连 `-wal` 一起读（我第一次 B2 实验就被它咬过，见 `logs/B2_checkpoint.log` 的初版输出）。建议在"给外部测试人员"的材料里加一句。
3. **`PRAGMA synchronous` 是连接级的**：直接对库文件查 pragma 得到的是查询连接的默认值，不能用来证明写入进程的 durability 设置（我在 F1 首次测量时就得到误导性的 "2/FULL"）。
