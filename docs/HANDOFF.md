# 项目状态与交接（W8 开工前）

**这份文档是"压缩后的工作上下文"**：对话再长、上下文再挤，只要这份文件在，
W8 以及之后任何人接手都能从这里重新展开。细节不在这里——只放**不可丢失的事实与决策**，
每条都指向可以现场读的证据文件。

更新时间：W11（独立验证 2026-09-19：append-only 的 INSERT 旁路 + DDL 防线 + 落盘顺序 + 门禁指标定义覆盖面）+ 一轮拆包（评测层 `opsenv/suite.py` / `opsenv/systems.py` 拆成包，纯代码搬移，见 §十一）｜ 代码量见 `docs/HANDOFF.md` 的复现命令一节（`find ... | xargs wc -l`）｜ 测试全绿：**用例数不写在这里**，见 claim `tests-collected`（`scripts/count_tests.py` 再生）

> **W9 先读这一段**：本轮把"结论可再生"从纪律变成了门禁——文档里每个被引用的数字都登记在
> `reports/documented-facts.json`（claim 清单，条数以 `check_facts.py --list` 为准），
> 由 `scripts/check_facts.py` 逐条重跑比对
> （CI job `facts` 跑轻集、`nightly.yml` 跑整量集）。改代码导致数字变化时，先跑这条命令。

> **W10 先读这一段**：本轮做了两件事——(1) 外围集成（MCP 工具服务 + 可观测接收器，见 §九）；
> (2) **变异门禁的判据修复**（两条 P0 由独立验证证伪：缺项被当好消息、对新增未覆盖代码不敏感）。
> 动变异门禁、动 `integrations/`、或要引用"变异存活率"之前，先读 **§九的"已知盲区"**——
> `survivor_rate` 不是覆盖率，且单条变异体的判决不承诺稳定。

> **W11 先读这一段**：独立验证（2026-09-19，报告见 `docs/independent-test-2026-09-19/report.md`）
> 证伪了一条**语义承诺**、并指出门禁的一个结构性盲区，两者都已修：
> (1) **append-only 不是"物理上禁止改写历史"**——SQLite 的 `INSERT OR REPLACE` 走"删+插"，
> 而隐式 DELETE 不触发 `BEFORE DELETE` 触发器（`recursive_triggers` 默认 OFF），
> 于是普通 DML 就能把已提交事件换掉。修法是第三条触发器（`BEFORE INSERT` 守卫），schema 升 v4；
> (2) **门禁只断言"比率等于多少常数"**，把指标定义改掉可以让它们全绿——新增四条**关系**门禁
> （指标定义 / 新破坏性动作的对称自检 / CRN）；
> (3) **append-only 原先没防 DDL 路**——一句 `DROP TRIGGER` 就能让后续 `UPDATE` 畅通。
> 现在有四层：触发器（DML）、连接层 authorizer + `DBCONFIG_DEFENSIVE`（防）、
> 追加前的逐字核查与 `setup()` 默认拒绝（检测）、`audit_chain` 的 `guard` 字段与
> `verify_append_only_guard`（离线）。**残余边界写在 `docs/semantics.md` §2.1**：
> 拿到库文件的人仍可另开无防线连接，链没有密钥。
> 改 `harness/store/schema.py`、`harness/store/guard.py`、`harness/store/sqlite_store.py`、
> `harness/execution.py` 或 `opsenv/suite/gates.py::check_gates` 之前，先读 **§十**。
> 另外：`docs/semantics.md` §2.1 与 §2.4 的措辞已按实测收窄。

> **动 `opsenv/` 的目录结构或 import 面之前先读这一段**：评测层已按职责拆成包
> （`opsenv/suite/` 五个模块 + 门面、`opsenv/systems/` 六个模块 + 门面），
> **纯代码搬移、无语义改动**——门面把拆分前的命名空间一个不少地 re-export，
> 旧 import 路径与 `python -m opsenv.suite` 全部继续可用。要加新代码就加到对应子模块，
> 不要往门面里塞逻辑。理由、证据与"已接受的体量离群点"见 **§十一**。

---

## 一、这个东西是什么（一句话 + 三个数字）

**面向崩溃一致性与治理语义的 Agent runtime 实验台**：自研精简内核作为被测对象与测量仪器，
用真实 `SIGKILL` 做崩溃注入，用四系统对照做架构消融，用配对统计与门禁保证结论站得住。

* 64 个合成故障场景 × 4 条技术路线 × 2 个推理器人格 = 1536 次运行的对照实验；
* 6 个命名崩溃窗口 × 16 格 × 5 次 = 80 次运行，其中 **70 次注入真实 SIGKILL**
  （`(tamper)` 与 `(long-baseline)` 两个对照格按设计不注入；W3 那轮 70 次运行中是 65 次）；
* 8 个长任务变体 × 6 档压缩阈值，产出三维成本曲线（入库产物 80 个 cell：dev 72 + holdout 只在 off 与选中阈值各 8）。

---

## 二、硬事实（不要从记忆里改这些）

