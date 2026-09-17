# 架构与代码评审：W9 交付（A 包 + B 包，六个堆叠分支）

* 评审对象：`main`（`0ab04b3`）→ 交付 tip `docs/w9-backfill`（`49f9f51`）；
  六个堆叠分支 `feat/facts-gate` … `feat/otel-sweep-lease`
* 评审方式：**只读仓库**；所有实验在 `/tmp` 的克隆里做（`git clone` + `PYTHONPATH` 指向克隆，
  已确认 `harness.__file__` / `opsenv.__file__` 落在克隆里）；除本报告外，未改动仓库内任何代码/测试/文档
* 状态：本报告是评审产出，不是交付的一部分

---

## 0. 总体判断

**不推荐按现状合并。** 两个 P0 都在**门禁与文档**层，不在机制层：

1. **P0-1**：`scripts/mutation_check.py` 在 `mutmut run` 没有产出任何结果时**退出 0（绿）**，
   并且把基线里 625 条幸存变异报告成"已被杀死"。这直接违反 `AGENTS.md` 红线 5 与
   `docs/testing.md` §3.3 自称已做过的退化注入验证。
2. **P0-2**：`README.md:122` 与 `docs/HANDOFF.md:166` 引用的变异数字（1642 / 1000 / 628）
   与**同一次交付入库的** `reports/mutation_baseline.json`（1670 / 1031 / 625）矛盾，
   而 facts 门禁全绿——因为这两处引用没有被登记成 claim。这正是 A-M1 要消灭的那类缺陷，
   在 A-M1 的交付里复发了一次。

机制层（哈希链、schema v3 迁移、ArgPolicy 闸门、cassette 三模式、OTLP、artifact sweep、
租约、oracle、噪声人格）我逐项读过并做了注入实验：**结构与边界基本正确，可修可回滚**，
没有发现第二权威、没有发现被削弱的 append-only 触发器、没有发现自证指标。
修掉两个 P0 与三条阻塞性 P1（都是小改：一处返回值检查、一处承诺措辞与一处 docstring、一处 CLI 旗标语义）后可以合并。

| 维度 | 结论 | 一句话依据 |
|---|---|---|
| 语义承诺（§2.1） | **基本一致，4 处措辞需要收窄** | 逐句 diff 后与实现对照；见 §4 |
| INV-008 定义 ↔ 实现 | **等价** | 逐字段核对 `event_hash` 输入 + 往返实验（含 tuple/float/NaN/unicode/大整数） |
| 架构原则（§2.2） | 通过 | 无第二权威、append-only 触发器未削弱、随机源全部有显式 seed、runtime 仍只依赖 pydantic |
| 接口设计（§2.3） | 通过（两处措辞与一处死参数） | 见 P1-2、P2-15 |
| 错误处理与边界（§2.4） | **一处假绿、若干处失败语义可读性不足** | 见 P0-1、P2-4/5/6/7/8/9/10 |
| 测试质量（§2.5） | **主体扎实，三条用例/覆盖有欠缺** | 8 组注入实验：7 组变红、1 组（OTLP CLI 分支）仍绿；见 §3 与 P2-14/19/20 |
| 复杂度与文档同步（§2.6） | 通过（一处重复、一处注释漂移） | 见 P2-16/17/18/24 |
| 作者自陈 5 条（§2.7） | 4 条处置正确，1 条（第 3 条）需要补措辞 | 见 §6 |

### 复现与验证记录（我实际跑过的）

