# 交付汇报：设计文档 C（外围集成：MCP 工具服务 + 可观测导出落地）

* 日期：2026-09-18
* 分支（堆叠，各自 PR，由所有者在网页端合并）：`docs/integrations-open-questions` → `feat/mcp-tool-service` → `feat/mcp-crash-demo` → `feat/integrations-boundaries`
* 一句话：治理层现在能被外部 MCP 客户端调用、也能被真 SIGKILL 之后续跑，**而且没有第二套执行语义**——
  三次抽取 + 一致性证明的机制见下。

---

## 目标达成

| 目标 | 状态 | 证据（命令 + 产物路径） |
|---|---|---|
| **C-G1** 治理型 MCP 工具服务（stdio，本地单用户） | **达成** | `python -m integrations.mcp_server --run-dir DIR`（`integrations/mcp_server.py`）；外部验收 `tests/test_mcp_server.py`（官方 SDK stdio 客户端，9 例）；治理语义 `tests/test_mcp_mapping.py`（23 例）；工具清单可再生产 `--list-tools`（claim `mcp-tools-declared` = 6、`mcp-governance-tools` = 2） |
| **C-G2** 审批在 MCP 形态下可用且可移植 | **达成** | 默认形态＝把"待审批"作为普通工具结果返回；`list_pending_approvals` / `approve`（approve / reject / **edit 改参**）走既有 `decide()`；elicitation **从不使用**（用例注册 handler 并断言从未被调用）。改参沿用 `__edit1` 语义（原调用 superseded + 新幂等键），未另写一套 |
| **C-G3** 崩溃语义在集成形态下成立且可演示 | **达成** | `python -m integrations.mcp_crash_demo --work-root /tmp/mcp-crash`：`with_outbox` max_per_key=1（`reconstructed_from_probe=true`）、对照 `without_outbox` max_per_key=2，两格**真 SIGKILL**（退出码 137 + `crash_marker.json` 的 window/occurrence/counts/pid）。claim `mcp-crash-exactly-once` / `mcp-crash-control-duplicate` / `mcp-crash-real-kills` |
| **C-G4** 可观测导出落到真实后端（可选路径） | **达成（含边界）** | `integrations/observability.md` ＋ 路径①本机实测：`python -m integrations.otlp_local_sink --selftest`（span=7 / tool=2 / model=3 / derived=3 / `has_ledger_attributes=false`，claim `otlp-local-sink-*`）；路径②Jaeger、③任意 OTLP/HTTP 后端**本机未实测并如实标注**（无 Docker、需凭据）。三条口径写在文档 §0，并有**反向断言**（trace 里没有账本） |
| **C-G5** 目录与依赖边界可审计 | **达成** | `integrations/` 独立目录 + `[mcp]` extra（**不进 dev**）+ `mcp` marker（默认过滤）；依赖方向用例 10 例（AST 全量扫描内核目录 + 子进程验证 `import harness.*` 不拉 MCP）；内核依赖仍只有 `pydantic` |
| **R-C6** 新增数字登记为 claim | **达成** | 新增 11 条 claim（工具数 / 各层用例数 / 崩溃三格 / 可观测四项）；`check_facts.py --run verify` 逐条通过 |

---

## 里程碑验收门结果

### M1（`feat/mcp-tool-service`）：抽取 + 工具服务 + 审批形态

| 验收门 | 结果 | 证据 |
|---|---|---|
| 外部 MCP 客户端能列出工具并调用只读工具 | 通过 | `pytest -m mcp -q`：`test_list_tools_reflects_the_registry_and_declares_schemas`、`test_read_only_tool_executes` |
| 写操作停在审批门并返回可读待审批结果 | 通过 | 同上：`test_write_stops_at_the_approval_gate_then_runs_after_approval`（停在门时账本 0 行） |
| `approve`（含 reject / edit 改参）后续跑 | 通过 | 同上 + `test_reject_closes_the_call_without_side_effect`、`test_edit_approval_runs_with_the_edited_args` |
| **管线复用裁决已落地** | 通过 | `harness/execution.py::ToolExecutor`；`Loop` 改为委托 |
| **两处调用点行为一致的可执行证明** | 通过 | `tests/test_execution_parity.py`：差分（事件序列逐字相等 + 外部账本相等 + 工具计数相等）／委托 spy／结构扫描 + **正对照**（合成坏源码必须被抓到） |
| 既有用例全绿 / ruff / 矩阵 / 门禁 / 内核依赖 | 通过 | 见下"每阶段共同门" |

### M2（`feat/mcp-crash-demo`）：崩溃与恢复演示

| 验收门 | 结果 | 证据 |
|---|---|---|
| 强杀 MCP 服务 → 重启 → 同一 run 续跑 → 外部账本 **1 次** | 通过 | `max_per_key=1`，且 `reconstructed_from_probe=true`（结论来自探针对账） |
| **对照组显示重复** | 通过 | 只把 `--outbox` 关掉：`max_per_key=2`（其余配置逐字相同） |
| 全程真 SIGKILL 且有崩溃位置证据 | 通过 | 两格 `exit_code=137`、`crash_marker.json`：`window=post_tool_effect_pre_record`、`occurrence=1`、`counts`、`pid` |
| 判分只认外部账本 | 通过 | `opsenv.oracle.read_effects`（连 `-wal`/`-shm` 快照）；用例另断言"日志里 tool_result 只有一条、重复只体现在账本上" |