| 项 | 值 |
|---|---|
| 提交 | W1 `b42eb6f` → W2 `4122c62` → W3 `0f66913` → W4 `a993f39` → 审计 `d90811c` → W5 `6438ac5` → W6 `35b9b31` → W7 `1268ff6` → W8 `ac651fe`；W9 见 §八（六个里程碑各自一个分支，待 PR 合并） |
| 环境 | Python 3.14.6（声明 >=3.11，`uuid7` 有 uuid4 回退）、pydantic 2.13.5、langgraph 1.2.11（仅 `[eval]` extra） |
| 测试 | 用例数见 claim `tests-collected`（`scripts/count_tests.py` 再生；默认集已过滤 `live` marker 的用例，CI 用 `pytest -m live` 单跑其中的反例）；`tests/test_audit_regressions.py` 是审计回归 |
| 六条命令 | `pytest` / `crash_matrix --repeats 5` / `context_cost` / `opsenv.suite --gate` / `context_sweep --repeats 2` / `scripts/check_facts.py`（README「如何复现全部结论」一节） |
| 报告 | `docs/semantics.md`（承诺清单）+ `docs/w2..w7-report.md` + `reports/*.md`（表格入库、明细走 `--runs-out` 不入库） |
| 产出物 | `harness/`（内核 ~3.9k 行）、`opsenv/`（场景与评测 ~1.7k 行）、`fakeworld/`（受控世界）、`experiments/`、`tests/`、`integrations/`（**外围集成**：MCP 工具服务 / 崩溃演示 / 可观测接收器，见 §九；独立 extra、内核不依赖它） |

---

## 三、口径规则（跨轮次必须一致，否则数字不可比）

1. **kill -9 ≠ 掉电**：W2–W4 的崩溃结论只对 `kill -9` 成立（`synchronous=NORMAL` 下掉电会丢最后一个事务）。
2. **W2–W4 与 W5–W7 是两套样本量口径**：前者每格 n=5（点估计，0/5 的失败率上界约 60%），
   后者报 Wilson 区间 + 配对 bootstrap（n=192）。**不要混引**。
   **最小可检测效应有三个数字，引用时必须写明口径**（各自一条 claim，都在
   `reports/w6_eval.json` 的 `statistics` 里）：
   **配对口径（80% 功效）约 9.2%**（解释配对结论只能用这一个）、
   独立两样本口径（p=0.5）约 10.0%（两臂不配对时的粗口径）、
   独立两样本口径在 p≈0.9 时的 95% CI 半宽约 6.0%（**没有功效项，不能当 MDE 引用**——
   早期版本就是拿它当"最小可检测效应约 6%"引用的，既漏了功效项、又误用了独立公式）。
3. **成本口径**：W4 用本地缓存模型（前缀匹配/TTL/1.25× 写溢价/0.1× 读折扣）；
   W5–W7 的横排对照**不计缓存折扣**（否则会把两件事混在一起）。
4. **场景全为合成**：没有真实流量、真实事故或公开 Postmortem 数据集；分类学参考公开故障模式。
5. **推理器是受控变量**：四条路线共用同一推理器，因此差异只能来自架构。
   它的取证策略是理想化的（看到决定性证据就停）。
6. **质量不可测**：脚本模型无法衡量"回答好不好"；"完成率" = 任务能否跑完（不溢出）。
7. **禁止"生产级"字样**；所有结论相对基线、带区间或明确标注"点估计"。

---

## 四、七轮结论（每条一句话，细节在对应报告）

| 轮次 | 结论（一句话） | 证据 |
|---|---|---|
| W1 | 事件日志是唯一权威：扁平 append-only + 物理禁止改写 + 状态=日志折叠 | `docs/semantics.md` §2 |
| W2 | "效果已发生、记录未落盘"是唯一会丢一致性的窗口：下游不幂等时重复 5/5，runtime 侧去重无效 | `w2-crash-windows.md` |
| W3 | outbox + 探针把重复 5/5 → 0/5；无读回时收敛为恰好 1 行 unknown；gate 的价值是"把决定权交给人" | `w3-report.md` |
| 审计 | 5 路只读审计发现 4 个 P0（视图自毁配对、分叉复用幂等键、异常穿透、压缩越压越大）+ 文档承诺背离 | `w4-report.md` §六 |
| W4 | 缓存纪律值 2 倍成本（+105.1%）；卸载是唯一纯赚杠杆（-66% token）；压缩与缓存冲突被量化 | `w4-report.md` |
| W5 | 取证充分性决定正确率上限（50% vs 100%）；无 gate 的路线在 20.8% weak 场景执行破坏性动作，有 gate 的 0%；三条读证据路线诊断同分（CRN 修复后 90.1%/61.5% 逐格相同） | `w5-report.md` |
| W6 | "谁更准"不可区分到零差异（配对 CI [0,0]），"谁更安全"显著（CI [−0.266,−0.151]）；代理指标须先校准（κ）；门禁要能抓"静默丢弃" | `w6-report.md` |
| W7 | 压缩买的是完成率不是省钱（净成本 +79%）；压缩次数与缓存命中聚合反向、细胞级非单调；0.70/0.85/0.95 逐格相同（程序选点 0.95 属并列），要避免 ≤0.50 与不压缩 | `w7-report.md` |

---

## 五、未决与已知边界（W8 不要顺手"修"掉它们，要么做要么写清）

1. 掉电语义仍未覆盖（只做探测器：torn write / SIGSTOP / SIGTERM，见 `docs/fault-spectrum.md`）；
   多进程并发**仍不承诺**，但租约有了最小实现（`harness/lease.py`：持有/续租/过期失活 +
   可读失败；**过期接管仍开放**）。`state_update` 仍是"已登记未实现"。
2. 评分口径敏感性**已被激活**（W9：噪声人格 `--reasoner noisy`，strict 66.7% < cause_only 77.6%，
   见 `docs/noisy-reasoner.md`）；但它用的是**受控扰动**而不是真实模型，真实模型的错法分布仍未知。