| # | 命令（在 `/tmp` 克隆里） | 结果 | 与交付报告是否一致 |
|---|---|---|---|
| 1 | `pytest -o addopts= -p no:cacheprovider -q` | `316 passed in 7.55s` | 一致 |
| 2 | `scripts/check_facts.py --run verify` | `claim 对账：27/27 通过`（6.2s） | 一致 |
| 3 | 改一条 claim 的 `value` 后重跑 #2 | `26/27`，退出 1，报出偏离 | 一致（门禁真的会红） |
| 4 | 盲掉 `opsenv/oracle.check_effect_accounting` 后跑 `chaos_fuzz --repeats 2` | 退出 1，敏感性自检失败 | 一致（自检真的会红） |
| 5 | 同上 + `--skip-sensitivity` | **退出 0，报告打印"通过——实测 findings=[]"` | **不一致**，见 P1-3 |
| 6 | `mutmut run` 换成"立即失败"后跑 `mutation_check.py` | **退出 0**，"没有新增幸存变异" | **不一致**，见 P0-1 |
| 7 | 用 `main` 的代码造 v2 run 目录 → 新代码 `setup()` | v2→v3 迁移成功，13 事件链可校验，`resume` 能继续写 | 一致（旧 run 目录仍可读） |
| 8 | 逐条注入：ArgPolicy / verify_chain / sweep / lease / oracle / 噪声 / 链挂接 | 对应用例文件分别 6/3/1/5/1/4/5 条变红 | 机制侧用例有效 |
| 9 | 注入：`harness/trace.py` 的 `--otlp-endpoint` 分支置为不可达 | `tests/test_otel.py` 5/5 **仍绿** | 见 P2-14 |
| 10 | 破坏 `runtime.db` 后调 `audit_run` | `check_no_tamper` → `inv_log_readable`；另两个 checker 与 `audit_run` **抛异常** | 见 P2-4 |

**没有 PR 可评**：`gh pr list --state all` 为空，六个分支只在远端分支上，
因此本报告没有用 `gh pr review` 张贴（任务要求"若已开 PR 才贴"）。

---

## 1. 逐分支结论

| 分支 | 需求 | 结论 | 阻塞项 |
|---|---|---|---|
| `feat/facts-gate` | A-M1 claim 再生门禁 | **通过**（附一条结构性建议 P1-4） | 无（P0-2 的落点在下游 `docs/w9-backfill`） |
| `feat/noisy-reasoner` | A-M2 噪声人格 + 变异常设化 | **阻塞** | P0-1 |
| `feat/chaos-fuzz-probes` | A-M3 fuzz + 谱系探测器 | **阻塞（小改）** | P1-3 |
| `feat/record-replay-model` | B-M1 三模式模型接入 | **通过，无需修改** | 无（仅 P2-12/13） |
| `feat/arg-policy-hash-chain` | B-M2 参数级审批 + 哈希链 | **通过，无需修改** | 无（仅 P2-1/2/3/18，其中链的挂接回退建议顺手改） |
| `feat/otel-sweep-lease` | B-M3 OTLP + GC + 租约 | **阻塞（小改）** | P1-1、P1-2 |
| `docs/w9-backfill`（交付 tip，非六个之一） | 全局回灌 | **阻塞** | P0-2 |

分支是**堆叠**的（`docs/open-questions-answered` → `feat/facts-gate` → … → `feat/otel-sweep-lease`
→ `docs/w9-backfill`），必须**按此顺序**合并；跨分支乱序合并会产生冲突。
A §4.1 的裁决（新增独立 flag `--kill-after-ms`、`injection_kind` 只增不改）**已回写**
B 文档 §7，且实现与裁决一致（`harness/chaos.py` 的 marker 字段只增不改）——这条跨文档一致性成立。

---

## 2. 分级问题清单

### P0（不修不能合）

#### P0-1｜变异门禁在"什么都没跑出来"时是绿的

* **位置**：`scripts/mutation_check.py:49-69`（`run_mutmut` 丢弃 `mutmut run` 的退出码）、
  `:96-109`（`mutation_summary` 对空 bucket 给出全 0）、`:205-215`（没有新增幸存就 `return 0`）。
* **机制**：`collect_status()` 读 `mutmut results --all=true`；该命令在**没有结果时退出 0 且无输出**
  （实测：空缓存下 `python -m mutmut results --all=true` → 退出 0、stdout 为空）。
  于是 `survived=[]` ⇒ `new_survivors=[]` ⇒ 打印"没有新增幸存变异：测试盲区没有扩大" ⇒ **退出 0**。
* **注入验证（我在克隆里做的）**：把 `_mutmut_argv` 改成"立即 `sys.exit(3)`"，其余全真：

  ```
  [mutation] 运行: … -c "import sys; sys.exit(3)" --max-children 4（上限 120s）
  [mutation] killed=0 survived=0 no_tests=0 survivor_rate=0.000
  [mutation] 有 625 条基线里的幸存变异已被杀死（好事，不判失败）
  [mutation] 没有新增幸存变异：测试盲区没有扩大
  EXIT=0
  ```

  注意最后两行：**基线被整体判成"已修复"，门禁自认为是绿的**。
* **为什么是 P0**：`AGENTS.md` 红线 5 要求"每条新门禁须做退化注入验证（把机制改坏 → CI 必须变红）"；
  `docs/testing.md:72` 把 `mutation` 的退化验证写成"从基线里删掉一条幸存变异 → 退出 1"——
  那条路径**默认 mutmut 真的跑起来了**。而"没跑起来"这一支（配置错、OOM、collect error、
  缓存被清）恰好是最需要报红的一支：nightly 会在什么都没测的情况下绿。
  另外 `collect_status` 读的是 mutmut 的缓存结果，`mutmut run` 失败而缓存*残留*时，
  还可能读到上一轮的结果（CI 上是干净 job，本地上有风险）。
* **建议**：
  1. `run_mutmut` 接收并检查返回值，非 0 即 `SystemExit`；
  2. `mutation_summary` 的 `total == 0` 视为失败（"跑不出变异体"不是"没有盲区"）；
  3. 补一条回归用例：不需要真跑 mutmut，针对 `mutation_summary({})` 与"结果为空"的判定路径断言退出 1，
     并在 `docs/testing.md` §3.3 的表格里加这一行退化注入。

#### P0-2｜文档里的变异数字与入库基线矛盾，且 facts 门禁没覆盖它

* **位置**：`README.md:122`、`docs/HANDOFF.md:166`。
* **事实**：
  * `README.md:122`："**变异测试常设化**：1642 个变异体 / 1000 killed / 628 survived（基线入库，防"盲区扩大"）"
  * `docs/HANDOFF.md:166`："变异基线 628 幸存/1642"
  * 入库基线 `reports/mutation_baseline.json`：`{"killed": 1031, "survived": 625, "no_tests": 14, "total": 1670}`，
    `survivor_rate 0.3774`（`python -c "import json; print(json.load(open(...))['counts'])"`）
  * 同一交付的 `docs/design/2026-09-17-delivery-report.md:19` 写的是 1670 / 625（与 JSON 一致）
* **成因（有 git 证据）**：`git show 29905af:reports/mutation_baseline.json` = `{1000, 628, 1642, 0.3857}`；
  `git log --name-status -- reports/mutation_baseline.json` 显示该文件在 **`49f9f51`（回灌提交）被 M**
  ——即基线在写 README/HANDOFF 的同一次提交里被重算成 1670/1031/625（B 包改了 `harness/loop.py`，
  变异体总数随源码变化），而文档沿用旧数。
  628+1000=1628≠1642，说明旧数连自洽性都不成立。
* **为什么门禁没红**：claim `mutation-survivors` 的 `docs` 字段是
  `["reports/mutation_baseline.json", "docs/testing.md:§3.3"]`——**README/HANDOFF 不在引用位置里**，
  而且 `docs` 字段本身不参与机器对账。我在克隆里跑 `--run verify` 得到 27/27 通过。
* **建议**：改正两处数字（或重算基线后同步），把 README/HANDOFF 补进这两条 claim 的 `docs`；
  见 P1-4 的结构性收口。

### P1（本分支合并前应修）

#### P1-1｜`semantics.md` §3 的租约承诺没有"无生产接入点"这个限定

* **位置**：`docs/semantics.md:202-207`（"持有有效租约的进程可以写；未持有 / 已过期 / 被别人持有 ⇒ **可读失败**（绝不静默放行）"）。
* **证据**：全仓 grep `SingleWriterLease|guarded_write|lease.require|fold_lease` 的调用点只有
  `harness/lease.py` 自身与 `tests/test_lease.py`；`experiments/worker.py`、`harness/loop.py`、
  `SqliteStore.append` 都不调用 `require()`。也就是说 runtime 的写路径**完全没有接入**租约，
  双进程写同一 run 今天仍然不受任何检查。
* **判断**：`harness/lease.py` 的模块 docstring 是诚实的（"只回答当前进程有没有写权限"、
  "**不做**并发承诺"），README/HANDOFF 也写了"不是并发承诺"。但 `semantics.md` 是承诺清单，
  那三行读起来像"runtime 会拦住未持有租约的写"。**这是措辞问题，不是实现缺陷**——
  但承诺清单里不能留这种可被误读的句子。
* **建议**：在 §3 那一段补一句限定，例如"本实现提供的是**校验入口**（`require`/`guarded_write`）；
  runtime 的写路径尚未接入它，因此'未持有 ⇒ 可读失败'只在调用方显式使用时成立；
  下一波接入写路径时需要重新过语义文档"。

#### P1-2｜`sweep` 自称"引用枚举全库扫描"，实现只扫一个 run 目录（删除路径）

* **位置**：`harness/artifacts.py:183`（docstring："**引用枚举全库扫描**……不限于当前 run 目录"）、
  `:116`（"全量收集**引用来源**（对抗审查第 21 条：**不许只扫当前 run**）"）↔
  `:154-168`（实现：只打开 `run_dir / "runtime.db"` 的三张表）。
* **风险**：artifact 是内容寻址的，`--artifacts-root` 明确允许指向共享目录
  （`harness/artifacts.py:213-221`）。`python -m harness.artifacts --run-dir A --artifacts-root SHARED --apply`
  会删除**只被 run B 引用**的对象——docstring 承诺的正是"不会发生这件事"。
* **同一路径的第二处**：`:135-138` 的 `_harvest` 对 `(TypeError, ValueError)` 静默 `return 0`：
  在校举"哪些对象**不能删**"时吞掉解析失败，等于把损坏 payload 里的引用当作"没有引用"。
  dry-run 默认与 `--apply` 显式开关减轻了后果，但这是**删除安全**的判定输入。
* **建议**（任选其一，第一项最省）：
  1. 把 docstring 改成事实（"只扫 `--run-dir`；共享 artifacts_root 需自行保证"），并在
     `--apply` 且 `artifacts_root != run_dir/"artifacts"` 时拒绝执行（或要求 `--force`）；
  2. 或者让 `referenced_digests` 接受一个 run 根目录集合，真正做全量扫描。
  另建议把"解析失败的 payload 数"计进 `SweepReport`（现状 `scanned_sources` 只报来源种类）。

#### P1-3｜`--skip-sensitivity` 把"没有证明"写成"通过"

* **位置**：`experiments/chaos_fuzz.py:443-447`（`{"sensitive": True, "findings": [], "note": "已跳过"}`）、
  `:468-472`（verdict 只要求 `sensitivity.get("sensitive")`）。
* **注入验证**：先盲掉 `opsenv/oracle.check_effect_accounting`（让它恒返回空 findings），
  跑 `--repeats 1`：

  | 命令 | 退出码 | 报告文本 |
  |---|---|---|
  | 默认 | **1** | 敏感性自检**失败**——实测 findings=[] |
  | 加 `--skip-sensitivity` | **0** | 敏感性自检**通过**——实测 findings=[] |

  第二行是自相矛盾的输出：一个"通过"的自检，实测却是 0 条 findings。
* **危害面**：`--skip-sensitivity` 目前没有被 CI/claim/文档使用（grep 只有脚本自身），
  所以不是当前流水线的洞；但报告里那句话正被 A §4 R-A2 的验收要求（"报不出来说明 oracle 不敏感，
  验收不通过"）当作通过证据，而它恰恰可以被旗标伪造成通过。
  作者自陈第 2 条（窗口 2 的微秒级缝隙由确定性注入证明）**把全部重量压在这个自检上**，
  所以这条比一般旗标更值得修。
* **建议**：`--skip-sensitivity` 时把 `sensitive` 置为 `False` 并在报告写"**未执行**（不构成证明）"，
  让 verdict 变红；或者干脆删掉这个旗标（没有使用者的"关掉安全阀"开关）。

#### P1-4｜claim 覆盖是人工登记，门禁只保护"登记过的数字"（结构性）

* **位置**：`reports/documented-facts.json`（`docs` 字段是自由文本，不参与对账）、
  `scripts/check_facts.py:53-91`（`load_claims` 只校验 claim 自身字段自洽）。
* **证据**：P0-2 的 README/HANDOFF 数字与 JSON 矛盾而门禁全绿；另外
  `docs/HANDOFF.md:7` 一边写"测试 316 全绿（**不手写**）"一边手写了 316——
  这处今天是对的，明天会漂，同样无人守。
* **判断**：**不阻塞合并**（这是增强），但它决定了"历史 P2 里 7/8 同源"那类缺陷会不会第四次复发。
* **建议**（按性价比排序）：
  1. 至少让 `check_facts.py` 校验 `docs` 里每个引用位置**存在**（文件存在 + 段落锚点可匹配），
     引用位置写错就报红——这条能挡住"数字改了对账位置没跟着改"；
  2. 进阶：对 `README.md` 的"已产出的实测结论"一节做数值字面量扫描，列出**没有被任何 claim 覆盖**
     的数字（先做成报告/警告，再考虑做门禁）；
  3. 把 README/HANDOFF 里"手写的当前值"改成直接引用 claim id（HANDOFF 那句自相矛盾的话尤其要改）。

### P2（可跟进；每条都是 `file:line` + 建议）

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `harness/store/sqlite_store.py:295` | 本分支最后一条事件的 `event_hash` 为空串时，新事件**静默挂到 genesis/fork 点**而不是报错，链在无人察觉下被延长 | 这里改为 `raise StoreError`（"尾行没有哈希，库未迁移或被改过"），把发现时机从"下次离线校验"提前到"写入时" |
| P2-2 | `harness/store/schema.py:206` | docstring 引用测试名 `tests/test_hash_chain.py::test_migration_recreates_the_append_only_triggers`，实际名是 `test_migration_backfills_the_chain_and_recreates_triggers`（`docs/semantics.md:105` 写对了） | 改正 docstring |
| P2-3 | `harness/store/schema.py:242-244`、`:225-236` | 迁移的**分叉分支**路径（fork 点哈希未计算 ⇒ `RuntimeError`；谱系环 ⇒ `RuntimeError`）没有用例覆盖；`tests/test_hash_chain.py:299` 只覆盖单分支旧库（其 `V2_DDL` 也是手抄的） | 补一条"旧库含分叉分支"的迁移用例（可复用 `test_forked_branch_links_to_the_fork_event` 的构造方式） |
| P2-4 | `opsenv/oracle.py:186-231`、`:234-284` | 三个 checker 对"库读不出来"的处理不一致：`check_no_tamper` → `inv_log_readable`；`check_closed_calls` / `check_effect_accounting` 的查询抛 `sqlite3.DatabaseError`，于是 `audit_run` 直接抛异常（实测：打烂 `runtime.db` 后 `audit_run` → `RAISED DatabaseError: database disk image is malformed`） | 抽一个"读不出来 ⇒ `inv_log_readable`"的统一包装；`tests/test_chaos_fuzz.py:165` 的用例名承诺了"读不出来是发现不是静默通过"，但只测了第一个 checker，顺带补另两个 |
| P2-5 | `opsenv/oracle.py:95-96`、`:244-245` | `world.db` 不存在 = 空账本，两者不可区分，且**不产生任何 finding**（`facts` 全是 0） | 显式记 `ledger_present: false` 并在缺失时给一条 finding（"裁判缺席不等于没有副作用"） |
| P2-6 | `opsenv/oracle.py:259-271` | `unknown` 这条"免罪通道"由**被审判的 runtime 自己**写入，且无上界：把所有键标成 unknown 就能豁免全部重复（`inv_effect_accounting` 只查"有没有 unknown"） | 按 W2 已声明的语义加上界（每键 ≤1 行 unknown），或把 unknown 行数计进 `facts` 供报告复算 |
| P2-7 | `experiments/chaos_fuzz.py:243`（`:342` 只用于报表） | `killed = exit_code != 0` 是**推断**：任何非零退出（argparse 报错、Python 异常）都算"注入成功"；`crash_marker.json` 的 `injection_kind=="time_hit"` 没有参与判定 | 让 verdict 依赖 marker 证据（或至少统计 `killed_without_marker` 并在 >0 时报红）。注：`reports/chaos_fuzz_report.md` 的 30/30 marker 列说明**这一轮结论本身是成立的**，问题在判定代码没有把这份证据变成条件 |
| P2-8 | `scripts/probe_sigterm.py:93`、`:139-144` | `ok` 不含 `sigterm_sent`：进程若在延迟前就退出，探测器**没发出过 SIGTERM 也会通过**，并照样打印"运行时未注册 SIGTERM 处理器"的结论 | `ok = ok and sent and exit_code != 0` |
| P2-9 | `scripts/probe_tornwrite.py:167-182` | SIGSTOP/SIGCONT 循环没有 `try/finally`：`_ledger_rows` 抛错（`send_signal` 与 `poll()` 之间也有竞态）会把子进程**留在冻结态**；`wait(timeout=120)` 超时同样没有 kill | `try/finally` 里 best-effort `SIGCONT` + `kill()`；`terminate()` 兜底 |
| P2-10 | `scripts/check_facts.py:158-164` | 没有 `except subprocess.TimeoutExpired`：某条 claim 超时会以 traceback 中止整轮（退出码非零所以不会假绿，但拿不到逐条报表） | 捕获后按"该条失败"记入 `record`（与崩溃路径同样的可读失败） |
| P2-11 | `scripts/count_tests.py:28-50` | 记录了 `proc.returncode` 却从不检查：collect error（pytest 退出 2）仍然打印一个偏小的计数并返回 0 | 非 0 即失败（该值会漂成 claim 偏差，属于 P1-4 的同一类） |
| P2-12 | `harness/llm.py:380-384` | replay 带预算时成本按**当前**价目表重算，而录制里的 `entry.cost_usd` / `meta.price_version` 从不交叉校验（无预算路径 `:384` 反而用了 `entry.cost_usd`；`entry.cost_usd or …` 还会把"确实 0 成本"当成缺值） | 价目表版本不一致时给一条 replay warning（与 `:343-352` 的提示词指纹告警同级别），或用 `entry.cost_usd` 记账；`semantics.md` §2.7.2 的"成本不重算"要按实际口径改写（见 §4 第 3 条） |
| P2-13 | `scripts/replay_consistency.py:119-122`、`:132-139`、`:32-52` | ① `outcome_resume.json` 缺失时 `outcome={}`，而 `_compare` 用 `.get` 比较两侧——**两侧都缺会判为一致**；② 对外公布的 `WHITELIST` 不是比较用的键表（比较里有 `outcome.input_tokens`，白名单里没有；`events.*` 三项其实来自 `tokens` 列表构造） | 在 `run_consistency` 里断言 outcome 文件存在且非空；把白名单与比较键表合并成一个常量，避免"公布的口径"与"执行的口径"漂移 |
| P2-14 | `harness/trace.py:419`（CLI 分支）、`tests/test_otel.py`（5 条） | **没有任何用例走 CLI 的 `--otlp-endpoint`**：把该分支改成不可达，`test_otel.py` 仍 5/5 绿（实测）。而这正是 R-B4"显式启用、默认不变"的那个开关（默认那一半测到了，启用那一半只在函数级测到） | 补一条子进程用例：`--otlp-endpoint http://127.0.0.1:<关闭端口> --otlp-json`，断言可读失败与非零退出（不需要网络） |
| P2-15 | `harness/otel.py:90`、`:67`/`:80`、`:57` | `export_otlp` 的 `timeout` 参数从未使用；`build_sdk_spans` 返回的三元组里 `context` 恒为 `None`；`_load_sdk` 返回的 `SimpleSpanProcessor` 调用方不用 | 删死参数/死返回值，或写明"预留给后续批处理" |
| P2-16 | `scripts/mutation_check.py:38` | `MUTMUT` 常量是死的（执行走 `python -m mutmut`） | 删除或用它替换 `sys.executable -m mutmut` |
| P2-17 | `harness/audit_chain.py:36`、`opsenv/oracle.py:72`、`scripts/probe_tornwrite.py:95-99` | "-wal/-shm 一起快照"这条纪律现在有**三份实现**（harness / opsenv / scripts 各一） | 抽一个公共 helper（放 `harness/store/` 或独立 util），三处共用；这条纪律一旦在某一份里漂移，判定就会反转 |
| P2-18 | `harness/audit_chain.py:84-89` | 对未迁移的 v2 库，CLI 退出码正确（2），但信息是 `库无法读取：OperationalError: no such column: prev_hash`（在 main 造的 v2 run 目录上实测） | 在信息里点明常见原因（"v2 库：先跑一次 `store.setup()` 迁移，或确认跑的是哪个版本"） |
| P2-19 | `tests/test_lease.py:185` | `assert any("refused" in …) or len(outputs) == 2` **恒真**（`outputs` 由 2 元素循环构造），上一行 `all(item["outcome"] …)` 也恒真（内联脚本只输出 `"wrote"` / `"refused:…"` 两种非空串）。把 `lease.check` 改成恒通过后，这条用例**仍然通过**（实测：该文件 5 条红、这条绿） | 要么断言"两个进程的结果中至少一个是 refused"（去掉 `or len(outputs)==2`），要么把用例名从"records the actual behaviour"改成它真正断言的东西（seq 连续） |
| P2-20 | `tests/test_noisy_reasoner.py:193` | `test_both_flavor_mixes_the_two_forms_roughly_evenly` 直接调 stdlib `random.Random(...).choice([...])` 计数，**没有调用任何生产代码**——它复刻了 `opsenv/policy.py:101-102` 的那一行；生产代码改混合逻辑它不会红（同文件的 `:210` 才真正跑 `run_suite`） | 改成通过 `perturb_diagnosis`/`diagnose` 统计产生的 flavor（或直接删掉，`:210` 已覆盖） |
| P2-21 | `harness/cassette.py:105-115`、`harness/llm.py:151-158` | 归属为**权威计费数据**的 canonical 四段，缺键时静默按 0 计（`int(raw.get("prompt_tokens", 0) or 0)`）；供应商字段改名会变成"这次调用不要钱"，且没有任何告警 | 识别到形态（OpenAI/Anthropic）但缺关键段时给 warning（`replay_warnings` 已有现成通道） |
| P2-22 | `harness/artifacts.py:77` | `orphan_count` 已无生产调用者（只剩 `tests/test_w4_components.py`），`sweep` 取代了它 | 并进 `SweepReport` 或标注保留原因 |
| P2-23 | `docs/semantics.md:1` | 文件头仍写"运行时语义（v1，W1 冻结）"，但本轮改了 §2.4.1/§2.6/§2.7/§3/§4.0/§7（新增 1 条不变量 + 4 段承诺） | 加版本/变更记录行（谁在哪个里程碑改了什么），否则"唯一口径来源"没有可追溯的版本面 |
| P2-24 | `.github/workflows/nightly.yml:50`、`:74` | 注释说"15 分钟预算写死在脚本参数里"，实际 flag 是 `--timeout 1320`（22 分钟）；步骤名说"30 seeds x 3 combos x 2 phases"，代码是 6 个派生 seed × 30 次（总数 180 一致，说法不一致） | 改注释；顺带把 `experiments/chaos_fuzz.py:408,441` 的 `assert` 换成显式 `raise`（`python -O` 下 assert 会被剥掉，而同仓其它地方用异常） |
| P2-25 | `harness/lease.py:112` | `assert check.state is not None` 在 `-O` 下被剥掉（逻辑上 ok 蕴含 state 非空，仅是风格） | `if check.state is None: raise LeaseError(...)` 或加 `# noqa` 说明 |
| P2-26 | `experiments/chaos_fuzz.py:443`（`--skip-sensitivity` 的 note） | 见 P1-3；此处只登记"报告文本与 verdict 的可读一致性"这一项 | 随 P1-3 一起改 |

