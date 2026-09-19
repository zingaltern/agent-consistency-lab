# 独立验证报告：W11（2026-09-19）

**验证对象**：`agent-consistency-lab` @ `main`（验证开始时 HEAD = `426faaf`，工作树干净；
验证分支 `audit/independent-verification-2026-09-19`）
**验证者**：外部测试角色（独立会话，不复用作者的解释）
**方法**：先只读规格（README / `docs/semantics.md` / `docs/HANDOFF.md` / 全部源码与 `--help`），
冻结结论之后才读答案（`docs/w2..w7`、`docs/posts/`、`reports/`、`tests/`）；
四个方向的审计并行独立进行、各自只读仓库、产物写 `/tmp`；**每一条 P0/P1 都由我本人重新复现过**。

> **编号说明**：下文用 `D-n` 顺序编号；方括号里是**代码与文档里已经在用**的编号
> （`P0-1` / `P0-3` / `P1-1` / `P1-2` / `P2-1`…`P2-10`）。两套编号一一对应，不是两个缺陷。
> 编号按**方向**分配（语义 / 评测 / 文档各从小序号开始），合并成报告后只有并集——
> 所以是**不连续**的（缺 `P0-2`），没有别的含义。

---

## 0. 结论摘要（先读这一节）

**宣传与已完成的功能：总体相符。** 可执行门禁（本轮之前 14 条，修完后 18 条）、崩溃矩阵、四系统消融、配对统计、
claim 对账这套"别人可以自己验证我"的机制是**真的**，不是话术——每一条声称的结论都能由
仓库自带的命令再跑一次得到同样的数字，我逐条跑过。作者材料里没有发现"声称了但没做"的功能。

**但"相符"不等于"没有缺陷"**：本轮找到 **3 条 P0**（其中 1 条是**语义承诺被证伪**、
1 条是**实现背离自己文档**、1 条是**门禁的结构性盲区**），以及 4 条 P1、8 条 P2。
三类 P0 的共同点值得记下来：**门禁全绿、测试全绿、文档齐备，但它们抓不到自己**——
- 触发器只有 `BEFORE UPDATE` / `BEFORE DELETE`，于是 `INSERT OR REPLACE` 能改历史；
- 代码的落盘顺序与文档写的顺序相反，而门禁只查"有没有重复副作用"；
- 门禁只断言"某个比率等于多少常数"，把**指标的定义**改掉可以让 14 条全绿。

**已修**：3 条 P0 全部修复并各自附了"修复前会怎样"的回归用例 + 退化注入验证；
4 条 P1 全部修复（其中两条是"数字/产物不再是当前代码的产物"）；
8 条 P2 里 6 条修文档措辞、1 条（DDL 绕过路径）**后续已补实现**（三层防线，见 §3 的 P2-5 与 §5 第 1 条）、
1 条（门禁量 pooling）明确登记为未处置边界。
另外**按新代码重刷了变异基线**（`2050 / killed 1332 / survived 700 / no_tests 18`，
不可见空间 0.9%），并顺手量了一次它的判决稳定性（同代码两遍全量：2050 条**零翻转**）。

**未做**：见 §5。最重要的一条是 README 里仍有一批**没有 claim 覆盖**的数字
（14 格 / 1536 / 50% / κ=1.0 等），它们**没有**机器对账。

---

## 1. 方法与证据链

先跑基线，确认"仓库是健康的"——否则后面的发现会被误读成"仓库本来就是坏的"：

| 命令 | 结果 |
|---|---|
| `.venv/bin/pytest -o addopts= -p no:cacheprovider -q` | 429 passed |
| `.venv/bin/ruff check .` | 干净 |
| `.venv/bin/python scripts/check_facts.py --run verify` | 44/44 通过 |
| `.venv/bin/python -m experiments.crash_matrix --repeats 5` | 16 格全 `as-predicted`，**70/80 次真实 SIGKILL** |

**基线健康 ⇒ 工作重点不是"找 bug"，而是"找门禁抓不到的问题"。** 这句话是本轮的出发点，
也是三条 P0 的共同形态。

证据纪律：四个方向的审计各自把最小复现脚本与原始输出写在 `/tmp/audit-*/`，
**报告里每一条都在本轮重新跑过一遍**（脚本落在 `repro/`），不从审计结论里转抄。

---

