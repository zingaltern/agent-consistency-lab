# 独立功能验证报告：W9 交付（设计文档 A 测试强化 + B 功能扩展）

* **被测 commit**：`49f9f51`（分支 `docs/w9-backfill`，即六个堆叠分支的 tip）。
  `main`（`0ab04b3`）**尚未**包含本交付——它是 `docs/w9-backfill` 的祖先，因此按"若 main 已完成
  合并才以 main 为准"的条件，本次以 tip 为被测对象。
* **验证者**：独立测试会话（与被测代码的作者会话隔离，非真人专家）
* **验证环境**：`/tmp/verify-w9`（隔离克隆 + 独立 venv，`pip install -e ".[dev,eval]"`，
  Python 3.14.6，10 核 macOS）。**原仓库全程只读**，全部产物写 `/tmp/w9-evidence/`。
* **方法纪律**：先只读"规格"（`docs/semantics.md`、README、源码、`--help`，以及设计文档
  A/B 这两份**验收口径**），完成 15 个方向的验证并冻结结论之后，才读作者的
  `docs/design/2026-09-17-delivery-report.md` 与各轮报告。
* **本地探针 vs 仓库规模**：验证期间对 `/tmp` 副本做过 6 次"故意改坏"（README 数字、
  `reports/*.json` 字段、claim 容差、claim `source_path`、变异基线、`mutmut run` 直通），
  **每次都已 `git checkout` 还原或挪回**，最终 `/tmp` 副本 `git status` 干净。
* **并行工作**：审计期间发现**同一个工作目录里还有另一个会话**在做 W9 架构评审
  （提交 `9e9a570`）并在 `fix/review-p0-p1` 上修复。本报告结论先于读到它冻结；
  交叉验证见 §8。**本人未修改被审计仓库里的任何代码**，只新增本报告两个文件。

---

## 0. 结论摘要（先读这一节）

**总体：1 个 P0，且它落在门禁层而不是机制层。** 设计文档 A/B 的 11 个目标我逐条复现通过；
**没有一条运行时机制性结论被证伪**——崩溃语义、审批绑定、哈希链、恢复与对账在独立复跑下
都成立。**但**新门禁里有一条会"假绿"：变异门禁在 `mutmut` 什么都没跑出来时仍打印
"没有新增幸存变异：测试盲区没有扩大"并以 0 退出（§3 P0-1，我独立复现；与并行架构评审
的 P0-1 重合）。会假绿的门禁比缺失的门禁更危险，所以这一条必须修。

**能不能合：不推荐按现状合并。** 合并门槛 1–7（`docs/development.md` §2.3）我全部复跑通过
（pytest / ruff / crash_matrix 16 格 / opsenv gate 14/14 / 产物对账 / 文档数字 / 回归用例），
所以**机制层可以合**；阻塞点在门禁与文档层：

| 优先级 | 必须先修什么 | 为什么 |
|---|---|---|
| **P0** | 变异门禁在"什么都没跑出来"时判绿（§3 P0-1） | 一次 `mutmut` 失败/零产出会被读成"盲区没有扩大"，且没有任何 claim 兜底；违反 `AGENTS.md` 红线 5 |
| P1 | GC 的引用枚举只扫**单个 run**：`--artifacts-root` 指向共享库时 `--apply` 会删掉**另一个 run 仍在引用**的对象（设计文档 B §R-B5 明文禁止"只扫单 run"） | 数据丢失路径；默认（每 run 独立 artifacts 根）不受影响，故为 P1 |
| P1 | `README.md:122` 与 `docs/HANDOFF.md:166` 的变异数字（`1642/1000/628`）与入库基线（`1670/1031/625`）不符；门禁只保护"登记过的数字"，改 README 数字不触红 | 这是本轮交付**专门要消灭**的那类漂移，且它发生在引入门禁的同一个提交里 |
| P1 | fuzz 的 `verdict` 不要求"注入真的命中"——标定偏大时 0 次注入仍退出 0（§3 P1-1） | "没被杀到却全绿"；交付那份 180/180 是真的、且有 claim 兜底，故 P1 |
| P1 | `docs/semantics.md` §3 的租约承诺缺"本版**没有运行时接入点**"限定（§3 P1-4） | 承诺读起来像"runtime 会拦未持租约的写"，实际只有库接口 + 用例，worker/loop 零引用 |
| P2 | `docs/semantics.md` §7.5 仍写"artifact GC … 未实现回收"，与 §2.6 自相矛盾 | 承诺清单内部矛盾，属"改代码没回灌文档" |

**无法判定项**（详见 §4）：live（真实 API）路径的行为——本轮**不发起任何真实调用**，
只验证"默认不跑、marker 已注册、CI 只跑反例"；变异门禁在 CI runner 上的实际耗时；
随机注入的**落点**（代码位置）——报告自己已声明只主张"注入时刻可复现"。

---

## 1. 方法与证据链

| 验证手段（独立方法） | 覆盖方向 | 说明 |
|---|---|---|
| **退化注入**：改坏机制 → 看门禁是否变红 | A-G1、A-G5、B-G1 | 5 次注入，每次记录"改了什么 / 期望 / 实际退出码" |
| **自造 fixture**：用裸 `sqlite3` 造最小 `runtime.db`/`world.db`，只调 `opsenv.oracle` 的公开入口 | A-G2（oracle） | 不复用作者任何测试或 fixture；含 4 例假阳性对照 |
| **自己实现被验证的公式**：按 `semantics.md` §2.4.1 的文字重写哈希链计算，逐条比对库里的 `event_hash` | B-G3 | 检验"文档定义 ↔ 实现"是否等价 |
| **对照交付前 commit**：`git worktree` 检出一周前的代码，对同一 run 目录比 CLI 输出 | B-G4 | 逐字节比对（`diff` 为空） |
| **走别人的出口**：用 `replay_consistency.py` 自己的 `_compare/_fingerprint` 比对被篡改的 replay 产物 | B-G1 | 用被测方自己的白名单口径验证其检测能力 |
| **换 seed / 换参数重跑**：噪声人格 3 个 seed、fuzz 换 seed、标定窗口在本机重测 | A-G2、A-G4 | 判"结论是数据的性质还是单次抽样的巧合" |
| **真子进程**：租约用两个真进程、崩溃注入用真 SIGKILL | B-G6、非回归 | 不 mock |
| **交叉印证**：同一条 claim 同时与"再生命令输出"和"仓库里的 `reports/*.json`"两条路径比 | A-G1（成色） | 17 条抽样 |
| **逐格对账**：新跑产物与入库产物做字段级/单元格级 diff | 非回归 | crash_matrix 16 格、w6_eval 8 格 |