3. 压缩扫描只覆盖阈值，未扫 `keep_recent_groups` 与摘要模型选择；摘要用确定性桩。
4. `artifact` GC 已实现（W9：`python -m harness.artifacts --run-dir X [--apply]`，
   默认 dry-run，只由显式 CLI 调用）。
5. W6 的 holdout 尚未被真正当考卷（无调参过程）；W7 的 holdout 每档 n=4，仅方向确认。
6. agent 路线（harness/langgraph）的**取证充分性代理指标无方差**，不能当线上监控指标（κ 不适用）。
7. **变异门禁的已知盲区**（W10 新增，完整记录见 §九）：不可见空间 18/2050 = **0.9%**；
   **单条变异体的判决不承诺稳定**（实测 `_replay_outcome__mutmut_37` 随负载在 survived ↔ killed 翻转；
   W11 在同一台机器上连跑两遍全量**没有出现翻转**，见 §十——所以这是"不承诺"，
   不是"已知会红"）；`[tool.mutmut].only_mutate` 之外的代码不参与变异，
   门禁不红是**范围外**而不是漏检。引用"变异存活率"前先读这三条。
8. `integrations/` 的 MCP 服务是**本地单用户**形态：不承诺并发、多用户、远程访问、断线重连时序，
   也不承诺任何性能数字（`docs/integrations.md` §6）。租约**没有**接进 MCP 写路径——
   与 `semantics.md` §3 一致，后果由外部账本裁决。

---

## 六、复盘：做对了什么、踩了什么、重来会怎么改

### 做对的（值得保留成习惯）

1. **先写语义承诺，再写代码**：`semantics.md` 是承诺清单，代码与测试都以它为口径；
   审计能逐条对账，正是因为承诺是显式的。
2. **把"自证"当敌人**：场景来源声明、holdout 冻结、评分口径敏感性、代理指标 κ、
   配对 CI、"实验假设自检"门禁——每一条都是在防"自己给自己出题自己判卷"。
3. **用真实故障注入而不是 mock**：真 SIGKILL 子进程 + 外部副作用账本做裁判；
   任何 runtime 自己的日志都不作为"效果发生了几次"的证据。
4. **每轮留失败复盘**：七轮里六次翻车都写进了对应报告（见下），这是最像真实工程的部分。
5. **审计前置**：在写 W5 之前先做 5 路独立审计 + 修复，避免在错误地基上盖楼。

### 踩过的坑（模式化教训，按发生顺序）

| # | 事故 | 教训 |
|---|---|---|
| 1 | W2 报告用点估计（n=5）宣称结论 | 小样本必须报区间，或明说"不可区分"（W6 补齐） |
| 2 | W3 `SCOPE_SESSION` 未导入，被 `or` 短路掩盖，全套测试仍绿 | 短路会让分支不可达；lint（F821）+ 场景级用例是唯一防线 |
| 3 | W4 为排版缩短一句日志文案，E2a 从 failed 变 completed，**结论翻面** | 涉及阈值的实验，输入体量必须写成实验变量 |
| 4 | W5 一次脚本化 patch 4 处替换里 3 处静默失败 | **脚本化改动必须逐条 assert**（这条后来在 W7 又犯了一次） |
| 5 | W5 中英文引号混用截断字符串；一次"修复"又吃掉 14 处 docstring | 中文文本里统一用「」；改引号必须过 `py_compile` |
| 6 | W5 `is_write_action` 只认合法动作 ⇒ 破坏性动作既不过 gate 也不执行，指标失去意义 | 基线写错会让对照整个失效；"未注册"≠"被拦住" |
| 7 | W6 第一版门禁抓不到上述退化的返回（静默丢弃） | **"没有坏事发生"≠"机制在工作"**：要检查"该拦的时候真的拦了" |
| 8 | W7 `Interval` 是 dataclass，没有 `model_dump()`，写 JSON 抛异常且被管道吞掉 | 输出被重定向时，退出码要单独看；序列化错误要早暴露 |

### 第九次事故（W5–W7 审计）

审计抓到 3 个 P0，都不是实现 bug 而是**评测本身的自证问题**：
① 跨系统比较被种子污染（system 名进了 hash → "langgraph 29.7% vs harness 20.3%" 是伪影）；
② workflow 基线的"弱"来自我们只给它两个通道（读满四通道的规则引擎实测 100% 正确、更便宜）；
③ gate 的 0% 红线与"值班人永远正确"这个假设是同义反复（橡皮图章下三条路线红线率完全相同）。
教训：**评测本身要按被审对象对待**——种子里不能有被测系统的名字、
基线不能靠"少给通道"来削弱、指标不能用 ground truth 去喂被判定的机制。

### 如果重来，会怎么改

* **W6 的东西应该更早出现**：统计口径与门禁如果在 W3 就建起来，W2/W3 的报告就不用返工；
  教训是"度量基础设施要和被测对象一起长"，而不是最后补。
* **场景集应该在 W4 之前就有**：W4 的成本实验只有一条长任务，
  阈值扫描到 W7 才做；如果一开始就有多任务变体，曲线会更早出现。
* **审计不止做一次**：W1–W4 之后的审计价值极高（4 个 P0），W5–W7 之后应该再来一轮
  （见下"W8 建议"）。
* 少写一点"看着漂亮但没被激活的检查"（如未被激活的口径敏感性）——宁可少而真。

---

## 七、W8 产出（已完成）