## 2. 各方向结论表

| 方向 | 结论 | 关键证据 |
|---|---|---|
| A 可复现性（宣传数字 → 命令） | **0 P0 / 0 P1**；README 的 `ops_demo`、五分钟崩溃演示、MCP 崩溃演示、本地 OTLP 接收器、各 `--help` flag 全部与文档一致 | 逐条实跑；两个 P2（措辞与命令不是同一配置、`artifacts --help` 有 `RuntimeWarning`） |
| B 语义黑盒（`docs/semantics.md` 逐条证伪） | **9 条承诺成立，1 条被证伪（append-only）、1 条实现背离（落盘顺序）** | `repro/p0_1_append_only_bypass.py`、`tests/test_audit_regressions.py::test_event_lands_before_the_dedup_row_is_closed` |
| C 评测层（门禁能不能被抓死） | **原有 14 条门禁每一条都能被对应退化打红**；统计实现独立复算一致（Wilson、配对 bootstrap −0.208 [−0.266,−0.151]、MDE 公式）；CRN 现行代码未被污染（**384/384 配对键一致**）；holdout 无调参路径。**但门禁只断言比率、不断言定义** | `opsenv/suite.py` 新增四条关系门禁 + 四组退化注入（`/tmp/gate-degrade/run.sh`） |
| D 可复现性（产物 vs 当前代码） | **W7 的 9 条成本 claim 已不再再生**：入库产物是旧代码的产物，而它们只在 nightly 对账 | `repro/p1_3_w7_artifact_drift.sh` |

---

## 3. 缺陷列表

### D-1【P0-1】append-only 可被 `INSERT OR REPLACE` 绕过——`semantics.md` §2.1 被证伪

**声称**（`docs/semantics.md` §2.1）："物理上禁止改写历史：`events` 表上的
`BEFORE UPDATE` / `BEFORE DELETE` 触发器直接 `RAISE(ABORT)`。"

**实测**：UPDATE 与 DELETE 确实被拒，但

```sql
INSERT OR REPLACE INTO events(...) VALUES('E', ...)   -- 撞主键 event_id
```

**成功**，payload 被原地换掉、行数不变。SQLite 把 `REPLACE` 实现成"删冲突行、再插入"，
而那个**隐式 DELETE 只在 `PRAGMA recursive_triggers=ON` 时**才触发 `BEFORE DELETE`
（默认 OFF，runtime 也不设它——实测运行时该 pragma 读出来就是 `0`）。
于是**只用普通 DML、连 DDL 权限都不需要**，就能改写已提交的历史。
第二条路径更彻底：换一个 `event_id`、撞 `UNIQUE(branch_id, seq)`，会把原行整个删掉。

**分级理由**：这是"某条结论被证伪"（机制层面），不是"措辞不准"。且它恰好落在
整个项目的地基上——"事件日志是唯一权威"这句话依赖历史不可改写。

**修法**：第三条触发器 `events_no_insert_over_existing`（`BEFORE INSERT`，
撞 `event_id` 或撞 `UNIQUE(branch_id, seq)` 即 `ABORT`），schema 升 v4，
存量库由 `MIGRATIONS[4]` 幂等补；两条路径各一条回归用例（`tests/test_event_log.py`），
另加"正常追加不受影响"的正对照。

**最小复现**：`.venv/bin/python docs/independent-test-2026-09-19/repro/p0_1_append_only_bypass.py`
（修复前：`INSERT OR REPLACE` 两行都是"被接受"；修复后：全部被拒，原文一字未动）

---

### D-2【P1-2】`harness/execution.py` 的落盘顺序与 `semantics.md` §2.5 第 8 步相反

**声称**（§2.5 第 8 步）："`tool_result` 事件（**权威记录**）→ 闭合意图行 → 边界 checkpoint 提交"，
理由写着"事件先于去重行的原因：这样『去重行已写、事件未写』这个**危险**微窗口不存在"。

**实测**：代码里 `_resolve_pending` 的 APPLIED 分支是**先** `complete()` 闭合去重行、
**后**写 `tool_result` 事件；`_handle_tool_failure`（`record(status='failed')` 先于事件）
与 `_resolve_pending` 的两个 `unknown` 分支同样倒置。真 `SIGKILL` 命中那一瞬间，
会留下"**行已闭合、事件缺失**"的形态。

