# 开发规范（Development Guide）

> **约束对象**：所有后续开发会话（人类或 AI agent）。
> **一句话版本**：分支开发 → 全绿验证 → 才允许合并进 `main`；文档里的每个数字都必须能被命令再生。

---

## 0. 动手之前先读什么

| 顺序 | 文件 | 为什么 |
|---|---|---|
| 1 | [`docs/semantics.md`](semantics.md) | 运行时**承诺清单**。代码、测试、文档都以它为口径；**要改语义，先改这里** |
| 2 | [`docs/HANDOFF.md`](HANDOFF.md) | 压缩后的工作上下文：硬事实、七轮结论、八次真实翻车的教训 |
| 3 | 本文 | 流程与纪律 |
| 4 | [`docs/testing.md`](testing.md) | 验证门槛与测试写法 |

如果任务来自 `docs/design/*.md`（每份都自包含，含 §0 第一性原理），先读该文档再动手。

---

## 1. 硬约束（违反即不得合并）

1. **禁止直接向 `main` 推送。** 一切改动走分支；合并前必须通过 §3 的全部门槛。
2. **事件日志是唯一权威。** 状态永远是日志的折叠物；不得引入任何"第二权威"持久状态
   （checkpoint 已明确降级为边界快照 + 交叉校验，不要再把它升级回去）。
3. **`events` 表 append-only 由 SQLite 触发器物理保证**；不得移除、绕过或"临时关掉"触发器。
   发现触发器挡路，说明设计走错了方向，改设计而不是改触发器。
4. **runtime 运行时依赖只允许 `pydantic`**（见 `pyproject.toml`）。需要新依赖时放进
   `[extra]` 并在提交信息里说明理由；核心内核保持"零编排框架依赖"。