* 三篇长文：[docs/posts/01-crash-semantics.md](posts/01-crash-semantics.md)、
  [02-approval-semantics.md](posts/02-approval-semantics.md)、
  [03-cache-accounting.md](posts/03-cache-accounting.md)（每篇含实测数字与复现命令）；
* demo：`python -m examples.ops_demo`（告警 → 取证 → 审批 → 执行 → trace + 消融对照）；
* 简历措辞与面试问答映射：[docs/resume.md](resume.md)（含"不能写的词"与必须主动交代的边界）；
* 版本 0.8.0；README 定稿（架构图 + 文档索引 + 一条命令跑通）。

## 附：W8 之前的输入清单（保留备查）

**计划里的 W8 = 三篇长文 + 文档收口 + demo + 简历措辞。** 素材已经全部就位：

| W8 交付物 | 素材位置 | 要写的核心句 |
|---|---|---|
| 长文①崩溃语义 | `docs/w2-crash-windows.md`、`docs/w3-report.md`、`docs/semantics.md` §2/§3 | "runtime 对副作用只能承诺 at-least-once；至多一次要么靠下游幂等、要么靠 outbox+对账" |
| 长文②审批绑定 | `docs/w3-report.md`、`harness/approval.py`、`tests/test_audit_regressions.py` | "批准的是'某次调用 + 某组参数'；nonce 不是闩锁；gate 的价值是把决定权交给人" |
| 长文③缓存会计 | `docs/w4-report.md`、`docs/w7-report.md`、`harness/cache.py` | "缓存纪律值 2 倍成本；压缩与缓存互斥；阈值应贴近上限" |
| 文档收口 | `README.md` 已有"如何复现全部结论" | 补：项目定位一段话、架构图（ASCII/SVG）、边界声明汇总 |
| demo | `examples/w1_tour.py`（W1 演示）、`harness/trace.py`（trace CLI） | 一条命令跑通"告警 → 取证 → 审批 → 执行 → trace 回放" |
| 简历措辞 | 本文件 §三/§四 | "自研 Agent runtime 实验台 + 四系统消融 + 配对统计门禁"，**不写"生产级"** |

**W8 之前建议补的一次动作**：对 W5–W7 再做一轮独立审计（与 W1–W4 那次同规格），
重点查：评测口径的一致性、门禁是否真的覆盖了它声称的东西、报告数字与 JSON 是否逐格对齐。
理由：W5–W7 是新增的评测层，而上次审计之后新增了约 4 千行代码与 12 份报告。

## 八、W9：设计文档 A+B 的交付（本轮）

六个里程碑各自一个分支（堆叠，按 A→B 顺序），全部只推分支、由所有者在网页端 PR 合并：

| 里程碑 | 分支 | 交付 | 关键数字（可再生） |
|---|---|---|---|
| 开放问题裁决 | `docs/open-questions-answered` | A §4 四条 + B §6 五条 + 回写 B §7 | 见 `docs/design/2026-09-17-open-questions-answered.md` |
| A-M1 | `feat/facts-gate` | claim 再生门禁 + CI job `facts` | 72 条 claim；`check_facts --run verify` 27/27 |
| A-M2 | `feat/noisy-reasoner` | 噪声人格 + 变异测试常设化 | strict 66.7% vs cause_only 77.6%；变异基线见 claim `mutation-survivors`（`reports/mutation_baseline.json`） |
| A-M3 | `feat/chaos-fuzz-probes` | 随机时刻 SIGKILL fuzz + 三档谱系探测器 | 180 次注入、0 新类违例；3 档探测器各有结论 |
| B-M1 | `feat/record-replay-model` | scripted/record/replay 三模式 | scripted↔replay 白名单一致（`identical: true`） |
| B-M2 | `feat/arg-policy-hash-chain` | 参数级审批 + 事件哈希链 | arg_policy 12 例；链 mirror test 定位到 seq=4 |
| B-M3 | `feat/otel-sweep-lease` | OTLP + artifact GC + 租约 | 三块各有单测；语义文档四段同步 |

**新增的门禁**（都已做过退化注入验证）：`facts`（文档数字对账，verify + nightly）、
`mutation`（幸存变异防倒退，nightly）、`chaos-fuzz`（随机注入 + 谱系探测器，nightly）。
`verify` 主作业仍然只跑 `pytest` + `ruff` + 轻量 claim 集。

### 评审修复（`fix/review-p0-p1`，2026-09-17）

W9 交付经过一次架构评审（`docs/design/2026-09-17-architecture-review.md`：2 个 P0 +
3 条阻塞性 P1 + 26 条 P2），修复分支处置如下：

* **P0-1**：`mutation` 门禁在"什么都没跑出来"时是绿的（`mutmut results` 空结果退出 0
  且无输出 ⇒ 基线里的幸存变异被报告成"已被杀死"）。修法：`mutmut run` 非 0 即作废、
  变异体总数为 0 即失败，并补回归用例。
* **P0-2**：README/HANDOFF 的变异数字与入库基线矛盾而 facts 门禁没覆盖。修法：数字对齐；
  **并给 `check_facts.py` 加了"引用位置必须指向真实文件与逐字锚点"的校验**（P1-4），
  claim 的 `docs` 全部改写成 `路径#锚点` 并逐条核对。
* **P1-1**：`semantics.md` §3 的租约段读起来像"已接入写路径"（其实没有生产接入点）——措辞已收窄。
* **P1-2**：`sweep` 自称"全库扫描"却只扫一个 run 目录——引用枚举改为接受多个 run 目录，
  共享 artifacts root 时 `--apply` 拒绝执行（除非 `--force`），docstring 改成事实。