**为什么这是 P0 而不是 P2**：项目**自己的裁判**判它违规——
`opsenv/oracle.py::check_closed_calls` 的判据就是"行在事件必在"，
它的 docstring 明说 loop 的正常顺序是"先写事件、后写行"。也就是说：
**修复前的代码会产出被本项目自己的 oracle 判定为违规的状态。**

**修法**：四个分支改为"先落 `tool_result` / error artifact，再 `complete` / `mark_unknown` /
`record`"；模块 docstring 把这条固化成纪律，并写明唯一例外（outbox **意图**行 `begin`
按设计必须早于副作用，且它不是闭合）。

**回归用例**：`tests/test_audit_regressions.py::test_event_lands_before_the_dedup_row_is_closed`
——真子进程 SIGKILL（`CHAOS_WINDOWS=post_tool_effect_pre_record:1`）+ 自杀式 patch 精确落在
`complete()` 返回之后，然后拿冻结的 `world.db` 交给项目自己的 oracle 判。
**退化注入验证**：把顺序改回 `complete()` 在前 ⇒ 用例变红（报 `inv_closed_calls`），还原后通过。

---

### D-3【P0-3】门禁只断言"比率是多少"，不断言**指标的定义**

14 条门禁里有 13 条形如"某格某比率 == 某常数"。这样的门禁**不能**发现下面这类退化：

* 把 `is_sufficient` 改成恒真 ⇒ "取证充分性决定正确率上限"这句话失去定义式，门禁全绿；
* 把"**静态拒绝列表之外**的新破坏性动作"从 operator 与红线判据里摘掉 ⇒
  "有审批门的路线 0 条红线"变成同义反复，门禁全绿（而 `novel_red_line_total` 从 3 涨到 9）；
* 把 system 名写回随机种子（CRN 被破坏）⇒ 门禁全绿，且**复现出历史伪影**
  （0.896 / 0.859 / 0.932 那组数字）。

**修法**：新增四条断言**关系**（而不是常数）的门禁——
`workflow.correct==workflow.sufficient`、`gated_routes.novel_red_line==0`、
`single_shot.novel_red_line>0`（对称自检，防止上一条空真）、
`crn.evidence_routes_agree`（同一配对键上三条读证据路线的 (诊断, 动作) 必须一致，当前 384/384）。

**退化注入验证**（四格，都在 `/tmp` 副本里做，还原后基线全绿）：

| 注入 | 期望 | 实测 |
|---|---|---|
| `is_sufficient → True` | 只红 `workflow.correct==workflow.sufficient` | ✅ 只红这一条 |
| 摘掉 operator+红线里的"新动作" | 只红 `gated_routes.novel_red_line==0` | ✅（harness / langgraph 各 3 次） |
| 场景目录里不再有新动作 | 只红 `single_shot.novel_red_line>0` | ✅ |
| 种子里写回 system 名 | 只红 `crn.evidence_routes_agree` | ✅ 211 个配对键不一致 |

四条各自的单元用例在 `tests/test_stats_and_gates.py` 的"指标**定义**门禁与 CRN 门禁"一节，
每条都带"改坏什么会让它红"。

---

### D-4【P1-1】CRN 规则只被 pytest 守着，门禁层面**没有**覆盖

`tests/test_*.py` 里有"种子不得含 system 名"的用例，但那是**测试**；门禁（`--gate` 的退出码）
层面没有任何东西守着它。退化注入已证明：把 system 名写回种子，14 条门禁全绿。
修法即 D-3 的第四条门禁（关系断言）。

---

### D-5【P1-3】W7 的 9 条成本 claim 已不再再生

* `scripts/check_facts.py --run nightly` 的 W7 部分只有 2/9 通过；
* 选点从 0.70 变成 0.95；公开数字应为 `$0.118732` 而不是 `$0.11834`；
* `reports/w7_sweep.json` 相对当前代码是**陈旧产物**——这不是"忘了重跑"，
  而是这 9 条 claim 当时**只在 nightly 作业里对账**，push 上没有人看得见。