---

## 2. 各方向结论表

| # | 方向 | 我的结论 | 关键证据（数字来自我的复跑） |
|---|---|---|---|
| 1 | **A-G1 claim 门禁** | 门禁**有效**；三类设计内注入全红，但**不覆盖文档正文** | `--run verify` 27/27、`--run nightly` 45/45、退出 0；改 `reports/*.json` 字段→退出 1 并报"期望 625±0.0，实测 624"；改 claim 值→退出 1；置空 `source_path`→"空路径等于没有绑定"退出 1；**改 README 数字→仍 27/27 通过** |
| 2 | **A-G1 claim 成色** | 抽查 17 条全部对得上，容差合理，档位分配与耗时相称 | 17/17 与两条独立路径一致（W4 ×4、W3/W4 矩阵 ×3、W5 ×4、W6 ×2、W7 ×2、fuzz/mutation/replay ×3）；计数类容差 0、比值类 1e-6~1e-9；verify 轻集实测 **7.3 s** |
| 3 | **A-G2 fuzz 可复现** | 标定与本机一致、注入时刻逐位可复现；**但门禁存在假绿通道** | 本机标定 run=180 ms / resume=35 ms（与交付报告同值）；6/6 格的注入时刻序列与交付 JSON 逐位相同；换 seed 424242 → 30/30 命中、0 新类违例、绿 |
| 4 | **A-G2 oracle 判定** | 四类不变量判定**正确**，假阳性对照全过；`inv_log_readable` 与文档不符 | 合成库 10 例：合规样本 0 findings；"有副作用无 tool_result"→`inv_closed_calls`；"同键 2 行无 unknown"→`inv_effect_accounting`；`pending` 行/已呈报 unknown/空账本→不误报；seq 断号与 id 重复→`inv_no_tamper`；**主库损坏→抛 `sqlite3.DatabaseError` 而不是 `inv_log_readable`** |
| 5 | **A-G3 四档探测器** | 四档结论**全部复现**；一处措辞不准 | SIGTERM：`graceful_handler_registered=false`、退出码 −15、账本 0→1、每键 ≤1；torn write：三档 resume 全部非零退出 + 零新副作用（但 0.99 档主库**可打开**，只有 integrity_check 报错）；SIGSTOP：40 次真实冻结，账本取值只出现 {0,1}、每键 ≤1；账本外注入：两变体账本行数不变、run 不复活 |
| 6 | **A-G4 噪声人格** | 口径敏感性**被真实激活**，方向对 seed 稳定，未与主口径混排 | strict 0.667 < cause_only 0.776（192 次/格，报告已披露样本量）；三个 noise seed（101/202/303）下 `strict<cause_only` 恒成立；三条读证据路线仍逐格相同；1 条门禁红且 `enforced:false`；主口径 `w6_eval.json` 里 strict==cause_only（0.901），两者不相等、未污染 |
| 7 | **A-G5 变异测试** | 基线**可复现**、删基线会红；但门禁在"零产出"时**假绿**；限时口径与文档不符 | 真实 mutmut：`killed=1031 survived=625 no_tests=14 rate=0.377`，与入库基线**同数**；删基线一条→退出 1 并指名 `...recovery_plan__mutmut_9` + 打印 diff；**把 `mutmut run` 换成直通（零结果）→ 仍退出 0 并打印"没有新增幸存变异"（P0-1）**；实测 354 s（冷）/28 s（热缓存），CI 参数 `--timeout 1320`（22 min，而文档写 15 min） |
| 8 | **B-G1 三模式模型** | 一致性主张成立；失败语义**部分履行** | `identical: true`（白名单：token/cost/事件序列/tool_calls）；删一条 cassette→**可读拒绝**（报出 `(step=1, attempt=0)` 与期望指纹）；usage 篡改→对账层能抓（`events.tokens_cost` 差异）；**指纹不一致的"警告"无出口**；**等 token 的文本篡改完全静默** |
| 9 | **B-G1 崩溃兼容** | `CHAOS_WINDOWS` 在 replay 下**语义不变** | replay + `CHAOS_WINDOWS=pre_tool_exec:1` → 退出 137 + marker `injection_kind: window`；`--kill-after-ms` 在 replay 下同样可杀（同场景标定窗口 4 ms，与 scripted 一致） |
| 10 | **B-G2 参数级审批** | 两条路径**都被闸住**，未发现"批准 A 执行 B" | P1 TOCTOU：执行前改参→`args_sha256_mismatch`，副作用 0；P1b "批准了越界参数"→原调用 `superseded` + 新调用 `arg_policy_violation`，副作用 0；P2 session 复用：越界参数被拒，副作用 0；边界值含端点、类型不符（字符串/布尔/None/列表）全拒；先拒绝→不执行 |
| 11 | **B-G3 事件哈希链** | 定义与实现**等价**；篡改**可定位** | 我按文档文字重算 14 条事件的 `sha256(prev_hash ‖ record_bytes)` → 逐条一致；`DROP TRIGGER` 后改 seq=5 的 payload → 退出 1、`first_break.seq=5`、`code=INV-008`；删中间一条 → 退出 1；分叉分支 `prev_hash` == fork 点事件哈希且自算一致、全量校验通过 |
| 12 | **B-G4 OTLP** | 三项主张**全过** | 未装 `[otel]` 时 `import harness.trace` 正常且 `sys.modules` 无 `opentelemetry`；不带 `--otlp-endpoint` 的 CLI 输出与交付前 commit 逐字节相同（1351 B，`diff` 为空）；只有显式传端点才走导出（本地废弃端口 → 可读 `OtelExportError`；缺 extra 且不带 `--otlp-json` → 退出 1 并报缺模块） |
| 13 | **B-G5 artifact GC** | 正面要求全过；**共享根会误删** | 四类引用来源（events / checkpoints / writes）逐一枚举成功、孤儿不计入；dry-run 不删；`--apply` 只删孤儿；删后重读 `FileNotFoundError` 带 digest；CLI 默认 `dry_run=True`；**共享 `--artifacts-root` 下会删掉别的 run 仍在引用的对象** |
| 14 | **B-G6 租约** | 库接口的 8 项双进程检查全过；但**没有运行时接入点**，而语义文档没写这一限定 | 未持有→`lease_not_acquired`；A acquire 成功；B（不同 owner）→`lease_held_by_other`；B renew→可读失败（不做隐式接管）；持有者 renew 成功；过期后同 owner→`lease_expired`（不自动续期）；`lease` 事件 kind 恒为 `artifact`；`semantics.md` diff 为**纯新增**且写明三条"不承诺"；**但 `SingleWriterLease`/`guarded_write` 在非测试代码里零调用点**（worker/loop/examples 全无引用）→ P1-4 |
| 15 | **非回归** | 门槛 1–5 **全部通过**，产物逐格一致 | `pytest`：默认 312 passed/1 skipped/3 deselected，`-m live` 3 passed（collected 316 = claim）；`ruff check .` 全净；`opsenv.suite --per-fault 8 --repeats 3 --gate` **14/14** 退出 0；`crash_matrix --repeats 5` 16 格 / 80 次 / **70 次真 kill** / 0 prediction-violations，且与入库 JSON **逐格相同**；`w6_eval` 8 格 rates/statistics/catalog 与入库**完全相同**；`w4_context_cost`/`w7_sweep` 抽样的 claim 值一致 |