* **P1-3**：`--skip-sensitivity` 会把"没有证明"打印成"通过"——旗标已删除。
* P2 的 26 条里，与正确性/安全性相关的已一并修掉（写入侧链保护、oracle 三处统一、
  账本缺席判定、unknown 上界、崩溃探测器的信号与进程回收、成本回放口径、快照纪律去重…），
  逐条状态见修复分支的提交信息与测试。

---

## 九、W10：外围集成（设计文档 C）+ 变异门禁判据修复（2026-09-18）

### 交付了什么

| 交付 | 落点 | 一句话 |
|---|---|---|
| MCP 工具服务 | `integrations/mcp_server.py` | stdio、一进程一 run、`tools/call` 走 `harness/execution.py::ToolExecutor` 的**七步管线**；写操作停在审批门，`approve` 支持 reject 与改参（沿用既有 `__edit1` 语义）；**不依赖 elicitation** |
| 崩溃演示 | `integrations/mcp_crash_demo.py` | 真 SIGKILL 服务进程 → 重启 → 同一 run 续跑 → 外部账本恰好 1 次；**含对照组**（只关 outbox 就是 2 次） |
| 可观测 | `integrations/observability.md` + `otlp_local_sink` | 路径①本机接收器**已实测**；Jaeger / 任意 OTLP 后端**本机未实测**（无 Docker / 无凭据），如实标注 |
| 管线抽取 | `harness/execution.py::ToolExecutor` | loop 与 MCP 共用同一实现；"两处调用点行为一致"有差分 / 委托 spy / 结构扫描三条证明 |
| 变异门禁重写 | `scripts/mutation_check.py` + 基线 v2 | 六条红灯条件（新增幸存 / **判定→无结论** / **条目缺失** / **新增 no tests** / 结果集为空 / 不可见空间增长），**全状态记账** |

**文件与依赖边界**：`integrations/` 独立目录 + `[mcp]` extra（**不进 `dev`**）+ `mcp` marker（默认过滤）；
内核依赖仍只有 `pydantic`；依赖方向单向，由 AST 扫描用例守着（内核目录不得 import `integrations`）。

### 两轮独立验证（PR #13 与 #17）

* **语义与功能面未被证伪**：包括"MCP 服务进程 vs 进程内 `Loop` 的事件序列逐位相同"这条独立对拍。
* **变异门禁被证伪两条 P0**（"缺项被当好消息"、"对新增未覆盖代码不敏感"）→ 判据已重写、
  基线换 v2（逐变异体状态 + 不可见空间占比），并用**实跑**的退化注入验证"改坏必红"。
* `segfault` 误判的根因（45% 变异体没有判决）已定位并修掉：macOS 上
  `urllib.request.getproxies()` 会读 SystemConfiguration，**父进程热过之后 fork 出的子进程再调会 SIGSEGV**；
  mutmut 用 fork 隔离 ⇒ 判决记成 `segfault` 且不计入任何计数 ⇒ 真幸存变异从视野里消失。
  修法在 `tests/conftest.py`（`no_proxy=*` 让 `getproxies()` 在环境变量层短路）；
  崩溃机理与最小复现见 `docs/design/2026-09-18-mutation-segfault-investigation.md`。
* 逐条处置见 `docs/design/2026-09-18-review-response-integrations.md`。

### 已知盲区（**改门禁或引用存活率之前必读**）

1. **不可见空间 = 18/2050 = 0.9%**（修复前 45.1%）。`survivor_rate` = 0.3445，
   **不是覆盖率**——每次运行都会把这行打出来。
2. 那 700 条幸存变异里，相当一部分由**子进程驱动**的验证（崩溃矩阵 / 四系统评测）在外面兜着，
   mutmut 看不见；基线 `note` 写了这一点。
3. **单条变异体的判决不承诺稳定**：实测 `_replay_outcome__mutmut_37` 随机器负载在
   survived ↔ killed 之间翻转。门禁对"判决集合的变化"敏感，但对**单条的抖动**没有免疫力——
   夜里误报红灯的可能性存在。
4. `[tool.mutmut].only_mutate` 之外的代码不参与变异：注入到未变异模块的代码不会让门禁变红，
   那是**范围外**，不是漏检。
5. `mutmut print-time-estimates` 输出里的 `<no tests>` 是**耗时估计占位**、不是状态标签。
6. **判据的强度只到"已覆盖代码的盲区倒退"**：`no tests` 只在**新增**时红灯；
   基线里既有的 18 条 `no tests` 是已知的未覆盖面，不是门禁的承诺面。
7. **"等量改写"这条残余边界已被内容指纹堵上**（2026-09-26，见 §十二）：基线现在为
   **每条**变异体存 `sha256(规范化 diff)`（v3 schema），同名不同指纹报
   `[mutant-content-changed]`。残余边界变成：指纹只覆盖 `only_mutate` 里的四个模块
   （与第 4 条同界），且**换指纹口径必须换算法标识名**（
   `fingerprint_algorithm`，否则新旧基线会被集体当成"内容变了"而误报）。
