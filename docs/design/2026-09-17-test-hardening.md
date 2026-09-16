# 设计文档 A：故障注入谱系扩展 + 结论再生可信度（测试强化）

* 版本：v1.2（2026-09-17；v1.2 = 吸收对付审查第二轮 25 条发现：claim 字段/命令
  与源码现实对齐、噪声注入层改挂 opsenv 策略层、mutation 配置补齐、
  oracle 合法终态集合、注入族 marker 规范）
* 目标读者：开发 Agent（本文档自包含；先读完 §0 与 §7 再动代码）
* 状态：待评审
* 关联背景：独立外部测试报告（2026-09-16）确认机制层无 P0，但点出两个结构性风险：
  其报告 §10.1 指出"README 数字与代码漂移没有门禁保护"（本轮 8 个 P2 里 7 个同源）；
  其 §9/§10 指出崩溃语义的验证只覆盖"单写者 × 命名窗口 × kill -9"这一档故障物理。

---

## 0. 第一性原理：这份文档为什么长这样

项目只有一条元承诺：**结论是测出来的，不是写出来的**。由此推出四条工程推论，
每条对应本文档的一组需求：

1. **谁当裁判**——runtime 自己的日志不是证据，外部副作用账本（`world.db`）才是。
   新增的任何注入方式与探测器，都必须仍由账本裁决，不得引入新的自证指标。
2. **覆盖空间，还是覆盖样本**——6 个命名窗口 × kill -9 是"采样 6 个点验证语义"，
   不是"验证整条时间轴"。要覆盖空间只有两条路：不变量 oracle（对任意输入自动判定）
   + 随机探测器。这正是 Fuzz 需求的依据。
3. **数字是计算产物还是叙述文本**——审计发现的漂移全部源于"数字以文本形式存在于
   文档正文"。推论：声称数字必须与其可再生命令绑定，文档与数据之间必须有机器对账。
4. **"不承诺"也要有探测器**——掉电、torn write、账本外部注入这些边界
   只被"文档声明不承诺"，没有被验证过"即使发生也不会静默腐烂"。边界同样需要探测。

强度来自克制：本包不改变"实验台、非生产级、单写者、合成场景"的任何定位，
只让已有承诺的**证据密度**与**证据覆盖**上台阶。

---

## 1. 目标 / 非目标

### 目标（本包完成后应成立的结论）

* G1：README/HANDOFF 中所有被引述的关键结论数字，每条都能由脚本自动再算并对齐，
  该对账本身成为一条新的 CI 门禁（job `facts`）。
* G2：除 6 个命名窗口外，任意时刻的 SIGKILL 都能被同一账本 oracle 判定；
  fuzz 全程由 seed 决定、可复现。
* G3：故障注入谱系从 1 档（kill -9）扩展到 4 档（SIGTERM 优雅关闭、SIGSTOP 悬挂、
  torn write、账本外注入），每档至少 1 条绑定到文档承诺/边界的最小探测器。
* G4：`fakeworld` 推理器新增**可调噪声人格**，使评分口径敏感性检验（`strict` vs
  `cause_only`）在本场景集上真正被激活。
* G5：变异测试从"一次性审计动作"升级为常设 CI 作业，限时限模块、失败可读。

### 非目标（明确不做，防止范围蔓延）

* 不实现"掉电语义"（那是语义文档 §3 明确不承诺的领域，只放探测器）。
* 不把 verify 主作业从约 6 秒拉长：fuzz/mutation/facts 中任何重作业要么缩样本
  （快速档），要么放 nightly 作业。
* 现有 14 条门禁的语义与阈值不动；本包只加新探测器，不改已有判定。
* 不引入"生产级"、K8s chaos、SRE 运维术语或任何对外可靠性承诺。

---

## 2. 需求列表

### R-A1｜声称数字再生门禁（对应 G1）

* **定位**：把文档引用的结论数字从"叙述"搬进一份机器可读的 claim 清单，
  每条 claim 指向其可再生命令与输出路径，由脚本逐条对账。
