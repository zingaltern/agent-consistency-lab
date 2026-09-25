# W6 报告：分段汇报、统计口径与 CI 门禁

**日期**：2026-09-16 ｜ **代码**：`opsenv/stats.py`、`opsenv/suite/`（新增统计/门禁/分段段）、
`.github/workflows/ci.yml` ｜ **数据**：`reports/w6_eval.json`（64 场景 × 4 系统 × 2 推理器 × 3 次 = 1536 次）

---

## 一、这一轮解决的是"结论能不能站住"

W5 给出的是一堆点估计（"红线率 20.3% vs 0%"，CRN 修正前的口径；修正后为 20.8%，
见 `reports/w6_eval.json` `cells[].rates.red_line`）。W2/W3 的评审明确指出：
**点估计没有区间等于不可辩护**，n 小的时候更要把"无法区分"说出口。W6 补齐三件事：

1. **区间与配对比较**：单比例用 Wilson 区间；两条路线的差值用**配对 bootstrap**
   （配对键 = 场景 × 推理器人格 × 重复序号），因为比较的是"同一道题上的表现差"。
2. **分段汇报**：dev / holdout 分开报，holdout 是冻结考卷。
3. **门禁**：把安全不变量、实验设计假设、统计结论编码成可执行检查，CI 里跑。

---

## 二、统计口径（`opsenv/stats.py`）

| 工具 | 用途 | 关键实现细节 |
|---|---|---|
| `wilson_interval` | 单比例区间 | 小样本比正态近似稳；0/0 与 n/n 都有界 |
| `paired_bootstrap_diff` | 配对差值区间 | 重采样**配对差值**而不是两组独立样本；A−B 定义写死 |
| `min_detectable_effect` | 说明"多大的差距才可辨" | 独立两样本公式；n=192、p≈0.9 时约 **6.0%** |
| `paired_min_detectable_effect` | 配对结论专用的 MDE | 不一致对（discordant pairs）+ 80% 功效；n=192、p≈0.9 时约 **9.2%** |
| `cohens_kappa` | 代理指标与真值的一致性 | 双方都无方差时约定返回 0，由调用方标注"κ 不适用" |

**最小可检测效应有三个数字，引用时必须写明用的是哪一个**（三者都已在
`reports/w6_eval.json` 的 `statistics` 里逐条物化，各自一条 claim）：

| 口径 | 本样本量的值 | 用在哪 |
|---|---|---|
| **配对口径（80% 功效）约 9.2%** | `min_detectable_effect_paired` | **解释配对结论只能用这一个**（本报告 §二、§五与 `pitch`） |
| 独立两样本口径（p=0.5）约 10.0% | `min_detectable_effect_independent_p05` | 当两条路线**不**配对时的粗口径 |
| 独立两样本口径（p≈0.9）的 95% CI 半宽约 6.0% | `min_detectable_effect_independent_p90` | **没有功效项**，不能当 MDE 引用 |

早期版本只物化了 6.0% 这一个，并把它当成"最小可检测效应"引用——那既低估了配对比较
的 MDE（独立两样本公式用在配对数据上，约低估 1.7 倍），又漏了功效项。
（独立验证 2026-09-19 · P2-3。）

**本轮的统计结论**（weak-guesser 人格，配对 192 组；`reports/w6_eval.json`
`statistics.paired_harness_vs_single_shot`）：

| 比较 | 差值 | 95% CI | 判定 |
|---|---|---|---|
| harness − single_shot 的**诊断正确率** | +0.000 | [0.000, 0.000] | **不可区分** |
| harness − single_shot 的**红线执行率** | −0.208 | [−0.266, −0.151] | 可区分 |

也就是说：**"谁更准"在这个样本量下说不了；"谁更安全"是统计显著的。**

---

## 三、分段汇报（dev / holdout）

| 系统 | 推理器 | 分段 | 运行数 | strict 正确（95% CI） | 红线率（95% CI） |
|---|---|---|---|---|---|
| harness | competent-honest | dev | 144 | 0.903 [0.843, 0.941] | 0.000 [0.000, 0.026] |
| harness | competent-honest | **holdout** | 48 | 0.896 [0.778, 0.955] | 0.000 [0.000, 0.074] |
| harness | weak-guesser | holdout | 48 | 0.583 [0.443, 0.712] | 0.000 [0.000, 0.074] |
| langgraph | competent-honest | holdout | 48 | 0.896 [0.778, 0.955] | 0.000 [0.000, 0.074] |
| single_shot | competent-honest | holdout | 48 | 0.896 [0.778, 0.955] | 0.062 [0.021, 0.168] |
| single_shot | weak-guesser | holdout | 48 | 0.583 [0.443, 0.712] | 0.229 [0.133, 0.365] |
| workflow | 两者 | holdout | 48 | 0.500 [0.364, 0.636] | 0.000 [0.000, 0.074] |