**修法**（根因修复，不只是改数字）：重新生成 `reports/w7_sweep.{json,md,svg}` 与
`reports/w6_eval.{json,md}`；把 9 条 W7 claim 的 `run` 从 `nightly` 升到 `verify`
（代价是 CI 的 facts 作业多约 38 秒，收益是漂移会在 PR 阶段变红）；
把 W7 数字回灌 `README.md` / `docs/w7-report.md` / `docs/posts/03-cache-accounting.md` /
`docs/HANDOFF.md`，并把"压缩桶占比"的口径标签补全（占**主桶** 26.8%–33.9%、
占**净成本** 21.1%–25.3%，两者不得混引）。

**最小复现**：`sh docs/independent-test-2026-09-19/repro/p1_3_w7_artifact_drift.sh`
（把入库产物与"按 claim 的再生命令重跑一次"逐位比较）

---

### D-6【P1-4】README 的 W3 括注是过期矩阵

README 的 W3 括注当时写的是"**14 格 × 5 次 = 70 次运行，其中 65 次真实 SIGKILL**"，
而按仓库自己的命令跑出来的崩溃矩阵是"**16 格 × 5 次 = 80 次运行，其中 70 次真实 SIGKILL**"
（两个对照格按设计不注入；`crash_marker.json` 逐格可查）。旧的 14 格 / 65 次是**更早一轮
矩阵规模**的残留，而且它把"70 次运行"与"65 次注入"同时写在了一句话里，读起来自洽、
实际上两个数都不是当前的。修法：README / `pitch` / `resume` 三处的括注全部对齐到
16 格 / 80 次 / 70 次，并把"16 格里 15 格 0 重复副作用、唯一例外是那格按设计关掉 outbox"
这一层写清（原先"3 个窗口 0 重复副作用"是漏读）。

---

### D-7【P1-5】`mutation-*` claim 的"再生命令"读的是入库基线

7 条变异 claim 的 `source_cmd` 是 `scripts/mutation_check.py --summary-only`，而
`--summary-only` 的语义是"只读基线并输出 JSON 摘要，不跑 mutmut"。也就是说：
**它们对账的是"文档 ↔ 入库基线文件"，不是"文档 ↔ 当前代码"**——基线本身漂了、
或者文档与基线一起过时，这条对账都不会红。

**处置**：不假装它是再生。把性质写进 `what`（"这是 docs ↔ 入库基线的一致性检查，
不是重生；真正的再生要跑 mutmut 全量，见 nightly job `mutation`"）并带上基线里的
`total/killed/survived`。**这是一个"如实标注"而不是修复**：要真正再生，只能在 nightly 里跑。

---

### D-8【P2 清单】

| # | 内容 | 状态 |
|---|---|---|
| P2-1 | `docs/semantics.md` §2.4 的交叉校验"只看最新 checkpoint、只校验引用 id 存在"，一句话读起来比实现大 | 已收窄措辞；未改实现 |
| P2-2 | 配对 MDE 的 discordant 计数错用"两臂阳性数之和"（当前恰好相等，换了竞争假设会算大） | 已修 + 单元用例 |
| P2-3 | 三个 MDE 数字（配对 9.2% / 独立 p=0.5 的 10.0% / p≈0.9 的 CI 半宽 6.0%）只有 6.0% 被物化，而文档拿它当"最小可检测效应"引用（漏功效项 + 用错公式） | 三个全部物化、各自一条 claim、口径写进 `HANDOFF` §三与 `w6-report` §二 |
| P2-4 | 门禁量 pooling dev+holdout（口径不自洽） | **未处置**（登记在 §5） |
| P2-5 | `ALTER TABLE ... RENAME`、`PRAGMA writable_schema` 等 DDL 绕过路径未登记 | 先写进 §2.1 的边界声明；**后续已补实现**（三层防线：authorizer + `DBCONFIG_DEFENSIVE`、写前逐字核查与 `setup()` 默认拒绝、离线核查；残余边界仍在 §2.1） |
| P2-6 | 4 条 W4 claim 的 `what` 散文与 `value` 不一致（+104.6 vs +105.1 等） | 已对齐 |
| P2-7 | README 的末位数字（`$0.01078+$0.00357` 与 `$0.01436` 差一个末位）、`-66%/-61%` 缺"截断期均值"口径 | 已补口径 |
| P2-8 | `docs/w5-report.md` §三 是 CRN 修正前的数字（且它引用的 `reports/w5_eval.json` 不存在）、`docs/w6-report.md` §二/§三/§四/§五 是 CRN 修正前的数字 | §三 加历史快照横幅、改指 `reports/w6_eval.json`；w6 各节数字对齐当前产物 |
| P2-9 | `harness.artifacts --help` 会打 `RuntimeWarning` | **未处置**（不影响结论） |
| P2-10 | `.venv/bin/pip` 与全部 venv 入口脚本的 shebang 指向项目移动前的旧路径（**环境问题，非仓库问题**） | 已就地修正（`.venv/` 被 gitignore） |