---

## 3. 测试质量（抽查 ≥15 条：谁在红、谁会假绿）

我做了两轮：**读**（11 个新测试文件逐个过，共 103 个测试函数 / 108 个用例）与
**注入**（把生产机制改坏，看对应用例是否变红）。结果：

| 注入的退化 | 对应的测试文件 | 结果 |
|---|---|---|
| `ArgPolicy.evaluate` 恒放行 | `tests/test_arg_policy.py` | 6 红 / 12 |
| `verify_chain` 不再重算哈希 | `tests/test_hash_chain.py` | 3 红 / 11 |
| `sweep(dry_run=False)` 不删任何东西 | `tests/test_artifact_sweep.py` | 1 红 / 5 |
| `lease.check` 恒通过 | `tests/test_lease.py` | 5 红 / 9（但双进程冒烟那条**没红**，见 P2-19） |
| `oracle.check_effect_accounting` 盲掉 | `tests/test_chaos_fuzz.py` | 1 红 / 11（另一路证据：整跑 fuzz → 退出 1） |
| `diagnose` 忽略 `noise_rng` | `tests/test_noisy_reasoner.py` | 4 红 / 15 |
| `_prev_hash_locked` 去掉"尾行哈希"分支 | `tests/test_hash_chain.py` | 5 红 / 11 |
| `harness/trace.py` 的 `--otlp-endpoint` 分支不可达 | `tests/test_otel.py` | **0 红 / 5**（见 P2-14） |

