# 开发任务单：交付设计文档 C（外围集成：MCP 工具服务 + 可观测导出落地）

> 用途：把本文件全文复制粘贴给开发 agent 作为任务提示词。
> 生成时间：2026-09-18 ｜ 对应设计文档：`docs/design/2026-09-18-integrations.md`（下称 **C**）

---

## 0. 你的身份与任务

你是本仓库（agent-consistency-lab，一个 Agent runtime 实验台）的**开发 agent**。
任务：按设计文档 C，做**外围集成**——把治理层接成 MCP 工具服务、把既有的 OTLP 导出
落到一个真实后端，并**把文件与依赖边界做干净**。

先想清楚这件事的性质：**集成层不产生结论**。它只改变"谁能调用这些工具"和
"别人用什么工具看这些数据"。任何让它听起来更像产品（"平台""服务化""生产可用"）的
措辞或设计，都是越界。

---

## 1. 第一步：建立纪律（读不完不许动代码）

1. `AGENTS.md`（红线）
2. `docs/semantics.md`（承诺清单——**本包不改任何一条**）
3. `docs/HANDOFF.md`（硬事实与八次翻车教训）
4. `docs/development.md`（分支流程 + 7 条合并门槛）
5. `docs/testing.md`（测试规范 + 自证作弊禁令）
6. **`docs/design/2026-09-18-integrations.md` 全文**（含 §0 第一性原理、§3 文件管理、
   §4 预置对抗审查、§5 开放问题、§8 边界纪律）
7. 参考实现：`harness/loop.py::_execute_tool`（七步管线）、`harness/tools.py`、
   `harness/approval.py`、`harness/otel.py`（extra 的既有写法）、
   `tests/test_otel.py`（"顶层不引入新依赖"的守法）

---

## 2. 第二步：先回答开放问题（答完再动手）

写成 `docs/design/2026-09-18-open-questions-answered.md`（分支的第一个提交），
每条给出**结论 + 依据（源码位置 / 一次实验的输出）**：

* **C §5 的第 1 条是技术裁决，最要紧**：如何在 MCP 形态下复用七步执行管线？
  (a) 给 loop 加"无模型模式" 还是 (b) 把 `_execute_tool` 抽成可复用单元？
  设计文档倾向 (b)——请给出抽取边界，并说明**既有 345 个用例的受影响面**，
  以及"两处调用点行为一致"的证明方式（不是"我看了一遍"，要可执行）。
* 其余 4 条（会话与 run 的映射、预算归属、崩溃演示口径、可观测后端交付方式）逐条给结论；
  其中**第 5 条需要所有者拍板**，不要自行决定——先给候选与前置条件，标为"待所有者确认"。

---

## 3. 第三步：执行顺序（一个里程碑一个分支）

**顺序：C-M1 → C-M2 → C-M3**。

每个里程碑严格执行：

```bash
git switch main && git pull
git switch -c <feat|fix|chore>/<slug>
# 只做该里程碑范围内的需求
.venv/bin/pytest -o addopts= -p no:cacheprovider -q          # 必须全绿
.venv/bin/ruff check .
.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate
.venv/bin/python -m experiments.crash_matrix --repeats 5
.venv/bin/python scripts/check_facts.py --run verify          # 新增数字要登记 claim
git add -A && git commit && git push -u origin <branch>
```

每个里程碑收口时**停下汇报**（分支名、验收门逐条结果、新增用例数（脚本计数）、
未满足项与原因、下一步计划），等确认后再进下一个。合并走 PR（`main` 有分支保护）。

---

## 4. 硬性纪律（违反 = 返工）

1. **禁止直推 `main`**；禁止 force push。
2. **内核零侵入**：
   * `harness/`、`opsenv/`、`fakeworld/`、`experiments/` **不得 import `integrations/`**；
   * `import harness.*` 不得触发任何 MCP SDK 的 import（延迟 import，照 `harness/otel.py`）；
   * 内核依赖仍只有 `pydantic`；MCP SDK 只进新增的 `[mcp]` extra，**不得进 `dev` extra**。
   * 这三条要有**测试守住**，不是靠自觉。
3. **不得绕过七步管线**：MCP 的 `tools/call` 必须复用既有实现（含审批门、幂等键含 branch 维度、
   outbox 三态、ArgPolicy、TOCTOU 复核）。发现管线难复用，回到 §5 开放问题 1 重新裁决并上报。
4. **不新增权威状态**：不设会话数据库、不缓存审批结论、不在内存里放 run 的权威副本；
   状态一律从事件日志折叠。
5. **审批默认形态不依赖 elicitation**：探测到客户端不支持必须自动退回，且退回路径有测试。
6. **崩溃演示必须有对照组**（只报"1 次"不算演示）；必须真 SIGKILL 子进程；
   外部账本读 `world.db` 时连 `-wal` 一起快照。
7. **可观测那条不得进入任何验收门前置**：本机网络对部分域名不稳定、Docker 未必可用；
   给至少两条路径与前置条件，并写明三条口径（时长是推导值 / 事后导入 / trace 里没有账本）。
8. **CI 与时长**：不加新作业；给 `verify` 增加的时长 **≤ 5 秒**，超出必须在 PR 说明取舍；
   `opsenv.suite --gate` 的 14 条语义与阈值**一律不动**。
9. 数字不手写（脚本计数）；文档引用用锚点；提交信息中文、说清"为什么"。

---

## 5. 验收判据（全部达成才算完成）

| 里程碑 | 验收门 |
|---|---|
| **M1** | 外部 MCP 客户端（用官方 SDK 的 stdio 客户端即可，**不要求装第三方 App**）能列出工具并调用只读工具；写操作停在审批门并返回可读的待审批结果；`approve`（含 reject / **edit 改参**）后续跑；**管线复用裁决已落地，且有"两处调用点行为一致"的可执行证明** |
| **M2** | 强杀 MCP 服务进程 → 重启 → 同一 run 续跑 → 外部账本 **1 次**；**对照组（outbox off / 下游不幂等）显示重复**；全程真 SIGKILL 且有崩溃位置证据 |
| **M3** | 可观测路径文档（含两条后端路径与三条口径）；`integrations/` 与 extra 边界就位并有依赖方向测试；`verify` 时长增长 ≤ 5 秒；新增数字登记为 claim；README 增补一小节（不得出现"平台/服务化"措辞） |
| 每阶段 | 既有 `345+` 用例全绿；`ruff` 全净；`opsenv.suite --gate` **14 条不变**；`crash_matrix --repeats 5` 16 格 as-predicted；内核依赖仍只有 `pydantic` |

---

## 6. 收口汇报模板（最后一个里程碑后提交）

```markdown
# 交付汇报：设计文档 C（外围集成）

## 目标达成
| 目标 | 状态（达成/部分/未达成） | 证据（命令 + 产物路径） |
（C-G1..G5 逐条）

## 里程碑验收门结果
（M1/M2/M3 逐条：通过与否 + 证据命令 + 未通过原因）

## 边界自检
- 内核依赖清单（应只有 pydantic）与依赖方向测试结果
- verify 时长变化（秒）与取舍说明
- 文档措辞自检：是否出现"平台/服务化/生产级"（应为零）

## 遗留与建议
（未决问题、需要所有者拍板的事项、你发现的与设计文档不符之处）
```