### M3（`feat/integrations-boundaries`）：可观测 + 边界 + 文档 + claim

| 验收门 | 结果 | 证据 |
|---|---|---|
| 可观测路径文档含两条后端路径与三条口径 | 通过 | `integrations/observability.md` §0（三条口径）/§1（三条路径与前置条件、逐条标注本机实测状态） |
| `integrations/` 与 extra 边界就位并有依赖方向测试 | 通过 | `tests/test_integrations_boundaries.py`（10 例）；`test_pipelines…` 里的 `integrations/**` 扫描 |
| `verify` 时长增长 ≤ 5 秒 | 通过 | 见"边界自检" |
| 新增数字登记为 claim | 通过 | `check_facts.py --run verify` 38/38（M3 分支） |
| README 增补一小节（不得出现"平台/服务化"措辞） | 通过 | README「外围集成」小节 + 文档索引一行；措辞自检 0 命中 |

### 每阶段共同门（逐条实跑）

```
pytest -o addopts= -p no:cacheprovider -q            405 passed
pytest -m mcp -o addopts= -p no:cacheprovider -q       14 passed（缺 extra 时是 skip，不是 collect error）
ruff check .                                          All checks passed
python -m experiments.crash_matrix --repeats 5         16 格 / 80 次运行 / 70 次真 SIGKILL / 80 as-predicted / 0 违例
python -m opsenv.suite --per-fault 8 --repeats 3 --gate  14/14
scripts/check_facts.py --run verify                    38/38（M3；M1 33/33、M2 34/34，各自按分支内容）
scripts/mutation_check.py --update-baseline            1110 个变异体 / 912 killed / 180 survived（率 0.1648）
```

---

## 边界自检

* **内核依赖清单**：`pyproject.toml` 的 `[project].dependencies` 仍只有 `pydantic>=2.7`
  （有用例逐字断言）；MCP SDK 只进新增的 `[mcp]` extra，`dev` 与 `eval` 都不含它。
* **依赖方向**：`harness/` `opsenv/` `fakeworld/` `experiments/` `scripts/` `examples/`
  的任何 import（含函数体内）都不得指向 `integrations`（AST 全量扫描，覆盖 50+ 文件）；
  反向由子进程断言：`import harness.*` 之后 `sys.modules` 里没有 `mcp*`、也没有 `integrations`。
* **`verify` 时长变化**：CI 口径 `pytest -q` 由 **7.6 s → 9.4 s（+1.8 s）**；
  新增默认用例自身 2.2 s（含 pytest 启动）。预算 ≤5 s，未超，因此本包**没有**为时长做取舍。
* **文档措辞自检**：`grep -rn "平台\|服务化\|生产级\|高可用" integrations/ docs/integrations.md`
  → **0 命中**（`integrations/observability.md` 也干净）。
* **`docs/semantics.md` 零改动**：`git diff` 对 main 无该文件改动；裁决文档 §七 逐条核对了为什么不必改。
* **可观测不进任何验收门前置**：三条路径中只有"本机接收器"被实测；Docker/外网不可用
  不影响上面任何一条门。
* **不做 mock**：崩溃注入是真 `os.kill(getpid(), SIGKILL)`（服务进程内）；裁判是外部账本快照。

---

## 遗留与建议

1. **`mutmut` 的"列出数 ≠ 评估数"（需要所有者知道的一条）**：
   `mutmut print-time-estimates` 列出 **2023** 个变异体，而三次 `mutmut run` 都只评估
   **~1110** 个（1058 / 1114 / 1110，可复现）。这不是本次改动引入的：历史基线也只被核对过
   "与本文件同数"，从未与本工具自己的清单对账过。影响面：基线**仍然有效**（它只做"新增幸存
   变异"的回归判定，缺项会导致**误报**而不是漏报），但 `survivor_rate` **不能**被读成覆盖率。
   建议下一步二选一：在 nightly 加一条"评估数 vs `print-time-estimates`"的对账并把它变成红灯，
   或深挖 mutmut 3.8 的跳过逻辑（`mutants/mutmut-stats.json` 的增量状态）。
2. **可观测的路径②③未实测**（无 Docker、需凭据与外网）。文档逐条标注了"本机未实测"，
   没有把它们写成已验证。
3. **`mcp` SDK 实测版本 2.2.0**（pin `>=1.2,<3`）。2.x 的 handler 注册方式与 1.x 不同
   （低层 `Server` 没有 `@list_tools()` 装饰器，必须按方法名 + 参数模型注册，且参数模型是
   `PaginatedRequestParams` / `CallToolRequestParams` 而不是请求信封）——踩坑点写在
   `integrations/mcp_server.py` 里。**升级大版本前必须重跑 `pytest -m mcp`。**
4. **登记为边界、不是缺陷**：MCP 不下发 `read_artifact`（不引入 artifact root 语义）；
   租约仍未接入写路径（与 `semantics.md` §3 一致）；MCP 驱动的 run 不会自行进入 `completed`
   （没有 final `agent_message`，不伪造终态）；`spent_usd` 恒为 0 是事实（MCP 侧没有模型调用）。
5. **与设计文档的两处偏离**已登记在裁决文档 §六（治理工具不带 `run_dir`、不下发
   `read_artifact`），两处都是**有意收窄**，不放松任何承诺。
6. **建议（未做）**：若 nightly 时长变得敏感，可把 `[mcp]` 的安装与 `pytest -m mcp`
   从 `facts-nightly` 拆出去；本包按"不加新作业"的要求刻意没做。