---

## 3. 缺陷列表

> 分级沿用仓库既有定义：**P0** 某条结论不成立；**P1** 声称与实现不符但结论仍成立；
> **P2** 文档/口径与事实不符。

### P0

#### P0-1｜变异门禁在"什么都没跑出来"时判绿

* **现象**：`scripts/mutation_check.py` 的 `run_mutmut()` **不检查 `mutmut run` 的退出码**
  （既不 `check=True` 也不看 `returncode`，只包了超时）。因此 mutmut 快速失败/零产出时，
  脚本继续读结果表；结果表为空 ⇒ `killed=0, survived=[]` ⇒ 判"没有新增幸存变异"并 **exit 0**。
  输出还会打印一句**主动误导**的话："有 625 条基线里的幸存变异已被杀死（好事，不判失败）"。
* **为什么是 P0**：这是"机制被改坏 / 根本没跑起来 → CI 仍然绿"，直接违反 `AGENTS.md` 红线 5；
  且**没有任何二次兜底**——claim `mutation-survivors=625` 由 `mutation_check.py --summary-only`
  再生，而那条路径**只读基线文件**，根本不会发现 mutmut 没跑。
  （对照：facts 门禁在这一类上是稳的——再生命令退出非零、或没写出 JSON、或路径取不到值，
  都会被判失败。）
* **最小复现**（隔离副本，不需要改仓库）：
  ```bash
  cd /tmp/verify-w9
  mv mutants /tmp/mutants-backup && mkdir mutants      # 模拟 CI 全新区、零结果
  .venv/bin/python - <<'EOF'
  import io, sys
  from contextlib import redirect_stdout
  sys.path.insert(0, "."); sys.path.insert(0, "scripts")
  import scripts.mutation_check as mc
  mc.run_mutmut = lambda **kw: print("[模拟] mutmut run 零产出")
  buf = io.StringIO()
  with redirect_stdout(buf):
      code = mc.main([])
  print("退出码 =", code); print(buf.getvalue())
  EOF
  # 实测：退出码 = 0；输出含 "[mutation] killed=0 survived=0" 与
  #       "没有新增幸存变异：测试盲区没有扩大"
  mv /tmp/mutants-backup mutants
  ```
* **建议**：`run_mutmut` 检查 `returncode`（非零即失败）＋让 `main()` 在
  `killed == 0 and survived == 0`（= 什么都没判定）时**显式判失败**——"跑完"与"没发现回归"
  是两件事，这一条与脚本 docstring 里"超时即失败"的纪律同源。
* **交叉验证**：并行架构评审（提交 `9e9a570`）把同一现象判为它的 P0-1，我在不知道它的
  结论之前独立复现了同一结果（见 §8）。

### P1-1｜fuzz 门禁存在"没被杀到却全绿"的通道（违反红线 5）

* **现象**：`experiments.chaos_fuzz` 的 `verdict` 只由 `unexpected_findings == 0 and
  sensitivity.sensitive` 决定，**不要求任何一次注入真的命中**。把标定改大（= 采样时刻全部
  落在进程退出之后）后，18 次试验**一次都没杀死进程**，报告仍打印
  "总试次 18，实际注入成功 0 次，新类违例 0 条"，退出码 0。
* **为什么算问题**：`docs/fault-spectrum.md` §2 自己把这种情况称为"最危险的一种假绿"，
  实现里的保险只覆盖另一个方向（标定窗口 == 0 时 `assert` 拦下）。`AGENTS.md` 红线 5
  要求"每条新门禁须做退化注入验证（把机制改坏 → CI 必须变红）"——**本门禁做不到**。
* **缓解**：交付的那份 `reports/chaos_fuzz_report.json` 我核过是 180/180 命中、180 个
  `time_hit` marker、0 异常，所以**结论未被证伪**；nightly 的 claim
  `fuzz-killed-injections=180` 也能在"标定系统性偏大"时兜住（该 claim 会重跑同一条命令）。
  真正的暴露面是"标定在与试次不同的负载下做"这类**部分失效**：可能只命中一部分，
  而 `verdict` 依旧绿。