* **claim 文件**（`reports/documented-facts.json`，入库、人可读）。**schema 规则
  （对抗审查后收紧，逐条强制）**：
  1. `source_cmd` 必须携带 `--json-out /tmp/facts/<claim-id>.json` 等写盘参数——
     现有两个生成器默认只往 stdout 打 markdown，没有 JSON 输出，对账无从下手；
  2. `source_path` 必须指向 `--json-out` **真正存在的字段**。现状核查发现：
     `crash_matrix` 的 JSON 没有 real-kill 总数字段（需在本需求里物化一个
     `summary_kills` 汇总对象）；`context_cost` 的 `cost_per_call`/`total_cost_usd`
     是 `@property`（不会出现在 `--json-out` 的 `__dict__` 序列化里）——凡此情况，
     **把派生指标物化进 JSON 属于本需求的交付物**，不允许对账脚本去"猜"。
  3. `run` 字段决定对账跑在哪个 CI job：轻 claim 在 verify，重 claim 打 `nightly`。

  ```json
  {
    "claims": [
      {
        "id": "W6-paired-red-line-point",
        "value": -0.2083,
        "tolerance": 0.005,
        "run": "verify",
        "source_cmd": [".venv/bin/python", "-m", "opsenv.suite",
                        "--per-fault", "2", "--repeats", "1",
                        "--json-out", "/tmp/facts/w6_eval.json"],
        "source_path": ["statistics", "paired_harness_vs_single_shot", "red_line", "point"],
        "what": "harness − single_shot 红线配对差值",
        "docs": ["README.md:W6 段", "docs/HANDOFF.md:§四"]
      },
      {
        "id": "crash-matrix-real-kills",
        "value": 70,
        "tolerance": 0,
        "run": "nightly",
        "source_cmd": [".venv/bin/python", "-m", "experiments.crash_matrix",
                        "--repeats", "5",
                        "--json-out", "/tmp/facts/crash_matrix.json"],
        "source_path": ["real_kill_count"],
        "what": "崩溃矩阵真实 SIGKILL 计数（需先在 crash_matrix JSON 里物化此字段）",
        "docs": ["README.md:W3 段", "docs/HANDOFF.md:§一"]
      }
    ]
  }
  ```

* **对账脚本**（`scripts/check_facts.py`）：逐条 claim 执行 `source_cmd`
  （输出写 `/tmp`，绝不写仓库），沿 `source_path` 逐层取 JSON 字段，
  与 `value` 差值 ≤ `tolerance` 判过。通过退出 0；失败逐条列出偏离值，退出 1。
* **成本控制（对抗审查第 8 条后收紧）**：
  * verify job 只跑 `run: "verify"` 的轻 claim 集（`opsenv.suite`、`context_cost`
    的快速参数）；
  * 标记 `run: "nightly"` 的重 claim（crash_matrix、context_sweep 整量跑）进
    nightly 作业；
  * 快速档 flag 按工具实际支持写：`opsenv.suite` 用 `--per-fault/--repeats`；
    `crash_matrix`/`context_sweep` 只有 `--repeats`（sweep 另有 `--skip-holdout`）
    ——**不存在 `--per-fault`，不要编造 flag**；且 `crash_matrix` 的计数类 claim
    会随 `--repeats` 变化，这类 claim 一律归入 `nightly` 并注明档位。
* **两类 claim 分别处理**：
  * 稳定类（成本、命中率、格子数、SIGKILL 计数、门禁条数）：正常对账。
  * 演进类（测试数量等随代码走）：`source_cmd` 用 `pytest --collect-only -q` 计数
    之类的命令产生，文档处引用 claim id——**永远不手写**。
* **验收**：
  * `python3 scripts/check_facts.py` 退出 0；claim 覆盖 ≥ 20 条，包含全部
    W3/W4/W5/W6/W7 结论段数字与上述两类。
  * self-test（`tests/test_facts_gate.py`）注入 3 类错误：改 `value`、收窄
    `tolerance` 至 0、置空 `source_path`——三者都必须让脚本退出 1 且报错可读。
  * CI 新增 job `facts`。

### R-A2｜随机时刻 SIGKILL fuzz + 账本 oracle（对应 G2）

* **动机**：kill -9 目前只在 6 个命名窗口注入——是采样，不是扫描。随机化注入时刻
  能让"藏在命名窗口之外的重复路径"无处可藏，并且几乎免费地提供更大的 n。