5. **文档里的每个数字都必须有对应的可再生命令。** 改代码导致数字变化时，必须同步更新
   **所有**引用该数字的位置（README / HANDOFF / docs/*.md / docs/posts/ / reports/）。
   历史上 8 个 P2 里有 7 个都是"改了代码没回灌文档"造成的。
6. **不得使用"生产级""高并发""提升了 X%"这类措辞。** 这是实验台，不是产品：
   结论只以"相对基线的配对差值 + 置信区间"报告，绝对百分比只在有对照时使用。
7. **对外口径材料与仓库严格分离**（`packaging/`，已被 `.gitignore` 排除）。不得提交，
   也不得把其中的措辞带进仓库文档。
8. **崩溃注入与裁判不得被替换成 mock**：必须真实 SIGKILL 子进程，必须由外部副作用账本
   （`world.db`）裁决。理由与反例见 [`docs/posts/01-crash-semantics.md`](posts/01-crash-semantics.md)。

---

## 2. 分支与合并流程

### 2.1 分支命名

| 前缀 | 用途 | 示例 |
|---|---|---|
| `feat/` | 新功能 / 新机制 | `feat/record-replay-model` |
| `fix/` | 缺陷修复 | `fix/systems-subset-crash` |
| `exp/` | 实验与测量（不改语义） | `exp/crash-window-fuzz` |
| `audit/` | 审计与结论核验 | `audit/w9-review` |
| `docs/` | 文档 | `docs/agent-guides` |
| `chore/` | 工程杂项（CI、依赖、整理） | `chore/ci-nightly` |

### 2.2 标准流程

```bash
# 1) 从最新 main 开分支
git switch main && git pull
git switch -c feat/<slug>

# 2) 开发（小步提交，一个提交一件事）

# 3) 自检：§3 的合并门槛逐条跑过，§5 的自检清单逐条确认
git status                 # 确认没有意外文件（.zcode/、packaging/ 不应出现）

# 4) 提交并合并
git add -A && git commit
git switch main && git merge --no-ff <branch>
git push origin main
```

**开启远端 branch protection 后（推荐，见 §2.4），流程强制切换为 PR 模式**：

```bash
git push -u origin <branch>        # 推分支
# 在 GitHub 上开 PR → CI 全绿 → 用 merge 按钮合并 → 删分支
```

此时本地 `git merge` 后再 `git push origin main` 会被服务端**拒绝**——这是有意的：
它把"禁止直推 main"从人工约定变成服务端强制。CI 不绿禁止合并；
评审意见属于 P0/P1 的必须修复后才能合并。

### 2.3 合并门槛（Definition of Done）

| # | 门槛 | 命令 |
|---|---|---|
| 1 | 测试全绿（用例数见 claim `tests-collected`，**不手写**） | `.venv/bin/pytest -o addopts= -p no:cacheprovider -q` |
| 2 | lint 全绿 | `.venv/bin/ruff check .` |
| 3 | 崩溃矩阵逐格 `as-predicted` | `.venv/bin/python -m experiments.crash_matrix --repeats 5` |
| 4 | 评测门禁全过（14 条） | `.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate` |
| 5 | 改动涉及的实验重跑并与产物对账 | `experiments.context_cost` / `experiments.context_sweep --repeats 2` |
| 6 | 文档数字与 `reports/*.json` 一致 | 见 §4 |
| 7 | 新增/修改的机制附回归用例 | 见 [`testing.md`](testing.md) §3 |

* W9 起还多了两条**不属于合并门槛、但会红的**作业（都跑在 nightly，失败同样要处理）：
  `mutation`（变异门禁：**盲区扩大**就红——新增幸存变异、基线里有判定的变异体本轮变成
  无结论/未覆盖、基线变异体整条缺失、新增 `no tests`、无结论集合增长、
  以及 mutmut 出现了未归类的新状态）与 `chaos-fuzz`（随机时刻注入 + 三档谱系探测器）。
  本地复现命令见 [`testing.md`](testing.md) §2；判据与退化注入记录见
  [`design/2026-09-18-mutation-segfault-investigation.md`](design/2026-09-18-mutation-segfault-investigation.md)。
  门禁**每次运行都打印不可见空间**（无结论类 + 未覆盖类）的规模：
  `survivor_rate` 不是覆盖率，不得当质量分引用。
* 门槛 6 的执行者是 **CI job `facts`**：`.venv/bin/python scripts/check_facts.py --run verify`
  逐条重跑 `reports/documented-facts.json` 里的轻 claim 并比对 JSON 路径；
  整量类（`crash_matrix --repeats 5`、1536 次评测、阈值扫描）在
  `.github/workflows/nightly.yml` 里跑（`--run nightly`）。改代码导致数字变化时，
  先跑这条命令，再回灌文档。
* 涉及**崩溃语义 / 审批 / 幂等 / 恢复路径**的改动：门槛 3 必须跑满 `--repeats 5`。
* 纯文档改动：门槛 1、2 必跑，3–5 不要求；§4 全部适用。
* 任何"跑得慢所以跳过"的决定都要在提交信息里写明跳过了哪条、为什么。

### 2.4 远端一次性设置（人工，在 GitHub 网页操作）

Settings → Branches → **Add branch protection rule**：

* Branch name pattern：`main`
* ✅ Require a pull request before merging
* ✅ Require status checks to pass（选择 CI 的 `verify (3.11)` 与 `verify (3.12)`）
* ✅ Do not allow bypassing the above settings
* ❌ 关闭 force push / 禁止删除分支

配置完成后，"禁止直推 main" 就由远端服务端强制，而不只是靠约定。两个后果要提前知道：

* 开启 **Require a pull request** 后，任何直接推送到 `main` 的操作（包括本地 merge 后的 push）
  都会被拒绝——所有合并必须走 PR；
* 开启 **Require status checks** 后，PR 只有在 CI（`verify (3.11)`、`verify (3.12)`）全绿时
  才能点亮 merge 按钮。

需要临时绕过时（例如紧急修文档），在提交信息里写明原因，或临时关闭规则并在合并后立即恢复。

---

## 3. 代码纪律

* **风格即法规**：`ruff` 配置（line-length 100、target py311、select E/F/I/UP/B/SIM/RUF）
  是唯一风格标准，不要再引入第二套。
* **模型接入的边界**（W9）：三种模型模式（`scripted`/`record`/`replay`）走同一条 loop，
  **不得**为某一种模式改承诺层代码；录制物默认写 `/tmp`，key 只从环境变量读，
  绝不写进仓库文件、报告或事件 payload。
* **确定性**：任何涉及随机的地方必须显式播种；评测的种子遵循 **CRN 规则**——
  `f"{profile.seed}:{scenario.id}:{repeat}"`，**不含 system 名**（含了会产生哈希伪影，
  审计实测 p=0.0009）。崩溃注入的子进程固定 `PYTHONHASHSEED=0`。
* **schema 变更**：`harness/store/schema.py` 的 DDL 改动必须配迁移，并递增 `SCHEMA_VERSION`；
  旧 run 目录必须仍可读。
* **错误分类**（`harness/loop.py::_handle_tool_failure`）：非幂等写的异常 → `unknown`（可能已发生，
  等对账）；只读/幂等 → `failed`。**异常绝不允许穿透 loop**（那是审计实测的 P0）。
* **ID 与指纹**：新 ID 一律用 `harness/ids.py::new_id` 生成；涉及内容比较时用规范化哈希
  （`canonical_json` / `canonical_args_sha256`），不要另外发明序列化方式。
* **文档内引用代码位置**要写相对路径 + 符号名（如 `harness/loop.py::_execute_tool`），
  不要写行号（行号会漂移）。

---

## 4. 文档与数字纪律

1. **数字的唯一来源是 `reports/*.json`**（由命令再生的产物）。文档正文只允许引用这些数字或
   给出再生命令；不允许"手算"或从旧文档抄。
2. **改代码 → 回灌文档**：提交前用这条命令找出可能受影响的文件，逐个确认：

   ```bash
   grep -rn "<被改动的数字或结论关键词>" README.md docs/ reports/ | grep -v reports/*.json
   ```

3. **口径必须显式**：写"70 次真实 SIGKILL"必须同时能说清分母（16 格 × 5 次 = 80 次运行，
   两个对照格按设计不注入）；写"配对 192 组"必须能说清配对键（场景 × 人格 × 重复序号）。
   历史数字（如 W3 的 65 次）不要与新数字混引。
4. **入库产物的再生成**：改动影响 `reports/*.md` 或 `reports/*.json` 的内容时，必须重新生成并提交；
   逐次明细（`reports/*_runs.json`）不入库。注意 `avg_wall_ms` 一类计时噪声不算回归，
   重新生成时确认只有噪声在变再提交。
5. **中文排版**：正文统一使用中文引号「」，避免中英引号混用截断字符串的历史事故；
   代码/命令一律用反引号包裹。
6. **报告类文档的写法**：先给结论、再给证据、最后给边界（"本结论不适用的范围"）。
   边界章节不是可选项——没有边界章节的结论类文档不允许合并。

---

## 5. 提交规范

* 提交信息**用中文**，格式：`<类型>(<范围>): <一句话说清"为什么">`，类型可用
  `feat / fix / exp / audit / docs / chore`，范围写模块名（如 `opsenv`、`harness/store`）。
* **一个提交只做一件事**：代码与"回灌文档"可以分开提交，但必须在同一个分支上成对出现。
* **脚本化批量改动必须逐条断言**：任何用脚本做的多处替换，必须对每处替换写 `assert`
  或验证命令——历史上出现过"4 处替换 3 处静默失败，然后拿旧代码跑出结论"的事故。
* 提交前跑一遍 §6 清单；提交信息里无法回答"这条改动让什么变得可验证了"时，重新想清楚再提交。

---

## 6. 自检清单（复制即用）

```
[ ] 我读了 docs/semantics.md，且我的改动没有让它变旧（变了就先改它）
[ ] 我在独立分支上开发，main 未被直接修改
[ ] pytest 全绿（用例数用 `scripts/count_tests.py` 计数，不凭记忆写）；ruff 全绿
[ ] 改动涉及崩溃/审批/幂等 → crash_matrix --repeats 5 全格 as-predicted
[ ] 改动涉及评测/统计/门禁 → opsenv.suite --gate 全过
[ ] 我改动或引用的每个数字都能由命令再生，且所有引用处都已同步
[ ] 新增机制/修复缺陷都附了回归用例（写明"修复前会怎样"）
[ ] 没有新增"第二权威"状态；没有绕过 append-only 触发器
[ ] 没有 mock 掉崩溃注入或外部账本裁判
[ ] 提交信息说清了"为什么"，脚本化改动逐条 assert 过
[ ] 合并后 main 已推送到 origin，工作区干净
```