* **最小复现**（不需要改仓库，进程内替换标定函数即可）：
  ```bash
  cd /tmp/verify-w9 && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'EOF'
  import io, sys
  from contextlib import redirect_stdout
  sys.path.insert(0, ".")
  import experiments.chaos_fuzz as cf
  cf.calibrate_kill_window = lambda **kw: 2000      # 标定偏大 = 注入全落空
  buf = io.StringIO()
  with redirect_stdout(buf):
      code = cf.main(["--repeats", "3", "--seed", "7",
                      "--workroot", "/tmp/w9-evidence/fuzz-sabotage-a2"])
  print("exit =", code)
  print([l for l in buf.getvalue().splitlines() if "总试次" in l])
  EOF
  # 实测：exit = 0；"实际注入成功 0 次 … 新类违例 0 条"；敏感性自检仍打印"通过"
  ```
* **建议**：`verdict` 增加一条硬条件（如 `killed == trials` 或至少 `killed / trials > 0.9`
  且 `killed > 0`），失败即非零退出；或让 `--skip-sensitivity`/标定异常单独判红。

### P1-2｜claim 门禁不覆盖"文档正文 ↔ claim"这一跳

* **现象**：门禁把 `claim.value` 与**再生命令的输出**对齐，但**从不读 README/HANDOFF 正文**。
  因此"改了 README 里的数字"不会被发现（实测 27/27 仍通过）；只有被登记成 claim 的数字
  才受保护，而未登记的数字**完全无人看管**。
* **已发生的实例**：见 P2-1（README/HANDOFF 的变异数字与基线不符，就发生在引入门禁的
  同一个提交里）——这说明缺口不是理论上的。
* **为什么算"声称与实现不符"**：设计文档 A 的 G1 写的是"README/HANDOFF 中**所有**被引述的
  关键结论数字，每条都能由脚本自动再算并对齐"，README 的 W9 段落标题是"文档里的数字从此有
  门禁"。实际覆盖范围 = 72 条登记项，而非"所有被引述的数字"。
* **最小复现**：
  ```bash
  cd /tmp/verify-w9
  python3 - <<'PY'
  import pathlib; p = pathlib.Path("README.md"); s = p.read_text()
  p.write_text(s.replace("命中率 57.4% → 0%", "命中率 87.4% → 0%", 1))
  PY
  .venv/bin/python scripts/check_facts.py --run verify ; echo "exit=$?"   # 实测 exit=0，27/27 通过
  git checkout -- README.md
  ```
* **建议**（任选其一，成本都不高）：① 在 claim 里加 `doc_quote` 字段，脚本用正则把
  README/HANDOFF 里的数字抽出并与 claim 值比对；② 退一步，在 `docs/development.md` §4 与
  README 里把口径改成"被登记为 claim 的数字受门禁保护；未登记的数字不受保护"，
  并把"新增结论数字必须同时登记 claim"写成硬约束（现在只是软约定）。

### P1-3｜GC 的引用枚举只扫单个 run，共享 artifacts 根会误删

* **现象**：`harness/artifacts.py::referenced_digests(run_dir)` 只读**传入的那一个** run 的
  `runtime.db`。当 `--artifacts-root` 指向多个 run 共用的库时，从 run A 扫描会把只被 run B
  引用的对象判成孤儿，`--apply` 直接删掉。
* **为什么算问题**：设计文档 B §R-B5 明文规定"引用枚举 … **不限于当前 run 目录** …
  **禁止只扫单 run**（否则会误删被其它 run 引用的 artifact）"，`docs/governance-extras.md`
  §2 也把"只扫当前 run 目录会误删被别的 run 引用的对象"列为"会丢数据的错法"之一。
  实现正是它自己警告的那一种。
* **影响范围**：默认路径（artifacts 根 = `<run-dir>/artifacts`，worker 就是这么建的）**不受影响**，
  所以不是 P0；但 `--artifacts-root` 是已交付 CLI 的正式开关，用户按它把库放共享位置就会丢数据。
* **最小复现**（合成两个 run + 一个共享根，全部在 `/tmp`）：
  ```bash
  cd /tmp/verify-w9 && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /tmp/w9-evidence/gc_shared_root.py
  # 实测：dry-run 报 orphans=1；加 --apply 后 runB 引用的 digest 被删（"FAIL：runB 仍引用的对象被删掉了"）
  ```
* **建议**：三选一——① `sweep` 接受"引用来源 = 共享该 artifacts 根的**所有** run 目录"
  （或反过来要求传 `--run-dir` 列表）；② 当 `--artifacts-root` 不等于 `<run-dir>/artifacts`
  时**拒绝执行**并提示"本版只支持单 run 库"；③ 至少在 `docs/governance-extras.md` 写明
  该限制（最省事，但仍是数据丢失风险）。

### P1-4｜租约承诺没有"本版无运行时接入点"的限定

* **现象**：`docs/semantics.md` §3 写"持有有效租约的进程可以写；未持有 / 已过期 / 被别人持有
  ⇒ **可读失败**（绝不静默放行）"，读者会以为 runtime 的写入路径会拦。实际
  `SingleWriterLease` / `guarded_write` 在**非测试代码里零调用点**：`experiments/worker.py`
  与 `harness/loop.py` 里没有任何租约引用，`examples/` 也没有。也就是说这是一份**库级契约 +
  用例**，不是"runtime 会拦"。
* **为什么是 P1**：语义文档是承诺清单，代码、测试、后续结论都以它为口径；把"库接口存在"
  写成"写入会被拦"会让第三方验证者得出错误结论（我自己在 §2 第 14 行最初也据此判"无差异"）。
  `docs/governance-extras.md` §3 的示例是库用法，没有说"runtime 接入"——两份文档的读者会得出
  不同印象。