8. **`--update-baseline` 现在有两道默认拒绝**（同见 §十二）：一条指纹都取不到 ⇒ 拒绝写基线；
   候选比旧基线**更宽** ⇒ 拒绝写入（`--allow-wider-baseline` 显式放行，条件记进基线的
   `refresh.widening_override`）。**改代码导致变异体重编号时也属于"更宽"**（旧条目整条不见）
   ⇒ 那种刷新必须显式放行，这是有意的：刷新基线应当是个有意识动作。
   顺带一条实测教训：**测试不许碰仓库的 `mutants/`**——本轮的守卫用例第一版没把
   `MUTANTS_DIR` 指到 tmp，直接把正在跑的**全量刷新**的缓存搬走了，mutmut 父进程写
   `mutants/harness/loop.py.meta` 时 FileNotFoundError 退出 1（好在那条路径是
   "跑不起来 ⇒ 判失败"，不是静默绿）；现在 `_run_gate` 一律隔离到 tmp。

### 本轮之后仍需注意的

* `integrations/` 的 MCP 服务是**本地单用户**形态；不承诺并发/多用户/远程/断线重连时序，
  也不承诺性能数字（`docs/integrations.md` §6）。租约**没有**接进 MCP 写路径。
* nightly 的 mutation 作业预算已从 25 分钟放宽到 **45 分钟**——这是**两个数字，别混引**：
  `nightly.yml` 里 job 的 `timeout-minutes: 45` 是 GitHub 给出的**墙钟上限**
  （连 checkout / install 一起算），脚本参数 `--timeout 2400` 是 mutmut 运行自身的预算
  （= 40 分钟，与 `docs/testing.md` §2 同口径）。放宽的理由：修掉误判后原先"一进去就崩"
  的那部分变异体会真的跑完，**本机空闲冷跑实测 631.5 秒**。
  这是预算调整，不是判据放宽（两种情况下的超时都判失败）。
  **CI 侧实测（2026-09-26 补，带 run id）**：nightly run `36112304447`（2026-09-25，
  ubuntu-latest，`--max-children 4`）job 墙钟 **21 分 11 秒**（含 checkout/install）、
  `Mutation gate` 步骤 **20 分 52 秒**，两项预算都还有约 2 倍余量；变异子进程自身的耗时
  从下一轮 nightly 起读 `--json-out` 的 `elapsed_s`（本包新加的落盘字段）。

---

## 十、W11：独立验证（2026-09-19）

完整报告：[`docs/independent-test-2026-09-19/report.md`](independent-test-2026-09-19/report.md)
（含最小复现脚本与未处置清单）。结论一句话：**宣传与已完成的功能总体相符**
（基线四件套先跑绿：429→当前用例数见 claim、`ruff` 净、`facts --run verify` 44/44、
崩溃矩阵 16 格 as-predicted），但独立验证抓到 **3 条 P0**，共同形态是
**"门禁全绿、测试全绿、文档齐备，但它们抓不到自己"**。

### 修了什么

| 缺陷 | 一句话 | 落点 |
|---|---|---|
| P0-1 append-only 被绕过 | `INSERT OR REPLACE` 走"删+插"，隐式 DELETE 不触发 `BEFORE DELETE`（`recursive_triggers` 默认 OFF）⇒ 普通 DML 就能改历史，§2.1 被证伪 | 第三条触发器 `events_no_insert_over_existing`、schema **v4** + `MIGRATIONS[4]`、`tests/test_event_log.py` 4 例、`semantics.md` §2.1 |
| P1-2 落盘顺序倒置 | `_resolve_pending` 的 APPLIED / 两个 unknown 分支与 `_handle_tool_failure` 都是"先闭合去重行、后写事件"，真 SIGKILL 会留下**被自家 `check_closed_calls` 判违规**的状态（§2.5 第 8 步写的是反过来） | `harness/execution.py` 四处 + 模块 docstring 固化纪律；真子进程 SIGKILL 回归用例 |
| P0-3 门禁只断言比率 | 13 条门禁断言"某比率 == 某常数"，把**指标定义**改掉（`is_sufficient` 恒真 / 摘掉"新动作" / 种子写回 system 名）可以全绿而结论失效 | 四条**关系**门禁：`workflow.correct==workflow.sufficient`、`gated_routes.novel_red_line==0`、`single_shot.novel_red_line>0`、`crn.evidence_routes_agree`（四组退化注入已实测） |
| P1-3 W7 成本数字不再再生 | 入库产物是旧代码的产物；9 条 claim 只在 nightly 对账 ⇒ push 上没人看得见 | 重生产物 + 回灌文档；9 条 claim 的 `run` 从 `nightly` **升到 `verify`**（facts 作业因此多约 38 秒） |
| P1-4 README 的 W3 括注过期 | "14 格 × 5 次 = 70 次运行、65 次 SIGKILL" ⇒ 实际 16 格 / 80 次 / 70 次 | README / `pitch` / `resume` 三处对齐 |
| P1-5 mutation claim 不是再生 | `--summary-only` 只读回入库基线，永远抓不到漂移 | `what` 里如实标注性质 + 带上基线计数（不假装是再生） |
| P2-5 DDL 绕过路径未登记（**本轮补做**） | `DROP TRIGGER` / `ALTER TABLE ... RENAME` / `PRAGMA writable_schema` 都不在守卫范围内，§2.1 的"物理上禁止改写历史"只覆盖普通 DML | `harness/store/guard.py`（authorizer 拒 DDL + `DBCONFIG_DEFENSIVE` + `guard_report`）、`sqlite_store.setup/append_many` 的写前核查与 `allow_repair`、`audit_chain` 的 `guard` 字段、`verify_append_only_guard`、`tests/test_append_only_guard.py` 17 例、新增两条 `chain-mirror-*-guard-*` claim |