**明确点名的问题用例**（3 条）：

* `tests/test_lease.py:185`（P2-19）：`or len(outputs) == 2` 恒真 ⇒ "另一个进程拿到的是可读拒绝"
  这一结论**没有被断言**。交付报告把它列为"含双进程冒烟"的证据，这一条需要改。
* `tests/test_noisy_reasoner.py:193`（P2-20）：测的是 stdlib 的 `random.choice` 分布，
  与被测实现无因果关系。
* `tests/test_chaos_fuzz.py:165`（P2-4）：用例名承诺"读不出来是发现不是静默通过"，
  实际只覆盖 `check_no_tamper` 一个 checker，另两个 checker 的同场景会抛异常。

**没有发现的问题**（我逐条读过的，作为"为什么没找到问题"的交代）：

* 崩溃注入与外部账本的纪律**没有被 mock 破坏**：`test_replay_model.py` 的 `_FakeView/_StaticTransport/_ScriptedModel`
  替换的是**视图接口与外部供应商**，不是崩溃注入/账本/审批门；`test_arg_policy.py` 的
  `_loop`/`stub fn` 用的是生产的 `ScriptedLLMClient` + 真实 `Loop`/`World`（断言落在 `world` 的行数与
  `tool_calls` 表的 `pending` 上）；`test_artifact_sweep.py:140` 的 `fn=lambda` 只是构造渲染输入。