* **最小复现**：
  ```bash
  cd /tmp/verify-w9
  grep -rn "SingleWriterLease\|guarded_write" --include="*.py" . | grep -v "^./tests/" | grep -v "^./harness/lease.py"
  # 实测：输出为空（只有 tests/test_lease.py 与 harness/lease.py 自身有引用）
  ```
* **建议**：在 §3 补一句限定（"本版只提供库接口与用例；runtime 的写入路径**尚未**接入租约，
  接入点是扩展点"），或把 `state_update` 旁边的"已登记未实现"式标注同样给租约的**接入**部分。

### P2 清单

| # | 缺陷 | 事实 | 最小复现 |
|---|---|---|---|
| P2-1 | `README.md:122` 与 `docs/HANDOFF.md:166` 的变异数字与入库基线不符 | 文档写 `1642 变异体 / 1000 killed / 628 survived`；`reports/mutation_baseline.json` 是 `1670 / 1031 / 625 / 率 0.3774`（作者交付报告 §1/§5 用的是**正确**数字，说明是 README/HANDOFF 没回灌）。溯源：基线在 `29905af` 是 `1642/1000/628`，在最终提交 `49f9f51` 被重算成 `1670/1031/625`，而 README 的 W9 段正是在这同一提交里写的 | `grep -n "1642" README.md docs/HANDOFF.md` vs `python -c "import json;print(json.load(open('reports/mutation_baseline.json'))['counts'])"` |
| P2-2 | `docs/semantics.md` §7.5 与 §2.6 自相矛盾 | §2.6 写"artifact 回收（R-B5）：`harness.artifacts.sweep(dry_run=True)` …"（已交付），§7 未决问题 5 仍写"artifact GC 只有 `orphan_count` 暴露，**未实现回收**（W7 之后）" | `sed -n '150p;319,320p' docs/semantics.md` |
| P2-3 | oracle 的 `inv_log_readable` 实际不可达 | `docs/fault-spectrum.md` §4 把它列为一条判定（"主库读不出来 = 显式发现"），但 `audit_run` 里 `check_no_tamper` 处理了读失败、`check_closed_calls` 的查询**没有**包住：主库损坏时 `audit_run` 抛未捕获的 `sqlite3.DatabaseError`（两种损坏样本都复现）。结论方向仍安全（抛异常=响亮的失败，不是静默判绿），且 torn-write 探测器的结论**不依赖**它 | `cd /tmp/verify-w9 && .venv/bin/python /tmp/w9-evidence/oracle_synthetic2.py`（09/09b 两例 FAIL：`RAISED:DatabaseError`） |
| P2-4 | torn write 一档措辞不准 | 文档："三种截断（50% / 90% / 尾部 −4KB）都让主库不可打开"。实测 0.99 档 `openable=True`（`PRAGMA integrity_check` 报 `*** in database main ***`），只有 50%/90% 打不开。三档的 `resume` 都是非零退出 + 零新副作用，**结论不变** | `.venv/bin/python scripts/probe_tornwrite.py --mode truncate --json-out /tmp/t.json` 后看 `results[*].openable` |
| P2-5 | 变异门禁的限时口径不符 | 设计文档 A §R-A5 与 `nightly.yml` 注释都写"**15 分钟**预算写死在脚本参数里"，实际参数是 `--timeout 1320`（**22 分钟**），job 上限 25 分钟。本地实测 354 s（冷）/28 s（热） | `grep -n "15 分钟\|--timeout 1320" .github/workflows/nightly.yml docs/design/2026-09-17-test-hardening.md` |
| P2-6 | replay 的"指纹不一致 ⇒ 警告"在端到端没有出口 | `docs/model-modes.md` §4 承诺"记进 `replay_warnings`"。该列表只在 `harness/llm.py` 内被 append（外加一条单测断言），**worker 不打印、不落事件、不进 outcome JSON**——改掉 cassette 的 `request_fingerprint` 后三个 phase 全 `exit=0`，事件日志里也查不到任何告警 | `cd /tmp/verify-w9 && .venv/bin/python /tmp/w9-evidence/cassette_tamper.py`（④ 指纹被改 → "静默完成"） |
| P2-7 | cassette 的**文本**不在任何校验面内 | 白名单比较 token/cost/事件序列/tool_calls 结构，都不含模型文本；`request_fingerprint` 只覆盖**请求**。把首条录制的文本换成等 token 数的"先删指标"后，replay 全绿、对账 `identical=true`，而篡改文本**落进了事件日志**。`docs/model-modes.md` §5 的表把"不比较"列成了时间戳/ID/`view_fingerprint`/计时噪声，**没提文本** | `.venv/bin/python /tmp/w9-evidence/cassette_tamper.py` + `/tmp/w9-evidence/cassette_text_boundary.log` |
| P2-8 | `ArgPolicy(allowed=[1])` 接受 `True` | 区间策略显式拒绝布尔（`isinstance(value, bool)` 判非数值），但 `allowed` 列表用 `in` 判等，`True == 1` 成立 → 数值白名单会被布尔值"等价穿透"。方向安全（`forbidden` 侧同样成立，等于更严），影响低 | `python -c "from harness.tools import ArgPolicy; print(ArgPolicy(field='m', allowed=[1]).evaluate({'m': True}))"` → `(True, 'ok')` |
| P2-9 | `SweepReport.never_deleted` 是死字段 | 声明在模型里，但 `sweep()` 与任何代码都不填充它（全局 grep 只命中声明处）。报告读者会以为它表示"永不回收清单" | `grep -rn "never_deleted" --include="*.py" .` |
| P2-10 | `--skip-sensitivity` 会伪造"敏感性自检通过" | 该 flag 把 `sensitivity` 直接置成 `{"sensitive": True}`，于是报告会打印"敏感性自检…通过"而**实际没跑**。CI 未使用该 flag，属本地误用风险 | `.venv/bin/python -m experiments.chaos_fuzz --repeats 1 --skip-sensitivity --workroot /tmp/x | grep 敏感性` |
| P2-12 | `check_facts` 的默认输出目录是固定的 `/tmp/facts`，同机并发两次运行会互相清档 | 每条 claim 的输出路径 = `/tmp/facts/cmd-<hash>.json`，脚本在跑命令前会先 `unlink`。我这次审计就撞上了：并行的另一条会话也在同一台机器上跑 `check_facts`，导致我读某条 claim 的再生输出时文件恰好被删掉（表现为"再生=None"）。CI 每个 job 独立容器、不受影响；本地两个会话/两个 checkout 同跑会互相干扰 | 并发跑两条 `check_facts`（不同 workdir，默认 outdir 相同），观察其中一条出现"命令未写出 …（source_cmd 缺写盘参数？）"或读数失败 |
| P2-11 | 交付报告 §2 的"316 passed"措辞不精 | 实际是 `collected 316`（默认 `addopts` 过滤 3 条 `live`：312 passed + 1 skipped；`pytest -m live` 3 passed）。claim `tests-collected` 定义的是 collected，数字**没错**，只是"passed"用词偏了 | `.venv/bin/pytest -p no:cacheprovider 2>&1 | tail -1` |