P2 十条（交叉校验范围、discordant 口径、三个 MDE 的物化、门禁量 pooling、DDL 绕过路径（**已补**）、
W4 claim 散文、README 末位与口径、w5/w6 的 CRN 修正前数字、`artifacts --help` 告警、
venv 入口脚本的旧路径）逐条状态见报告 §3 与 §5。

### 本轮之后仍需注意的（**改这几处前先读报告**）

1. **append-only 的 DDL 防线只保护本进程的连接**（P2-5 已补三层：authorizer + DEFENSIVE、
   写前逐字核查、离线核查）。**残余边界不变**：拿到数据库文件的人可以另开一条没有防线、
   没有触发器的连接，改完还能把触发器文本与 `PRAGMA schema_version` 一起凑成"看起来没被动过"
   的样子；链没有密钥，所以这不是密码学意义上的防篡改（§2.1 已写明）。
   真正不可越过的那条线是**"改写无法静默"**：链与离线核查会把痕迹留在审计里。
2. **门禁量仍 pooling dev+holdout**：报告分开报，判据没分。改它 = 改门禁语义，应单独一轮。
3. **README 里仍有一批没有 claim 覆盖的数字**（14 格 / 1536 / workflow 50% / κ=1.0 等）。
4. **变异基线已按 W11 的代码重刷**：`harness/execution.py` 改过 ⇒ mutmut 按**函数内序号**
   命名变异体，编号必然大范围变化。**刷新前必须先把陈旧的 `mutants/` 缓存整体移开再全量跑**——
   否则 `mutmut run` 走的是增量路径，会输出一份"看起来跑过了"的混合结果
   （本轮实测过一次：14.7 秒跑完、`no tests` 从 18 虚增到 241，那就是增量结果，不是基线）。
   重刷结果：`total 2050 / killed 1332 / survived 700 / no_tests 18`，不可见空间 18/2050 = **0.9%**，
   `survivor_rate` **0.3445**。
   顺带补了 §九第 3 条"判决不承诺稳定"的**实测**：同一份代码连跑两遍全量，
   **2050 条逐条判决零翻转**、门禁退出 0——所以那条是"不承诺"，不是"已知会红"。
   （改动前后基线的差异因为命名位移而**不可比**，不要拿旧基线的名字去对。）
   **这条纪律现在是代码而不是提醒**：`python -m scripts.mutation_check --update-baseline` 会自动
   把 `mutants/` 整体移开（改名成 `mutants.stale-<UTC 时间戳>/`，已 gitignore）再跑全量，
   并把这次的条件写进基线与 `--json-out` 的 `refresh` 字段；要沿用旧缓存必须显式
   `--allow-incremental-refresh`（会大声警告）。判据是"缓存干不干净"，不是 mtime——
   同模块里没改过的函数本来就该保留旧判决，只有"整体重跑"才谈得上基线。

---

## 十一、评测层拆包（纯代码搬移，无语义改动）

### 拆了什么、为什么

| 拆分 | 结果 | 为什么 |
|---|---|---|
| `opsenv/suite.py`（1096 行 → 包） | `opsenv/suite/`：`run.py`（跑批/聚合）、`stats.py`（评分口径与配对统计）、`gates.py`（门禁）、`report.py`（渲染）、`cli.py`（命令行）+ 门面 `__init__.py` + `__main__.py` | `check_gates` 是门禁核心（W11 的四条**关系**门禁都在里面），却与渲染、CLI、聚合挤在同一个 1096 行文件里——读它要跨过几百行无关代码 |
| `opsenv/systems.py`（876 行 → 包） | `opsenv/systems/`：`base.py`（共用底件）、`workflow.py`、`single_shot.py`、`langgraph_system.py`、`harness_system.py` + 门面 | 四条路线、LangGraph 图定义、策略模型混在一起，"这条路线做了什么"要读完整文件才能回答 |
| 两处 `_agent_output_tokens` / `_run_state` **重复定义**（同一文件里各两份，函数体逐字相同） | 删除第二份（保留紧邻唯一消费者 `run_harness` 的那份） | 第二份**静默遮蔽**第一份：改第一份不生效，用例与门禁全绿、无人报警。现在由 `tests/test_no_duplicate_defs.py` 钉死（`opsenv/` 与 `harness/` 顶层同名重复定义即失败，已做过退化注入自证） |

### 门面（`__init__.py`）的约定

拆分前 `opsenv.suite` / `opsenv.systems` 命名空间里的名字（含它们自己 import 进来的类型与
私有辅助）**一个不少**地 re-export，因此旧 import 路径与 `python -m opsenv.suite` 全部继续可用。
唯一的例外是有意保留的间接层：`PROFILES` 定义在 `opsenv/suite/run.py`，但 `noisy_profiles`
与 `main` 通过 `run._live_profiles()` **在调用时**从门面读——`docs/independent-test-2026-09-17/commands.sh`
用 `opsenv.suite.PROFILES = ...` 换 `noise_seed` 来证明噪声口径可复现，子模块各持副本会让
这个赋值**静默失效**。

### 布局上的一处刻意偏离（与任务给的草案不同，理由是依赖方向）

`_finish`（把环境状态折成一个 `RunResult`）放在 `systems/base.py`，草案把它列在
`harness_system.py` 下。四条路线都要调它，放在 agent 路线那个模块里会让
`workflow` / `single_shot` / `langgraph` 反向 import 它——依赖方向变坏，而且读规则基线的
人得先看懂 agent 路线。其余布局与草案一致。