* 三条新门禁的**退化注入**：`facts` 有三条错误注入（改 value / 容差收窄 / 清空 `source_path`）且
  断言退出码与文本，我自己又对真 claim 文件做了一遍；`chaos-fuzz` 的敏感性自检有效（实测红）；
  `mutation` 的那一条**不成立**（P0-1）。
* 哈希链测试是本次交付里质量最高的部分：mirror test 真的 `DROP TRIGGER` 后改/删一行再校验，
  断言 `first_break.seq == 3`；迁移用例用**手写的 v2 DDL**（而不是当前 DDL）造旧库，并断言
  "触发器回来了且真的拒绝 UPDATE/DELETE"。我又用 main 的代码造了**真的 v2 run 目录**做端到端验证。

---

## 4. 承诺变更清单（`docs/semantics.md` 逐段）

| # | 变更 | 定性 | 实现是否一致 | 是否建议所有者拍板 |
|---|---|---|---|---|
| 1 | §2.4.1（`:78`）新增 INV-008 事件哈希链 | **扩大**（新增一条不变量 + 新增"违规"的定义） | **一致**。`record_bytes = json.dumps(record, sort_keys=True, ensure_ascii=False)`，输入字段 = `CHAIN_FIELDS`（不含 `trace_id/span_id/prev_hash/event_hash`）；写入用同一函数（`sqlite_store.py:234-235`）；genesis/分叉挂接规则与 `_prev_hash_locked` 一致；`audit_chain` 退出码 0/1/2 实测正确 | **建议**。理由：① 它改变了"什么算日志被篡改"的对外定义（第三方拿着副本就能判）；② 字节口径是**跨语言兼容面**（只对 CPython 的 `json.dumps` 浮点/转义成立）——建议补一条 golden vector 测试把字节格式钉死，并写明"只承诺本仓库实现口径" |
| 2 | §2.6（`:150`）新增 artifact 回收承诺 | **扩大** | **不完全一致**：sweep 的默认 dry-run、只由显式 CLI 调用、内容寻址可重建都与实现一致；但"引用枚举**全量扫描**"与实现（只扫 `--run-dir`）不符 → P1-2 | 建议（删除语义的承诺） |
| 3 | §2.7（`:161`）新增"模型属于测量外部" | **澄清为主**（把全部既有承诺的适用边界画在 harness 接口上）+ 内含一处新权威声明 | 部分一致：三种模式走同一 loop、live 非默认、OTLP 延迟导入都与实现一致；"录制的 usage 是权威、**不重算**"对 usage 段成立，但**成本在带预算的 replay 路径上按当前价目表重算**（`harness/llm.py:380-384`）→ P2-12 | 建议（这是对承诺边界的重新划定 + 一条新权威声明） |
| 4 | §3（`:202-207`）租约段落改写 | **扩大**（"已实现"） | 机制一致，但**没有生产接入点**，措辞读起来像已接入 → P1-1 | 不需要（改措辞即可），但改完建议在 HANDOFF 记一句"下一波接入写路径" |
| 5 | §4.0（`:245`）新增参数级安全域 | **扩大**（新增执行期拒绝语义） | **一致**。逐条核对：`allowed`/区间缺字段 ⇒ 拒绝；`forbidden` 缺字段 ⇒ 放行；越界/非数值 ⇒ `status=rejected` + `error_class=arg_policy_violation`；闸门在 `harness/loop.py:553-571`，位于 outbox 预写之前（被拒调用不留意图行，`tests/test_arg_policy.py:166` 断言了 `world` 0 行与无 pending 行）；嵌套/强转/正则确实未做 | 建议（新增了对工具作者可见的契约面） |
| 6 | §7（`:289`）新增 INV-008 bullet、§7 开放问题第 2 条划改 | 与 #1/#4 同源 | 与实现一致 | 随 #1/#4 |