---

## 4. 无法判定项

1. **live（真实模型）路径的全部行为**——按任务纪律不发起真实调用。已验证的部分：
   `pyproject.toml` 注册了 `live` marker 且默认 `addopts` 过滤它；`pytest -m live` 在无 key 下
   **3 passed**（反例测试：live 模式必须给出可读失败并非零退出）；CI 的 `ci.yml` 里
   `facts` / `experiments` / `verify` 三个作业均不含在线调用。**未验证**：真实 transport 的
   字段映射、真实 usage 的 canonical 归一、真实调用的预算硬停（这三项在交付里由
   `--transport scripted` 的端到端路径 + 单测覆盖，我没有真 key 去证伪）。
2. **`--kill-after-ms` 在 replay/record 模式下的"可杀死窗口"是否够用**——我实测同场景下
   scripted 与 replay 的窗口都是 4 ms（默认场景），机制可用；但 `chaos_fuzz` 的标定
   **只跑 scripted 模式**（它的 `_worker` 不传 `--model`），所以"模型模式下的随机注入覆盖"
   没有任何现成门禁，我也无法替它判定"够不够"。
3. **变异门禁在 CI runner 上的实际耗时**——本地 10 核 354 s（冷）远低于 1320 s，但 runner 的
   核数与磁盘 I/O 不同，我无法判定 22 分钟预算在 CI 上是否宽裕（这个数字只能由 CI 自己回答）。
4. **随机注入的"落点"**——报告自己声明只主张"注入时刻可复现"，落点受机器时序影响；
   我复现了"时刻逐位相同"（6/6 格），落点问题按声明不主张，故不做判定。
5. **`fix/review-p0-p1` 上的修复是否修好**——审计期间有另一个会话正在修 P0-1/P0-2 等条目
   （工作区内 33 个文件被改，我离开时仍在进行）。我的全部结论**只对被测提交 `49f9f51` 成立**；
   它们的修复版本我没有验证，也不在本次交付的范围内。
6. **`mutmut` 缓存导致的"热跑"**——第二次门禁运行（28 s）复用了首次的变异结果。
   CI 每次全新 check out，不存在该问题；我无法判定本地长期使用同一 `mutants/` 目录时
   是否会出现"源码改了但缓存未失效"的假绿（mutmut 的设计是增量重跑，理论上会重算）。

---

## 5. 未覆盖风险

0. **会假绿的门禁这一类**：我实测到三处"机制被改坏/没跑起来 → 门禁仍绿"的形态——
   `mutation_check`（P0-1，最严重，无兜底）、`chaos_fuzz` 的 `verdict`（P1-1，报告文本里可见
   0 命中、且有 claim 兜底）、`--skip-sensitivity`（P2-10，伪造"通过"）。我**没有**对
   余下门禁逐条做这一方向的注入（`opsenv.suite --gate`、`check_gates` 的 14 条、
   `experiments.crash_matrix` 的 `as_predicted` 判定、`harness.audit_chain` 的退出码语义），
   所以"其余门禁都不会假绿"这一结论我**没有**验证过。
1. **文档文本与 claim 的双向漂移**（P1-2 的延伸）：即使修了 README 那三个数字，
   下次改代码后仍可能出现"claims 更新了、正文没更新"。这是本轮结构性风险的**唯一未解决项**。
2. **共享 artifact 根**（P1-3）：我的复现是合成的最小场景；真实多 run 共享库的删除范围
   可能更大（例如 compaction 摘要与实际渲染缓存混放时）。未测"多 run 引用同一 digest"的
   更复杂引用图。
3. **`--artifacts-root` 之外的第二条删除路径**：我只审了 `sweep`。仓库里没有别处调用
   `path.unlink()` 于 artifacts 根（我做过 grep），但未做全量静态审计。
4. **哈希链的整段重算**（B-G3 边界）：我实测"改一条 + 从该条起重算全部哈希 → 校验器判
   `ok=true`"。这是无外部锚点的哈希链的固有性质，`semantics.md` 也写了"链不阻止篡改，
   只让篡改无法静默"——但**整段重算就是静默的**。若要让这句话严格成立，需要外部锚点
   （把最新 event_hash 写到日志之外的地方），本包没做，也未列为开放问题。
5. **噪声人格的统计强度**：3 个 seed 下 `strict<cause_only` 方向稳定，但幅度在
   0.089–0.135 之间波动；我没有做配对 bootstrap 去判"某一次噪声重跑的差值是否显著"，
   也没有验证 `--noise-flavor wrong_diagnosis` 单独运行时的口径行为。
6. **CI 未在真实 GitHub runner 上跑过**：我读的是 `ci.yml`/`nightly.yml` 的配置（作业、
   命令、marker、路径都对），但没有观察过一次真实 CI 运行（本地无 GitHub remote 之外的
   执行环境），因此"CI 会绿"仍是推断而非证词。

---

## 6. 证据索引

全部原始产物在 `/tmp/w9-evidence/`（仓库只留本报告与 `commands.sh`）。