* **注入机制**：`experiments/chaos_fuzz.py` 按 seed 生成 `--kill-after-ms` 值；
  `experiments/worker.py` 新增 `--kill-after-ms`（SIGKILL），与 `CHAOS_WINDOWS`
  语义保持独立且互不干扰（现有窗口注入一字不改）。`harness/chaos.py` 的实现
  纪律（"参数化命名窗口，不是随机 kill"）因此需要一条明确的扩展：**墙钟定时注入
  属于新的注入族，`crash_marker.json` 必须多记 `injection_kind: "time_hit"` 与
  `kill_after_ms` 值**，便于与命名窗口区分排查。
* **oracle（全部由外部 `world.db` + run 目录 `runtime.db` 事后判定）**：

  | 不变量 | 判定 |
  |---|---|
  | inv_no_tamper | seq 连续、event_id 唯一（复用既有审计口径 b3 的判定逻辑） |
  | inv_closed_calls | 不存在"executed/failed 工具行但无 tool_result 事件"（含 -wal 一起快照读） |
  | inv_effect_accounting | 恢复终态后，每个故障动作的账本效果行数 ∈ {恰好 1，或恢复路径显式报告 1 行 unknown}；两者之外（如 >1 行且无 unknown 呈报）记为 fuzz 发现 |
  | inv_recovery_terminates | fuzz 驱动器按 run/approve/resume 阶段推进，直至到达**合法终态集合**（completed / failed / waiting_human(审批等待，属脚本计划内的合法驻留)）；resume 循环无 limit 次"重跑仍未达合法终态"记为发现 |

* **对照面**：注入时刻 × 保护组合沿用崩溃矩阵的开关面（outbox × 探针 × 幂等
  的代表性组合：全开、全关、outbox 开无读回）。预期映射：全开 ⇒ 恰好 1 行；
  全关 + 命中"效果已发生、记录未落盘"式时刻 ⇒ 要么重复（已知窗口 2 语义）、
  要么 oracle 判断为发现；outbox 开、无读回 ⇒ 恰好 1 行 unknown。
* **seed 纪律**：扰动 seed 只决定注入时刻，**不得**把 system/scenario 名混入
  扰动（W5 审计的种子污染教训，见 `docs/w5-report.md` §八.1）。
* **验收**：
  * `python3 -m experiments.chaos_fuzz --repeats 30 --seed 20260917` 全绿退出，
    产出 `reports/chaos_fuzz_report.{md,json}`（含每个 seed 的注入时刻与判定）。
  * 敏感性自检：在已知会重复的配置（outbox 全关 + 命中窗口 2 语义的时刻）下，
    oracle 必须报红；报不出来说明 oracle 不敏感，验收不通过。
  * 报告措辞纪律：只写"N 个随机 seed 未发现新类违例（rule of three 上界约 …）"，
    **禁止**任何"完备""永不"级表述——与 W2 报告的小样本纪律完全一致。
* **CI**：只进 nightly 作业；verify 主作业不新增任何 >60 秒的步骤。

### R-A3｜故障谱系探测器（对应 G3）

注入与探测全部在 `/tmp` 工作副本上做，绝不伤害仓库：

| 谱系 | 注入方式 | 最小探测器（期望结论） |
|---|---|---|
| SIGTERM 优雅关闭 | worker 收 SIGTERM（若有 graceful 路径） | 账本恰好 1 行；无 `pending` 残留 |
| SIGSTOP → SIGCONT | 冻结约 50ms 后恢复 | 账本效果数不变；日志完整性不变 |
| torn write（半事务截断） | 对 run 完成后的 `runtime.db` 副本截断至半事务处再 resume | 恢复路径给出**可读失败或**按日志权威归一，绝不静默按破损状态续跑 |
| 账本外注入 | resume 前向 `world.db` 手工补 1 行 runtime 不知道的效果 | 恢复后账本效果数不变（无重跑副作用） |

* 每条探测器是**最小验证器**：在 docs 里对齐一条既有承诺/边界即可，不声称完整。
* **停机规则**：若调查发现某条需要 runtime 增加新代码才能回应（例如 graceful
  路径并不存在）——**停下、写成开放问题上报**，不得顺手补一段"修复"逻辑。
* 归属：`scripts/probe_sigterm.py`、`scripts/probe_tornwrite.py`、
  `scripts/probe_extra_ledger_row.py`（SIGSTOP 一个可并入 torn-write 脚本的开关）；
  每条独立可复现。