**没有任何承诺被静默收窄或删除**：唯一被划掉的是 §7 开放问题第 2 条（租约接管），
它以删除线 + 说明的方式保留可见性，符合"承诺变化必须先改这里"的纪律。
但 `docs/semantics.md:1` 的版本行仍写"v1，W1 冻结"（P2-23），**建议补变更记录**——
本轮改动的面积（新增 1 条不变量 + 4 段承诺）已经不适合"冻结"这个词。

---

## 5. 关于"INV-008 与实现是否等价"（专项）

任务书要求单独回答：哈希输入是否字节级稳定（排序/编码/浮点）、能否在不触发验证器的情况下构造等价状态。

1. **字节级稳定性**：在克隆里对 8 种 payload 做"写入 → 重读 → 校验"往返实验
   （普通字符串键 / tuple 值 / `1.0` / `NaN` / `inf` / 中文 / 10^70 大整数 / 混合整数键）：
   **全部 OK**。两个隐患都被结构性挡住：
   * 整数键会让 `sort_keys` 出现"写入按数值序、重读按字符串序"的口径差异（如 `{9,10}`），
     但 `NewEvent.payload: dict[str, Any]` 在 pydantic 层就拒绝非字符串键（实测报
     `Input should be a valid string`），所以不可达；
   * tuple 会被 `json.dumps` 归一成数组，而哈希与序列化都走同一函数，因此也稳定。
   * `created_at` 是 `REAL` 列（SQLite 存 IEEE754 双精度），`float()` 往返精确。
