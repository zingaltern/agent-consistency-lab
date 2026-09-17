# 项目状态与交接（W8 开工前）

**这份文档是"压缩后的工作上下文"**：对话再长、上下文再挤，只要这份文件在，
W8 以及之后任何人接手都能从这里重新展开。细节不在这里——只放**不可丢失的事实与决策**，
每条都指向可以现场读的证据文件。

更新时间：W9（设计文档 A+B 交付 + 评审修复）｜ 代码量见 `docs/HANDOFF.md` 的复现命令一节（`find ... | xargs wc -l`）｜ 测试全绿：**用例数不写在这里**，见 claim `tests-collected`（`scripts/count_tests.py` 再生）

> **W9 先读这一段**：本轮把"结论可再生"从纪律变成了门禁——文档里每个被引用的数字都登记在
> `reports/documented-facts.json`（72 条 claim），由 `scripts/check_facts.py` 逐条重跑比对
> （CI job `facts` 跑轻集、`nightly.yml` 跑整量集）。改代码导致数字变化时，先跑这条命令。

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
| 产出物 | `harness/`（内核 ~3.9k 行）、`opsenv/`（场景与评测 ~1.7k 行）、`fakeworld/`（受控世界）、`experiments/`、`tests/` |

---

## 三、口径规则（跨轮次必须一致，否则数字不可比）

1. **kill -9 ≠ 掉电**：W2–W4 的崩溃结论只对 `kill -9` 成立（`synchronous=NORMAL` 下掉电会丢最后一个事务）。
2. **W2–W4 与 W5–W7 是两套样本量口径**：前者每格 n=5（点估计，0/5 的失败率上界约 60%），
   后者报 Wilson 区间 + 配对 bootstrap（n=192，最小可检测效应约 6%）。**不要混引**。
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
| W4 | 缓存纪律值 2 倍成本（+104.6%）；卸载是唯一纯赚杠杆（-66% token）；压缩与缓存冲突被量化 | `w4-report.md` |
| W5 | 取证充分性决定正确率上限（50% vs 100%）；无 gate 的路线在 20.8% weak 场景执行破坏性动作，有 gate 的 0%；三条读证据路线诊断同分（CRN 修复后 90.1%/61.5% 逐格相同） | `w5-report.md` |
| W6 | "谁更准"不可区分到零差异（配对 CI [0,0]），"谁更安全"显著（CI [−0.266,−0.151]）；代理指标须先校准（κ）；门禁要能抓"静默丢弃" | `w6-report.md` |
| W7 | 压缩买的是完成率不是省钱（净成本 +79%）；压缩次数与缓存命中聚合反向、细胞级非单调；0.70/0.85/0.95 不可区分，要避免 ≤0.50 与不压缩 | `w7-report.md` |

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