（以上逐行取自 `reports/w6_eval.md` 的 dev/holdout 分段表〔`opsenv.suite` 输出〕；
完整 16 行〔4 系统 × 2 人格 × 2 分段〕以该表为准。）

**没有 dev→holdout 的性能落差**（harness 90.3% vs 89.6%；single_shot 红线率 20.1% vs 22.9%）。
这符合预期：本轮没有做任何参数调优，因此**这份 holdout 目前还没被"用过"**。
它的价值在于：W7 做阈值扫描时必须只看 dev，holdout 只在最后跑一次。

---

## 四、评分口径敏感性（`reports/w6_eval.json` `grader_sensitivity`）

| 口径 | 含义 | harness | langgraph | single_shot | workflow |
|---|---|---|---|---|---|
| `strict` | 诊断与动作都对 | 90.1% | 90.1% | 90.1% | 50.0% |
| `cause_only` | 只看根因 | 90.1% | 90.1% | 90.1% | 50.0% |
| `safe` | 只看有没有踩红线 | 100.0% | 100.0% | 95.8% | 100.0% |

**诚实的坏消息**：`strict` 与 `cause_only` 完全相同——因为当前推理器的误判分支
**同时**改根因和动作，不存在「根因对、动作错」的样本。所以这轮口径敏感性检验
**没有被真正激活**，它证明不了"换口径结论不变"。真实模型完全可能给出
"根因对但动作过激"，那时两种口径才会分开。这条写进报告，等接入真实模型后再跑。

`safe` 口径下的排序与 `strict` 相反（single_shot 从第一掉到最后），
说明**换口径会换赢家**——这正是不能用单一口径宣称"某路线更好"的理由。

> ⚠️ **CRN 修正后的核对**：`strict` 口径下三条读证据的路线并列 90.1%（`reports/w6_eval.json`
> `grader_sensitivity`），single_shot 已不是第一，因此上一句"从第一掉到最后"只在"它不再是
> 赢家、且在 `safe` 口径下垫底"这个意义上成立；"换口径会换赢家"的方向保留，但**不能再说
> single_shot 在 `strict` 下领先**。

---

## 五、代理指标校准（κ）

生产里没有 ground truth，只能监控代理指标。问题是：代理指标值不值得信？
（本表口径：真值 = `reports/w6_eval.json` `cells[].correct`，「代理为真」= `cells[].sufficient`；
逐格 κ 见 `reports/w6_eval.md` 的代理指标校准表。）

| 系统 | 代理为真 | 真值为真 | 代理有方差 | Cohen's κ | 解读 |
|---|---|---|---|---|---|
| workflow | 96/192 | 96/192（两档人格相同） | 是 | **1.000** | 几乎完全一致 |
| single_shot | 192/192 | 173/192（competent）/ 118/192（weak） | 否 | — | 无方差，κ 不适用 |
| langgraph | 192/192 | 173/192（competent）/ 118/192（weak） | 否 | — | 无方差，κ 不适用 |
| harness | 192/192 | 173/192（competent）/ 118/192（weak） | 否 | — | 无方差，κ 不适用 |

结论：**"取证充分性"这条代理指标只对 workflow 有意义**（它的盲区正好等于它的错误）。
对 agent 路线它恒为真、没有区分力——**不能拿它当线上监控指标**。
这条结论对面试里的"你怎么监控一个上线后的 Agent"是很实用的回答：
代理指标要先证明它与真值有相关性，否则监控的是噪声。

---

## 六、门禁（CI 里真的会拦）

`python -m opsenv.suite --gate` 把以下检查编码为可执行门禁，任一条不通过即非零退出
（「实测」列取自 `reports/w6_eval.json` `gates`）：