2. **能否在"不触发验证器"的情况下构造等价状态**：`verify_chain` 不是写入路径的一部分
   （只有 `harness/state.py` 自己、`audit_chain` CLI 与测试引用它），因此**写入侧不做链校验**——
   这本身在文档里写明了（纵深防御、验证入口是增量 API + 离线 CLI），不构成缺陷。
   但有两个具体缺口：
   * `sqlite_store.py:295` 的静默挂接（P2-1）：尾行哈希为空时新事件挂到 genesis/fork 点，
     等价于"在一条断链上继续追加"，只有事后离线校验能发现；
   * `verify_chain(events, start_prev_hash=None)` 的"挂错地方"判定在
     `harness/state.py:126-137` 只在**本批没有任何其它违规**时才报——即"首条挂错 + 后面还有断点"
     时只报后者。方向上不影响红色结论（已经是红的），但"第一个断点"的定位会指偏。建议把
     这条判定移到循环之前，不与其它违规互斥。
3. **等价性结论**：定义 ↔ 实现**等价**（字段、顺序、编码、挂接规则、genesis/fork 语义逐项对上），
   限制是"第三方实现必须复刻 CPython 的 `json.dumps` 口径"（建议写成 golden vector）。

---

## 6. 作者自陈 5 条与设计不符的处理判断

| # | 自陈内容 | 我的判断 | 是否需要返工 |
|---|---|---|---|
| 1 | `--kill-after-ms` 必须先标定注入窗口，否则"全绿只是没被杀到" | **处置正确且必要**。标定结果写进了报告（本机 180ms / 35ms），我在报告里核对了标定表与每行 marker 计数（30/30）。这是本轮最有价值的一处"设计与实现不符上报" | 不需要。建议把 P2-7（判定不依赖 marker）一起修，让这份证据从"报表列"升级成"判定条件" |
| 2 | 窗口 2 的缝隙是微秒级，随机注入命中不了；其存在性改由确定性窗口注入证明 | **判断正确，且报告把方向讲清了**（"随机注入主张的是相反方向"）。副作用：**全部证明重量压在敏感性自检上** → 这让 P1-3（`--skip-sensitivity` 可伪造"通过"）从"小瑕疵"升级为"必须修" | 不需要（除 P1-3） |
| 3 | 渲染层卸载产生的 artifact 是派生缓存；引用只出现在视图里，不在事件 payload 里 | **方向正确，但落地时把"sweep 的引用来源"讲过头了**：docstring 写"全量扫描/不许只扫当前 run"，实现只扫一个 run 目录，且引用枚举里还有一处静默吞解析错误 → **P1-2** | **需要**：改 docstring + 加共享 root 的保护（或真的全量扫描） |
| 4 | `mutmut 3.x` 的 `--all` 是带值选项；`executescript` 会隐式 COMMIT（破坏补链事务原子性） | **处置正确**。两处都写在代码注释里（`scripts/mutation_check.py:75-77`、`harness/store/schema.py:24-25`），迁移用逐条 `conn.execute`（我在克隆里端到端验证了 v2→v3 迁移后的链与触发器）。**但**这一条恰好掩盖了同模块的另一个失败模式：`--all=true` 读得到结果的前提是 run 成功 → P0-1 | 需要（P0-1；与自陈第 4 条不是同一件事） |
| 5 | README 的 W7 段混用了两行阈值，已统一到选中行并给 0.85 行单独登记 claim | **处置正确且我复核通过**：`README.md:105` 现在是 `$0.11834；主桶增量 +41%，压缩动作本身占 +38%`，与 claim `w7-selected-avg-cost-usd=0.118337`、`w7-main-cost-delta-ratio=0.411634`、`w7-compaction-share-of-off-cost=0.379099` 一致，`w7-row-085-avg-cost-usd=0.118443` 也单独登记 | 不需要。**但同一类缺陷在同一个 W9 段里复发了一次**（P0-2 的变异数字）——建议在 PR 描述里把这两件事并列写出，作为 P1-4 的动机 |

---

## 7. 遗留风险与"本结论不适用的范围"

1. **链的字节口径是 CPython 专有的**：换语言/换实现要复刻 `json.dumps(sort_keys=True, ensure_ascii=False)`
   的浮点与转义规则，否则会出现假的"断链"。当前没有 golden vector 测试钉住字节格式（P2 建议）。
2. **INV-008 没有运行时执行点**：只有离线 CLI 与一个没有生产调用者的增量 API。
   "篡改无法静默"成立的前提是**有人跑校验**——这是运维前提，不是代码保证。
3. **变异基线的再生环境与 runner 不一致**（待确认，见 §8）：本仓库验证的环境是 Python 3.14.6，
   nightly 的 `mutation` 作业跑 Python 3.12。变异体集合是文本级的、稳定；但"某个变异体是否存活"
   取决于测试在 3.12 下的行为。P0-1 修好之前，这种不一致如果发生，表现是**绿**（什么都没跑出来）
   而不是红。
4. **fuzz 的统计含义**：180 次注入把失败率上界压到约 2%（rule of three），报告的措辞收敛
   （明确写了"不能据此声称覆盖所有窗口"）——这一点做得对，我不认为需要改。
   但要记住：**"0 条新类违例"的信息量完全取决于 oracle 敏感性与判定口径**
   （P2-5/P2-6：缺账本与"自我上报 unknown"这两条通道没有被门禁覆盖）。
5. **租约不是并发安全性**：没有生产接入点（P1-1），没有过期接管，`state_update` 仍未实现。
   README/HANDOFF 写"一次冒烟不是并发证据"——这个口径是对的。
6. **掉电语义、`-wal` 截断、位翻转**仍只做探测器（A §5）；torn-write 的结论明确限定为
   "截断后显式失败"。
