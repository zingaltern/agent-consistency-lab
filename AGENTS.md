# AGENTS.md — 给所有 AI agent 的入口约束

本仓库是**面向崩溃一致性与治理语义的 Agent runtime 实验台**（不是产品、不是框架替代品）。
任何开发或测试会话（人类或 AI）在动手前，先读这三份文件：

| 文件 | 作用 |
|---|---|
| [`docs/semantics.md`](docs/semantics.md) | 运行时**承诺清单**——代码、测试、文档的唯一口径来源 |
| [`docs/development.md`](docs/development.md) | 开发规范：分支流程、合并门槛、代码与文档纪律 |
| [`docs/testing.md`](docs/testing.md) | 测试规范：必跑命令、改动→测试义务、自证作弊清单 |

## 红线（违反即不得合并）

1. **禁止直接推 `main`。** 一切改动开分支（`feat/` `fix/` `exp/` `audit/` `docs/` `chore/`），
   **验证全绿后才允许合并**——流程与门槛见 `docs/development.md` §2。
2. **事件日志是唯一权威**；`events` 表 append-only 由触发器物理保证，不得绕过或移除。
3. **裁判必须是外部账本**（`world.db`，读取时连 `-wal` 一起快照），不得用 runtime 自己的日志；
   **崩溃注入必须真 SIGKILL 子进程**，不得 mock 或"模拟抛异常"。
4. **文档里的每个数字都要有可再生命令**；改代码导致数字变化 → 同步更新所有引用处
   （README / HANDOFF / docs/ / reports/）。历史 P2 里 7/8 都是这条没做到。
5. **每个缺陷修复附回归用例**（写明"修复前会怎样"）；**每条新门禁须做退化注入验证**
   （把机制改坏 → CI 必须变红）。
6. **禁止自证**：不得用 ground truth 喂被判定的机制；跨系统比较的随机种子不得含 system 名
   （CRN 规则）；holdout 冻结，不得拿去调参。
7. **不得使用"生产级""高并发""提升了 X%"这类措辞**；结论只以配对差值 + 置信区间报告，
   并写明"本结论不适用的范围"。
8. **测试 agent 只读仓库**：全部产物写 `/tmp`，不得为让测试通过而修改被测代码。
9. **`packaging/`、`.zcode/` 已被 `.gitignore` 排除**，属于本地内容，不得提交、不得引用其措辞。
10. **脚本化批量改动必须逐条 assert**；提交信息用中文，说清"为什么"而不是"改了什么"。

## 最短工作流

```bash
git switch main && git pull
git switch -c feat/<slug>
# ... 开发 ...
.venv/bin/pytest -o addopts= -p no:cacheprovider -q    # 全绿（用例数用脚本计数，不手写）
.venv/bin/ruff check .
# 语义相关改动还要跑：
.venv/bin/python -m experiments.crash_matrix --repeats 5
.venv/bin/python -m opsenv.suite --per-fault 8 --repeats 3 --gate
git add -A && git commit
# 合并（二选一，取决于 main 是否已开启 branch protection，见 docs/development.md §2.2/§2.4）：
git push -u origin <branch>                                     # ← 受保护：推分支后在 GitHub 开 PR 合并
git switch main && git merge --no-ff <branch> && git push origin main  # ← 未受保护：本地合并直推
```

不确定某件事该不该做时，先问自己：**这条改动让什么变得可验证了？** 答不上来就先别动手。