| 文件 | 内容 |
|---|---|
| `pytest_baseline2.log` | 全量测试（315 passed, 1 skipped） |
| `facts_verify.log` / `facts_verify2.log` / `facts_nightly.log` | claim 门禁 27/27 与 45/45、退出码 |
| `inject1_readme.log` | 注入①（改 README 数字）→ 仍 27/27 通过（P1-2） |
| `inject2_reportsjson.log` | 注入②（改 `reports/*.json` 字段）→ 退出 1，报 625 vs 624 |
| `inject3a_tol_tight.log` / `inject3b_tol_vacuous.log` | 注入③（容差即灵敏度：收紧→红，放宽到 1e9→绿且表格同时打印 0.673812/0.573812） |
| `inject4_emptypath.log` | 注入④（置空 `source_path`）→ 退出 1 |
| `claim_sampling.log` | 17 条 claim 的成色抽查（双路径互证） |
| `fuzz_calib.json`, `fuzz_seed_repro.log`, `fuzz_otherseed.log`, `fuzz_sabotage.log`, `fuzz_sabotage_a2.log` | 标定复现、seed 逐位可复现、换 seed、标定改坏的两种结果 |
| `oracle_synthetic2.log`, `oracle_synthetic.py` | oracle 合成库 10 例（含 P2-3） |
| `probe_sigterm.log/json`, `probe_tornwrite.log/json`, `probe_extra_ledger.log/json` | 四档谱系探测器的原始输出 |
| `noisy_rerun.log/json`, `noisy_seed_stability.log`, `noisy-seed-{202,303}.json` | 噪声口径复跑 + 三 seed 稳定性 |
| `mutation_run.log`, `mutation_tamper.log` | mutmut 真实跑（354 s，同基线数）+ 删基线→退出 1 |
| `replay_consistency.json`, `cassette_tamper.log`, `cassette_detect.log`, `cassette_text_boundary.log` | 一致性、四类 cassette 篡改、文本篡改边界 |
| `replay-chaos/`, `replay_killwindow2.log` | replay 模式下的两种注入 |
| `argpolicy_probe.log` | 参数级审批的 8 组绕过尝试 |
| `chain_verify.log`, `chain_rechain.log` | 哈希链独立重算 / 改历史 / 删段 / 分叉 / 整段重算 |
| `trace_before.txt`, `trace_after.txt`, `otlp_nosdk.txt` | OTLP：交付前 vs 交付后逐字节比对、缺 extra 的失败 |
| `gc_shared_root.log`, `gc_sources.log` | GC 的共享根误删（P1-3）与四类来源正面用例 |
| `lease_probe.log` | 双进程租约 8 项 |
| `main_rerun.json/md/log`, `matrix_rerun.json/md/log` | 非回归的两份新跑产物（与入库逐格对比） |
| `oracle_synthetic2.py`, `gc_shared_root.py`, `gc_sources.py`, `chain_verify.py`, `argpolicy_probe.py`, `lease_probe.py`, `cassette_tamper.py`, `fuzz_sabotage.py`, `claim_sampling.py` | 本轮自写的探针脚本（全部只读仓库、产物写 `/tmp`） |

`commands.sh` 是上面这一切的可执行版本（按 §2/§3 的顺序逐条跑）。

---

## 7. 与作者声称的差异

作者声称来自 `docs/design/2026-09-17-delivery-report.md`（我在完成全部验证后才读）。

### §1 目标表

| 声称 | 我的核对 | 差异 |
|---|---|---|
| A-G1：72 条 claim；`check_facts`；CI job `facts`；`test_facts_gate.py`(15) | 72 条 ✓（我数的）；脚本行为 ✓；CI 配置 ✓；用例数 15 ✓ | **无差异**（但"文档里的数字从此有门禁"的覆盖面比标题暗示的窄，见 P1-2） |
| A-G2：180 次注入、0 新类违例、敏感性通过 | 交付 JSON 180/180 命中、180 个 `time_hit` marker、0 异常 ✓；我换 seed 复跑同样绿 ✓ | 无差异；机制侧补一条 P1-1 |
| A-G3：4 档、结论见 `fault-spectrum.md` | 四档结论全部复现 ✓ | 一处措辞：P2-4 |
| A-G4：strict 66.7% < cause_only 77.6% | 复跑同值（0.6667 / 0.7760），192/格 ✓，三 seed 方向稳定 ✓ | 无差异 |
| A-G5：`[tool.mutmut]` + 门禁 + 基线 1670/625/0.3774 | 真实 mutmut 跑出**同数** ✓；删基线即红 ✓ | 限时口径 P2-5 |
| B-G1：`identical: true`；三模式同一条 loop | ✓（含删除条目的可读拒绝） | P2-6、P2-7（失败语义的两个出口不完整） |
| B-G2：`ArgPolicy` + 执行前闸门；12 例 | 12 例 ✓；两条路径都被闸住 ✓ | P2-8（`allowed` 的布尔等价） |
| B-G3：schema v3 + `verify_chain` + `audit_chain`；"mirror test 定位到 seq=4" | 我按文档文字独立重算逐条一致 ✓；改历史→`first_break.seq=5`（不同 run，位置自然不同）✓ | 无差异 |
| B-G4：`[otel]` extra；默认不变 | 逐字节一致（1351 B）✓；无顶层 otel import ✓ | 无差异 |
| B-G5：`sweep` + CLI（dry-run 默认） | dry-run 默认 ✓；只删孤儿 ✓ | **P1-3**（共享根误删） |
| B-G6：`lease.py` + `tests/test_lease.py`(9) + semantics §3 改写 | 9 例 ✓；8 项双进程检查全过 ✓；语义 diff 为纯新增 ✓ | 无差异 |

### §2 验收门