7. **live 路径是骨架**：工具只下发名字与描述（无参数 schema），因此"三模式语义等价"
   只对 runtime 侧成立，不对供应商侧的模型行为成立（作者已在交付报告 §5 列出）。
8. **facts 门禁的保护面 = 已登记的 claim**：未登记的数字（README/HANDOFF 的散文数字）不受保护
   （P1-4）。本轮已实证一次。
9. 本报告**不评价**：W5–W7 的既有统计结论、六系统对照的方法学、以及 `docs/open-questions-answered`
   里 9 条裁决本身（只在它们与实现相交处做了核对：A §4.1 的注入面裁决与实现一致）。

---

## 8. 待确认（没有证据，不下结论）

1. **变异基线在 CI runner 上是否可复现**（Python 3.12 vs 本机 3.14.6、并行度 4 vs 8）。
   本机没有 3.12 解释器，无法在这里验证。建议合并后立刻手动触发一次 nightly `mutation`
   （`gh workflow run nightly.yml -f ...` 或网页端 dispatch），若幸存清单不同，则在 runner 环境
   `--update-baseline` 并按 `docs/testing.md` 的纪律在 PR 里记录原因。
2. **`main` 是否已开启 branch protection**：决定了六个分支是"推分支 + 网页 PR"还是"本地 merge 直推"
   （`docs/development.md` §2.2/§2.4 把这条留给所有者）。我这边没有 PR 可评，也没有做任何推送。
3. **`--skip-sensitivity` 的历史用途**：grep 显示没有使用者；如果它曾是调试便利，可以按 P1-3 直接删除，
   否则需要保留一个"明确表达未证明"的版本。
4. **`reports/chaos_fuzz_report.json` 的 8200 行是否应该入库**：逐次试验明细按 `docs/development.md`
   的既有口径是"可由命令再生的明细不入库（`--runs-out`）"，而这份 JSON 含 180 条 trial 明细。
   它被 claim 引用（`summary/killed`），所以入库有理由；但 8.2k 行的体积与"明细不入库"的纪律存在张力，
   是否拆分（汇总入库 + 明细走 `--runs-out`）请所有者定调。

---

## 9. 我检查过但（在这些点上）没发现问题的维度

为满足"允许结论是通过，但要给出检查范围"的要求，逐维列出检查动作与结论：

* **唯一权威**：cassette 是"事实数据源"但成本/预算仍落事件日志；lease 状态由日志折叠
  （`harness/lease.py:60-73`，无新表）；OTLP 是纯投影（`harness/otel.py` 不写库）；
  hash chain 列是派生值、不参与任何决策。**没有第二权威**。
* **append-only 与迁移**：触发器文本与 DDL 共用同一常量（`schema.py:26-43`），迁移里 `DROP` → 补链 →
  `conn.execute(TRIGGER_STATEMENTS)` 原样重建，且用**逐条 execute** 避开 `executescript` 的隐式 COMMIT
  （`harness/store/schema.py:196-273`）。我在 main 造的 v2 run 目录上跑了全链路：迁移 → 链校验通过 → `resume`
  继续写 → 退出 0。旧 run 目录仍可读，**触发器未被削弱**（迁移用例还断言了 UPDATE/DELETE 仍被拒）。
* **确定性 / CRN**：fuzz 的注入时刻 = `args.seed + 组合序号×10 + 阶段序号`（不含 system/scenario 名，
  有用例钉住同 seed 同序列）；噪声流 = `noise_seed:scenario:repeat`（两条流分离，`error_rate=0` 时
  判定逐位不变）；`chaos_fuzz` 的落点受时序影响这一点被**明确写进报告边界**。
  评测层（`opsenv.suite`）没有引入新的随机源。
* **依赖边界**：`pyproject.toml` 的 `dependencies` 仍只有 `pydantic>=2.7`（与 main 逐字相同）；
  `[otel]` extra 是新增的独立 extra，CI 不装它；`harness/otel.py` 顶层只 import 标准库 + `.trace`，
  otel 的 import 全在函数体内（`:44-46`、`:97`、`:102-104`），`harness/trace.py` 里对 `.otel` 的
  import 也在 CLI 分支内（`:422`、`:432`）。
* **接口命名/粒度**：`ArgPolicy.evaluate → (bool, 稳定原因串)`、`LeaseCheck.reason`、
  `Finding.code`（`inv_*` 约定）都是"可断言 + 可进错误事件"的稳定标识，与仓内既有风格一致；
  `opsenv/oracle.py` 是纯函数 + 合成数据库可测（11 条单测印证）。
* **耦合方向**：`opsenv/oracle.py` 不 import `harness`；`scripts/*` 与 `experiments/*` 只 import 公开模块路径，
  没有出现 `from harness.x import _private` 这类越界（grep 无命中）。
* **范围蔓延**：对照 A §1 / B §1 的非目标清单逐条看——没有掉电语义实现（只探测器）、
  verify 主作业没有被拉长（新作业独立）、14 条门禁的阈值与判定逻辑未改
  （`reports/w6_eval.json` 的 `gate_summary{total:14, passed:14}` 与 `opsenv/suite.py:774-782`
  只对噪声口径翻 `enforced`）、没有模型选型/prompt 评测/网关重试、没有多租户/鉴权。
  OTLP/GC/lease 都在 B §1 的 G4/G5/G6 里，**没有越界**。
* **措辞纪律**：`README/HANDOFF/semantics/fault-spectrum/noisy-reasoner/model-modes/governance-extras`
  里没有"生产级/高并发/高可用/提升了 X%"式表述（grep 只命中一句"不写'生产级'"的自我约束）；
  fuzz 报告的边界段与 rule-of-three 表述正确。

---

## 10. 给出结论的一句话版

机制层：**可以合并**（六处注入实验、一次真迁移、两条门禁自检都支持这一点）。
门禁与文档层：**先修 P0-1（mutation 门禁假绿）与 P0-2（README/HANDOFF 变异数字与基线矛盾），
再修三条 P1 措辞/开关（租约承诺、sweep 的"全库扫描"、`--skip-sensitivity`），然后按
`docs/open-questions-answered` → … → `docs/w9-backfill` 的顺序合并**。
