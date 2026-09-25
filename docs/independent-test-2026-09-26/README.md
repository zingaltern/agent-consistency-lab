# 独立黑盒测试的准备物（模板，2026-09-26 建立）

> ⚠️ **本目录不是一次完成的测试，而是一套"待真人执行"的准备物。**
> 按仓库红线（`AGENTS.md` 红线 7、`docs/testing.md` §4-7/§6）：**测试 agent 只读仓库**，
> 报告必须由**真人**跑出来、由真人写；**任何 agent 不得代跑并把结论写成"测试已完成"**。
>
> 用法：把本目录**复制**成 `docs/independent-test-<你实际执行的日期>/`，
> 在那里跑 `commands.sh`、填 `report.md`。复制而不是就地填，是为了让"哪一天跑的"没有歧义。

---

## 1. 一分钟上手

```bash
# 0) 准备一个干净环境（仓库只读，产物全部写 /tmp）
git clone <repo> /tmp/icl-tester && cd /tmp/icl-tester
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,eval]"

# 1) 先读"规格"（不要先读"答案"，见 docs/tester-prompt.md §2）
#    README.md / docs/semantics.md / docs/HANDOFF.md / 全部源码与 --help

# 2) 跑一键入口：四套基线命令，产物落 /tmp/independent-test-<日期>/
bash docs/independent-test-2026-09-26/commands.sh

# 3) 按 docs/tester-prompt.md 的 §3 必测项 A–F 自己设计测试（基线只是起点）

# 4) 写报告：复制 report.md 的骨架，逐节填满（含"与作者声称的差异"一节）
```

## 2. 四套基线命令（`commands.sh` 逐条跑的就是这些）

| # | 命令 | 它回答什么 | 产物（全部在 `/tmp`） |
|---|---|---|---|
| 1 | `.venv/bin/pytest -o addopts= -p no:cacheprovider -q` | 用例集是否全绿；用例数用 `scripts/count_tests.py` 计数（正文不手写） | `$OUT/pytest.log` |
| 2 | `.venv/bin/ruff check .` | 风格门禁是否全净 | `$OUT/ruff.log` |
| 3 | `.venv/bin/python scripts/check_facts.py --run verify` | 文档里的数字能否被命令再生（逐条重跑比对） | `$OUT/facts-verify.json` + `.log` |
| 4 | `.venv/bin/python -m experiments.crash_matrix --repeats 5` | 6 个命名崩溃窗口 × 16 格是否逐格 `as-predicted` | `$OUT/matrix.json` + `$OUT/matrix.md` |

补充（按需，不属于"四件套"）：`python -m opsenv.suite --per-fault 8 --repeats 3 --gate`
（四路线对照 + 门禁，退出码非零 ⇔ 有条门禁红）、
`python -m experiments.context_sweep --repeats 2`（阈值扫描，约 38 秒）。

**退出码是判据**：脚本会把每条命令的退出码单独打印出来——不要只看输出文本
（管道/重定向会吞退出码，历史事故见 `docs/testing.md` §2 末段）。

## 3. 这个目录里有什么

| 文件 | 说明 |
|---|---|
| `commands.sh` | 一键入口：四套基线命令 + 退出码汇总 + 产物路径打印；**自检"没有写进仓库"** |
| `report.md` | 报告模板：分节骨架 + 每节要求；顶部带"未完成"横幅，防误读 |
| `README.md` | 本文件（协议与红线） |

## 4. 红线（照 `docs/tester-prompt.md` 与 `docs/testing.md` §4）

1. **仓库只读**：不得为了"让测试通过"改代码、数据或文档；要构造故障就复制到 `/tmp` 再改。
2. **产物只写 `/tmp`**（`commands.sh` 末尾会核对 `git status --porcelain` 是否干净）。
3. **先规格、后答案**：`docs/w2..w7`、`reports/`、`tests/`、`docs/posts/`、`docs/resume.md`
   是"答案"，**结论定稿之后**才读，并单列"我的结论与作者声称的差异"。
4. **裁判必须是外部账本**（run 目录的 `world.db`，读时连 `-wal`/`-shm` 一起快照）；
   不得拿 runtime 自己的日志当"效果发生了几次"的证据。
5. **不得把"没测出错"写成"不会出错"**：n 次全绿要按 rule of three 报失败率上界。
6. **不得由 agent 代跑并声称已完成**：报告的每一节都要有真人跑过的命令与输出要点。