---

## 4. 我无法判定的项

* **"9 个审计代理 / 7 个 P0"**（`docs/pitch-4min.md`）：这是作者对**自己**做过几轮审计的
  叙述，仓库里没有可核对的产物（那几轮的报告不在 `docs/`）。我无法判定数字，只能标为"不可核"。
* **`harness/execution.py` 的顺序改动对吞吐的影响**：没有基准测试，我不给方向性结论。
* **"压缩桶占比"的正确口径本身**：占主桶还是占净成本取决于读者想问什么，两者都对——
  我能做的只是要求文档写清分母（已做），不能替作者定哪一个"才是对的"。
* **真实模型下的结论**：评测层的推理器是受控桩，真实模型的错法分布未知（作者已披露）。

---

## 5. 未处置 / 未覆盖的风险

1. **append-only 的边界**：`DROP TRIGGER`、`ALTER TABLE ... RENAME`、`PRAGMA writable_schema=ON`
   原先都能绕过守卫，本文写作时只在 `semantics.md` §2.1 写成**范围外**。
   这块**已在本轮之后补上实现**：`harness/store/guard.py` 给连接装 authorizer
   （拒 `DROP TRIGGER` / `DROP TABLE|VIEW|INDEX` / `ALTER TABLE` / 写 `sqlite_master` /
   `PRAGMA writable_schema`；Python 3.12+ 另开 `SQLITE_DBCONFIG_DEFENSIVE`），
   `append_many()` 与 `setup()` 在动手之前先把 `sqlite_master` 里的触发器文本与
   `events` 结构逐字对回 DDL（版本已是最新却防线不全 ⇒ 默认拒绝，要修必须显式
   `setup(allow_repair=True)`），`audit_chain` 的结果多一个 `guard` 字段并计入退出码。
   **残余（仍然不能读过头）**：防线只保护**本进程这一条连接**。拿到库文件的人可以另开
   一条没有防线的连接，改完之后把触发器文本与 `PRAGMA schema_version` 一起凑回
   "看起来没被动过"的样子。链没有密钥，所以这不是防篡改——它保证的是**改写无法静默**。
   退化注入验证（把 authorizer 摘掉 / 把写前核查摘掉 ⇒ 用例必须变红）见 `docs/testing.md` §3。
2. **门禁量把 dev 与 holdout pooling**（P2-d）：报告里分开报了，门禁的判据里没有。
   改它要动判据阈值，属于"改门禁语义"，按仓库纪律应当单独一轮做。
3. **README 里没有 claim 覆盖的数字**：14 格 / 1536 次 / workflow 50% / κ=1.0 等
   仍未被 `documented-facts.json` 绑定。`doc_literals` 只覆盖了被挑中的那几条。
4. **变异门禁的判决稳定性**：仓库自己记着"单条变异体的判决不承诺稳定"
   （`_replay_outcome__mutmut_37` 随负载在 survived ↔ killed 翻转）。本轮顺手做了
   一次**同代码两遍全量**的实测：2050 条逐条判决**零翻转**、门禁退出 0——
   所以那条是"不承诺"，不是"已知会红"。**但这不等于以后不会抖**：它只是说明
   "抖动"在本机这两次之间没有发生；判据的第 1 条红灯条件（基线 killed → survived）
   仍然对抖动敏感。
5. **一个真实的操作陷阱**（本轮踩到）：`mutmut run` 是**增量**的——`mutants/` 缓存还在时
   它只重跑"函数哈希变了"的变异体。我在改过 `harness/execution.py` 之后直接
   `--update-baseline`，14.7 秒"跑完"并写出了一份 `no tests` 从 18 虚增到 241 的基线——
   那是**混合了 W10 旧判决**的增量结果，不是基线。判据本身没错（`--update-baseline` 只在
   `mutmut run` 退出 0 时才写），但**"退出 0"骗不过增量**。
   **后续已把这条纪律变成代码**：`scripts/mutation_check.py --update-baseline` 现在会先把
   `mutants/` 整体移开（`mutants.stale-<UTC 时间戳>/`，已 gitignore）再跑全量，并把条件写进
   基线与 `--json-out` 的 `refresh` 字段；要沿用旧缓存必须显式 `--allow-incremental-refresh`
   （会大声警告）。`docs/HANDOFF.md` §十第 4 条同步改写。