### 零行为变更的证据（可复跑）

`git archive` 出拆分前的树到 `/tmp` 跑同一批命令，与本树对比：

| 对照 | 结果 |
|---|---|
| 套件整跑 `--per-fault 2 --repeats 1`（JSON + Markdown） | **逐字相同**（仅 `avg_wall_ms` 计时列不同） |
| 五条路线（含反事实基线 `rule_full`）× 2 场景 × 2 值班人（oracle / 橡皮图章）× 2 重复的 `RunResult` | **逐字相同** |
| `opsenv.suite.PROFILES = <seed 202>` 后 `noisy_profiles` 读到的 `noise_seed` | 两边都是 `202`（间接层确实生效） |
| `--per-fault 0` 退出码 / 正常路径退出码 | 两边都是 `2` / `0` |

### 已接受的体量离群点（**不要顺手拆**）

* `harness/execution.py`（832 行，`wc -l` 现测）：七步管线的实现确实大，但它**在变异范围内**
  （`pyproject.toml [tool.mutmut].only_mutate`）。拆它 = 变异体按函数内序号重编号 =
  W11 刚重刷的基线整份不可比，而 W11 刚为落盘顺序（P1-2）改过它。拆分收益配不上基线作废的代价。
* ~~`scripts/mutation_check.py`（754 行）~~ → **已拆**（2026-09-26）：当时记的条件是"拆它要先补
  一遍判据的回归用例"——本届先补（第 4/5/6 项：墙钟落盘、内容指纹、更宽即拒绝守卫，各带用例与
  退化注入），再按与 `opsenv/suite/` 同一规格拆成 `scripts/mutation_check/` 包（config / status /
  fingerprint / baseline / gate / reporting / runtime + 门面 + `__main__.py`）。
  拆前先做了**逐字切片 + 拼接回原文逐字相同**的断言（见提交信息），拆后跑了
  `--summary-only` 与整轮门禁的**逐键对照**：除 `elapsed_s` 的计时噪声外完全相同。

### 本轮之后仍需注意的

1. **`mutants/` 缓存是"旧代码副本"这件事又咬了一次**：新用例读 `opsenv/` 与 `harness/` 的源码，
   而沙箱里那份 `opsenv/systems.py` 还带着本次删掉的重复定义 ⇒ 局部变异运行会把**每个**变异体
   判成 killed（存活率只会显得更低，**门禁不会因此变红**，属于静默失效）。已按既有纪律把缓存
   整体移开（`mutants.stale-<UTC 时间戳>/`，已 gitignore），下次变异运行全量重建。
   判断依据仍是"缓存干不干净"，不是 mtime。
2. **历史记录保持原样**：`docs/independent-test-*` 与 `docs/design/2026-09-*` 里的
   `opsenv/suite.py` / `opsenv/systems.py` 字样不改——那是当时那次运行/评审的记录（含当时的行号），
   改路径等于篡改证据。活文档（README / HANDOFF / `docs/*.md` / `reports/documented-facts.json`
   的 claim 锚点）已全部回灌，`scripts/check_facts.py --run verify` 是它的门禁。
3. `opsenv/` 仍**不在**变异范围内（`only_mutate` 只有 harness 的 4 个文件）。也就是说拆包本身
   没有变异覆盖，覆盖它的是 `tests/test_no_duplicate_defs.py` 这类结构断言与上面那批逐字对照。

### 补记：cross-job claim 漂移（2026-09-26，形态与规矩）

**形态**：同一条**结构性事实**在两处 claim 里各存一份，改代码时只更新了一份。
实例：W11 把门禁从 14 条加到 18 条，`suite-gate-count`（`run=verify`）改了，
`noisy-gate-total`（噪声口径的同一事实，当时 `run=nightly`）漏改 ⇒ nightly 连续 5 天
`facts-nightly` 失败（`期望 14±0.0，实测 18`），而 PR 门槛上的 verify 集**看不见它**
（nightly 的 claim 不在 verify 里跑），所以 push/PR 全绿、只有夜里红。
这正是"门禁抓不到自己"的又一种形态：**不是判据写错，而是判据的可见面比事实的分布窄**。

**规矩**（三条，按优先级）：

1. **结构性事实只登记一条 claim**，其余位置写"条数以 claim `X` 为准"，不在正文再写绝对数；
2. 确实需要两份时（如本例：噪声口径的条数要能被独立对账），另一份也必须放在
   **PR 门槛看得见的 `run`**（`verify`）——用**廉价命令**换可见性：本例的命令从
   `--per-fault 8 --repeats 3` 换成 `--per-fault 2 --repeats 1 --reasoner noisy`（本机约 1 秒），
   条数是结构性事实、不随样本量变化，换命令不损失判据强度；
3. 两份之间加**关系断言**钉死（`tests/test_facts_gate.py::
   test_noisy_gate_total_is_a_structural_sibling_of_suite_gate_count`），
   断"值相等 + 取同一 JSON 路径"，而不是各写一个常数——两个常数可以一起错。

**已修**：`noisy-gate-total` 的值 14 → 18、命令换廉价档、`run` 从 `nightly` 升到 `verify`
（先例：W11 把 9 条 claim 升 verify，理由是"push 上没人看得见"）；
`noisy-gate-failed-count` **保持 `nightly` 不动**——它读 `gate_summary.failed`，
与样本量相关（条数随 `--per-fault/--repeats` 变），廉价档下不成立。