| 门禁 | 类型 | 实测 |
|---|---|---|
| `harness.red_line[competent/weak]==0`、`langgraph.red_line[...]==0` | 安全不变量 | 0.000 ✓ |
| `harness.gated[weak]==1.0` | 审批覆盖率 | 1.000 ✓ |
| `harness.sufficient==1.0` | 取证策略按设计工作 | 1.000 ✓ |
| `single_shot.red_line[weak]>=0.10` | **实验假设自检** | 0.208 ✓ |
| `harness.blocked[weak]>0`、`langgraph.blocked[weak]>0` | **对称自检** | 0.208 / 0.208 ✓ |
| `harness.correct[competent] in [0.80,0.95]` | 正确率回归带 | 0.901 ✓ |
| `paired(harness−single_shot).red_line CI 上界 < 0` | **统计结论** | [−0.266,−0.151] ✓ |
| `catalog.holdout>0 and dev>0` | 场景集完整性 | 48/16 ✓ |

### 门禁自己也要被验证（本轮的真实教训）

第一版门禁**抓不到**一次真实退化。我故意把 `is_write_action` 改回"只认合法动作集合"
（W5 修掉的那个 bug：破坏性动作绕过审批门），suite 依然全绿——因为退化让 LangGraph
**静默丢弃**破坏性动作：它既不执行红线、也不报告拦截，指标看起来完美，机制却已经死了。

补上**对称自检**（`*.blocked[weak]>0`：有 gate 的系统必须真的拦下过东西）之后：

```
退化后 suite --gate 退出码: 1
门禁失败 1 条: langgraph.blocked[weak]>0   （实测 0.000）
还原后 suite --gate 退出码: 0
```

这条对称自检已固化成单测（`test_gate_requires_gated_systems_to_actually_block_something`）。
教训：**"没有坏事发生"不等于"机制在工作"**——门禁必须同时检查"该拦的拦住了"和"该拦的时候真的拦了"。

### CI 配置

`.github/workflows/ci.yml` 两个 job：

* `verify`：Python 3.11 与 3.12 双版本（声明的最低版本也要真跑，`uuid7` 有回退路径）
  → `ruff check` + `pytest`（含崩溃矩阵小样本与四系统断言）；
* `experiments`：跑崩溃矩阵（repeats=2）、上下文成本实验、四系统评测（`--gate`），
  报告作为 artifact 上传。任一实验出现 `prediction-violated` 或门禁失败即红。

---

## 七、边界与未决

* **口径敏感性未被激活**（见第四节），本轮不能声称"换口径结论不变"；
* holdout 尚未被真正当作考卷使用（没有调参过程），W7 的阈值扫描必须只看 dev；
* 仍无真实模型：所有结论限定在"模拟推理器 + 合成场景"的范围内；
* CI 未跑 W4 的缓存/压缩三维曲线（W7 才产出），也未做 nightly 全量重复（当前 repeats=3）。

---

## 八、W7.5 审计修复（统计与门禁）

四路审计对 W6 的统计工具与门禁提出的问题，已修并固化：

| 问题 | 修法 |
|---|---|
| `wilson_interval` 边界浮点缺陷：k=n 时上界 < 1.0、k=0 时下界 > 0（使全零样本的 `excludes_zero=True`） | 显式钳位到点估计之外并固定 0/1 边界；新测试覆盖 k=0/n |
| `min_detectable_effect` 用**独立两样本**公式解释**配对**比较（低估约 1.7 倍） | 新增 `paired_min_detectable_effect`（discordant 对数 + 80% 功效项），报告改用它 |
| 门禁无最小样本量：`--per-fault 3 --repeats 1`（n=24/格）12 条全绿；1 对数据也能过配对门禁 | 新增 `all_cells_present` 与 `min_runs_per_cell>=30` 两条前置门禁；`--per-fault 0` 直接退出码 2 |
| 缺格时安全不变量**空真通过**（`rate()` 对不存在的格返回 0.0） | 由前置门禁拦住；缺格即红 |
| "三条路线不可区分"只比较了一对（harness−single_shot），遗漏的 langgraph−single_shot 恰是最大的 | `findings()` 现在跑**全部两两配对比较**并逐对给 CI；且 CRN 修正后三对差值全为 0（真不可区分） |
| "把 20%/30% 的**出错场景**交给人工"分母错误 | 改为 `blocked / 出错 run 数` = 54%/54% |
| 空数据时打印"不可区分（CI 含 0）"与"MDE=100%" | `findings`/`statistical_notes`/`proxy_section` 对空输入改为明说"无数据，不做统计陈述" |

关键的一条：**"不可区分"现在是真的**（CRN 之后三条取证路线同分、CI 恰好为 [0,0]），
而不是"只测了一对就外推"。同时"可区分"的那条（红线率）依然稳：配对 CI [−0.266, −0.151]。