| 声称 | 我的核对 | 差异 |
|---|---|---|
| 72 条 claim；退化注入实测（改 value → 退出 1） | ✓ 我独立做了 4 类注入 | 三类设计内注入都成立；**改 README 不成立**（P1-2） |
| "--reasoner noisy 全量可跑（1 条降级门禁如实报红）" | ✓ 14 条中 1 条 `enforced:false` 的 `harness.correct[competent]` 红，退出码 0 | 无差异 |
| "180 次注入 0 新类违例；4 档探测器；nightly `chaos-fuzz`" | ✓ | 无差异 |
| "316 passed；`gate_summary {total:14, passed:14}`；crash_matrix 16 格 as-predicted" | 14/14 ✓；16 格/80 次/70 kill/0 violation ✓ | "316 passed" 应为 collected 316（312 passed + 1 skipped + 3 deselected），措辞偏了：P2-11 |
| B §5 各阶段门（`identical: true`、mirror test、`test_otel` 默认不变、sweep dry-run、semantics 同步） | 逐条 ✓ | 除 B-G5 的 P1-3 外无差异 |

### §3 测试与产物

| 声称 | 我的核对 | 差异 |
|---|---|---|
| 11 个新测试文件的用例数（15/15/5/11/17/3/12/11/5/9/5） | 逐文件用 `scripts/count_tests.py` 数：**全部相同** ✓ | 无差异 |
| 新增/再生的 reports 产物；"差异只允许计时噪声：数值逐格未变" | `w4_crash_matrix` 16 格逐格相同 ✓；`w6_eval` 8 格 rates 与 statistics/catalog 完全相同 ✓；`w4_context_cost`/`w7_sweep` 抽样 claim 值一致 ✓ | 无差异 |
| CI 作业变化（verify 增 `facts` + `pytest -m live`；新增 `nightly.yml` 三作业） | 配置逐条对上 ✓；`pytest -m live` 3 passed ✓ | 无差异 |

### §4 作者主动上报的 5 处"与设计文档不符"

| 声称 | 我的核对 |
|---|---|
| ① `--kill-after-ms` 必须先标定（本机 180/35 ms） | ✓ 我在本机标定出**同样的 180/35**，并复现了标定的必要性（也复现了它不够强的那一面，P1-1） |
| ② 窗口 2 缝隙是微秒级、由确定性注入证明 | ✓ 敏感性自检确实报出 `inv_effect_accounting` |
| ③ 卸载产物是派生缓存、不在事件 payload 里 | ✓ 我跑出真实 run：artifacts 目录有文件，而 `referenced_digests` 返回 0 —— 与"派生缓存"口径自洽 |
| ④ mutmut 的 `--all=true`、`executescript` 隐式 COMMIT | ✓ 代码注释与实现一致（`mutation_check.py` 的 `--all=true`） |
| ⑤ README 的 W7 段混用两行阈值、已统一并给 0.85 行登记 claim | ✓ README 现在用的是选中阈值行的数（$0.11834/+79%/+41%/+38%），且 claim `w7-row-085-avg-cost-usd` 存在 |

### §5 遗留与建议

与我的发现一致的两条：**变异基线"每次改被变异模块都要重算"**（我实测同数、可重算 ✓）；
**六个分支待合并**（我确认 `main` 不含本交付，故本轮以 `docs/w9-backfill` tip 为被测对象）。

---

## 8. 与并行架构评审的交叉验证（结论冻结之后才读）

审计期间（19:23 之后）发现**同一工作目录里有另一个会话**在做 W9 架构评审，并在
`fix/review-p0-p1` 上修复。按"先冻结自己的结论再读答案"的纪律，本节写在本报告 §0–§7
完成之后；下表**只列我亲自复跑过的**对照项：

| 对方的条目 | 我的独立结论 | 一致性 |
|---|---|---|
| P0-1 变异门禁"什么都没跑出来"时是绿的 | 独立复现（把 `run_mutmut` 换成直通 → `exit 0` + "没有新增幸存变异"） | **完全一致**，我也判 P0 |
| P0-2 文档变异数字与基线矛盾 + facts 门禁没覆盖 | 我同样发现这两件事（我的 P2-1 与 P1-2），并给出"改 README 数字不触红"的最小复现 | 事实一致；分级我原本偏保守（P1/P2），详见下段方法学差异 |
| P1-1 租约承诺缺"无生产接入点"限定 | 独立验证：非测试代码里零调用点 | 一致（我的 P1-4） |
| P1-2 `sweep` 只扫单 run 目录 | 我独立造了共享 artifacts 根的最小复现，数据确实被删 | **完全一致**（我的 P1-3） |
| P1-3 `--skip-sensitivity` 把"没有证明"写成"通过" | 我也复现了 | 事实一致（我的 P2-10，分级不同） |
| P1-4 claim 覆盖靠人工登记 | 与我的 P1-2 同因 | 一致 |
| （对方未列） | 我另有：fuzz 假绿（P1-1）、oracle `inv_log_readable`（P2-3）、replay 指纹警告无出口（P2-6）、cassette 文本无校验（P2-7）、torn write 措辞（P2-4）、变异限时口径（P2-5）、`allowed=[1]`（P2-8）、`never_deleted`（P2-9） | —— |

**方法学差异（值得记录）**：对方把门禁/文档层的问题也判 P0（"不修不能合"），我最初按本仓库
既有分级把它判为 P1/P2——差异在于"P0 = 结论被证伪"里"结论"算不算门禁自己的输出。
读完对方的 P0-1 证据后我接受了更严的口径并据此修订本报告：**一条会假绿的门禁，其输出
（"盲区没有扩大"）本身就是一条被证伪的结论**，因此该判 P0；而 P1-2（文档正文不参与对账）
是**覆盖缺口**，其输出没有撒谎，判 P1 更准确。两条口径下结论一致：**这两类都必须在合并前修**。

## 9. 复现方式

```bash
bash docs/independent-test-2026-09-17/commands.sh          # 逐条复跑本报告的全部检查
```

脚本会在 `/tmp/verify-w9` 建隔离克隆与 venv（**不触碰被审计的仓库**），
把全部原始证据写到 `/tmp/w9-evidence/`，并在最后打印逐项 PASS/FAIL。