6. **`docs/resume.md` 的"LangGraph 95 行 vs 自研 340 行"**：这两个行数没有 claim 覆盖，
   本轮没有独立复核，仍按作者原文保留。

---

## 6. 证据索引

| 证据 | 位置 |
|---|---|
| P0-1 最小复现 | `repro/p0_1_append_only_bypass.py` |
| P2-5 最小复现（DDL 防线三层：防 / 写前核查 / 离线核查） | `repro/p2_5_ddl_bypass_guarded.py` |
| P0-2 回归用例（真 SIGKILL + 项目自己的 oracle 判） | `tests/test_audit_regressions.py::test_event_lands_before_the_dedup_row_is_closed` |
| P0-1 的四条触发器件数用例 | `tests/test_event_log.py`（`test_trigger_blocks_*`）、`tests/test_hash_chain.py`（迁移） |
| P0-3 / P1-1 的四条新门禁 | `opsenv/suite.py`（`check_gates` 两处新段） |
| P0-3 / P1-1 的单元用例与退化注入 | `tests/test_stats_and_gates.py` |
| P1-2 的产物漂移复现 | `repro/p1_3_w7_artifact_drift.sh` |
| 三个 MDE 的物化 | `reports/w6_eval.json::statistics`、claim `w6-mde-*` |
| 本轮全部命令 | `commands.sh` |

---

## 7. 与作者声称的差异

### 7.1 一致的（先说这个）

* **"结论可再生"不是话术**：我按 README 的"如何复现全部结论"逐条跑，全部复现；
  `check_facts.py --run verify` 逐条对账通过。这套机制是真的，而且它确实在历史上抓到过缺陷
  （claim 的 `docs` 锚点校验就是这样进仓库的）。
* **崩溃语义的边界声明是诚实的**：`kill -9 ≠ 掉电`、`有 pending 意图行就先探针`、
  `at-least-once 而不是 exactly-once`——我按语义文档逐条设计证伪实验，**没有一条被推翻**。
* **自证陷阱的披露是真的**：`semantics`/`testing` 里的禁止清单（种子不得含 system 名、
  不能用 ground truth 喂被判定的机制、holdout 不调参）在代码里都有对应的门禁或测试。

### 7.2 差异（全部集中在这三类）

| 作者材料 | 实测 | 性质 |
|---|---|---|
| `semantics.md` §2.1"物理上禁止改写历史" | `INSERT OR REPLACE` 可绕过 | **承诺被证伪**（已修） |
| `semantics.md` §2.5 第 8 步"事件先于去重行" | 代码里顺序相反 | **实现背离文档**（已修） |
| "评测门禁全过"这类表述 | 门禁全过 ≠ 指标定义没被改 | **门禁的结构性盲区**（已补四条关系门禁） |
| README/HANDOFF/pitch/resume 的一批数字 | 与当前代码的产物不符（W3 括注、W7 成本、压缩桶占比、`$0.005–0.012`、`60 次 kill -9` 等） | P1/P2（已修） |

### 7.3 作者材料里我**无法**验证的部分

* 那几轮"对抗性审计"的原始报告（不在仓库里）；
* `packaging/` 与 `.zcode/` 的内容（按仓库纪律属本地内容，不得提交或引用，我也没有引用）。

---

## 8. 复现方式

```bash
cd <仓库根> && .venv/bin/python docs/independent-test-2026-09-19/repro/p0_1_append_only_bypass.py
sh docs/independent-test-2026-09-19/repro/p1_3_w7_artifact_drift.sh
.venv/bin/pytest -o addopts= -p no:cacheprovider -q \
    tests/test_event_log.py tests/test_hash_chain.py tests/test_stats_and_gates.py \
    tests/test_audit_regressions.py
.venv/bin/python scripts/check_facts.py --run verify
```

`commands.sh` 是本轮跑过的完整命令清单（含基线四件套与四条新门禁的退化注入）。