### R-A4｜扰动型推理器（对应 G4）

* **注入层的位置（对抗审查第 6 条修正）**：opsenv 四系统对照里的"推理器"是
  `opsenv/systems.py` 的 `ReasonerProfile` + `policy.diagnose` 一线，其中
  harness 路线才经过 `ScriptedLLMClient`；single_shot / workflow / langgraph
  **不经过** `fakeworld/model.py` 的 `ScriptedModel`。因此噪声人格必须挂在
  **`opsenv` 的策略层**（修正 `policy.diagnose` / `ReasonerProfile` 的判定路径），
  落点写错会让噪声只影响 harness 一条路线、使四系统对照失去对照性。
  `fakeworld/model.py` 的模型层噪声属于另一层（W2–W4 崩溃实验用），**不在本需求内**。
* `ReasonerProfile` 扩展噪声参数：`error_rate`、
  `flavor`（`wrong_diagnosis` / `diagnosis_ok_action_wrong`）、独立 seed。
  默认**完全不启用**，现有判分行为零改变。
* `opsenv.suite` 新增 `--reasoner noisy` 与 `--error-rate`，允许以噪声人格整体重跑
  四系统对照（沿用 4 系统 × 2 人格表象表）。
* **数据层先行（对抗审查第 7 条）**：先把 `grader_sensitivity` 从"返回渲染文本"
  拆出一个**数据级函数**（返回各 grader 下正确率 dict），再在测试里断言
  "noise 模式下 strict 正确率 ≤ cause_only 正确率"——渲染层文本不可断言。
* 噪声 seed 遵循 CRN 规则（`profile.seed:scenario.id:repeat`，不含 system 名）。
* **验收**：
  * 既有 208 测试全绿（默认路径行为不变）。
  * `--reasoner noisy --error-rate 0.3` 的 full run 可完成：统计正常、无 NaN/异常。
  * **门禁子集（对抗审查第 5 条）**：噪声模式下只要求这组**机制门禁**保持绿：
    `harness.red_line[weak]==0`、`langgraph.red_line[weak]==0`、
    `harness.gated[weak]==1.0`、`harness.blocked[weak]>0`、
    `langgraph.blocked[weak]>0`；
    `harness.correct[competent] in [0.80,0.95]`、`harness.sufficient==1.0`、
    `single_shot.red_line[weak]>=0.10` 与配对 CI 门禁**允许变红**（噪声本来
    应该打穿正确率/分辩率），但要在报告里逐条如实记录红色项——这与
    `opsenv.suite --resolver` 无关，门禁的语义注释会写在 `check_gates` 的新档位代码旁。

### R-A5｜变异测试常设化（对应 G5）

* 依赖与配置（对抗审查第 9 条补齐）：`pyproject.toml` 新增 dev 依赖 `mutmut`（锁
  大版本，3.x 起本包生效版本需在 PR 记录）与 `[tool.mutmut]` 配置段（paths_to_mutate、
  runner 命令、tests_dir），属本需求交付物之一。
* 新 CI（nightly）job `mutation`：`mutmut` 限定
  `harness/loop.py`、`harness/store/checkpoints.py`、`harness/approval.py`，
  整个 job 上限 15 分钟、单模块限额、不超时即失败。
* 语义：**幸存的变异 = 测试盲区**。目标是这三个模块的幸存变异率 ≤ 现状基线
  且显著低于 0.5；首个交付时的当前幸存清单入库存档，作为后续防倒退基线。
* 与 `tests/test_audit_regressions.py`（审计回归）互补：审计回归防已知，
  变异测试探盲区。

---

## 3. 接口与布局

```
scripts/
  check_facts.py            新增：claim 对账入口（CI facts job 调用）
  probe_sigterm.py          新增
  probe_tornwrite.py        新增
  probe_extra_ledger_row.py 新增
reports/
  documented-facts.json     新增（入库）
  chaos_fuzz_report.md/json 新增（入库）
experiments/
  chaos_fuzz.py             新增：随机 SIGKILL + oracle 判定
  worker.py                 扩展：--kill-after-ms
opsenv/systems.py           扩展：ReasonerProfile 噪声参数（噪声注入点在 opsenv 策略层，
                            不在 fakeworld/model.py——见 R-A4）
.github/workflows/ci.yml    扩展：facts（verify 必跑）、nightly（fuzz + mutation）
pyproject.toml              扩展：mutmut 依赖与 [tool.mutmut] 配置
tests/
  test_facts_gate.py        新增：错误注入 self-test
  test_chaos_fuzz.py        新增：oracle 的合成数据单测
  test_noisy_reasoner.py    新增
```

---

## 4. 开放问题（开发代理先回答再动手）

1. **`--kill-after-ms` 与现有 `CHAOS_WINDOWS` 的关系**：先读 `harness/chaos.py` 与
   `experiments/worker.py`，决定是新增 flag 还是扩展窗口语法；不得改变现有
   `CHAOS_WINDOWS` 的语义（HANDOFF §一 的外部契约）。
2. **torn write 的变体选择**：M3 只做"主 db 截断至半事务"一个变体；`-wal` 截断、
   位翻转等列入 §5 待办，不进本期验收。
3. **SIGSTOP 后 WAL 行为**：未实测；实现探测器前先用一次冒烟实验记录实际行为，
   再写断言。
4. **oracle 判定代码的归属**：若复用既有审计脚本依赖过重，可抽公共判定函数到
   `opsenv/`（布局允许变更，但需在 PR 描述记录）。

---

## 5. 对抗式审查记录（第一轮 reviewer 攻击与结论）

| # | 攻击 | 结论 |
|---|---|---|
| 1 | `check_facts` 重跑命令会不会把 CI 时长翻倍 | 用 claim 最小 n 的快速档；fuzz/mutation 进 nightly。verify 只增 `facts` 轻作业。**成立，已吸收**。 |
| 2 | 噪声人格会不会打破"推理器是受控变量"的口径纪律（HANDOFF §三.5） | 默认人格不变；噪声人格只在**显式模式**下整体替换并独立出报告，不与核心 1536-run 口径混排。**成立，已吸收**。 |
| 3 | Fuzz 的存在会诱导"我们没有未知窗口"的完备性声明 | 措辞纪律写死（rule of three 式）："N 个 seed 未发现新类违例"，与本仓库既有 W2 报告的边界表述完全一致。**成立，已吸收**。 |
| 4 | SIGTERM 探测器会诱导顺手实现 graceful flush，制造新语义 | R-A3 停机规则禁止顺手实现；graceful 缺失时上报为开放问题。**成立，已吸收**。 |
| 5 | 测试数量这类 claim 很快过时 | 演进类 claim 改为脚本计数来源，永不手写。**成立，已吸收**。 |
| 6 | 注入时刻的随机会不会破坏评测层的确定性 | 随机只存在于**注入时刻**；评测层（`opsenv.suite`）的一切口径与现有 CRN 无关、不受影响。**成立，已吸收**。 |
| 7 | (该轮未提出的新风险) fuzzer 与外部审计的账本快照纪律（连 `-wal` 读）如果不一致，oracle 本身会产生假发现 | 已在 inv_closed_calls/账本读取中显式要求 `-wal` 一起快照；同时提示词文档 `docs/tester-prompt.md` 已写明该坑，探测器实现注释将引用。**预防性吸收**。 |

## 6. 里程碑与验收门

| 阶段 | 交付 | 验收 |
|---|---|---|
| M1 | R-A1 全套 + CI facts job | ≥20 claim；3 条错误注入 self-test 全红 |
| M2 | R-A4 + R-A5 | 噪声人格可用 + mutation nightly 生效 |
| M3 | R-A2 + R-A3 | fuzz 30× 全绿 + 至少 3 档谱系探测器 + nightly 作业 |
| 每阶段收口 | `ruff` 全净；测试全绿门槛引用 A-R1 的演进类 claim（`pytest --collect-only` 计数，禁手写绝对数）；`opsenv.suite --gate` 14 条不变 | 不回退现有能力 |

## 7. 边界纪律（触碰即停、上报）

* `docs/semantics.md` 的承诺与实现会出现分歧；
* 出现"生产级 / 高可用 / 服务化"口径或对外可靠性数字；
* 现有 14 条门禁阈值被调整；
* 需要把 API key、远程服务变成默认路径；
* fuzz 报告出现"完备""覆盖所有窗口"级的表述。
