# 外围集成：MCP 工具服务（怎么用、边界在哪）

> **这是外围集成，不改变任何结论的适用范围。** 本仓库仍是**实验台**：
> 集成层只改变两件事——**谁能调用这些工具**、**别人用什么工具看这些数据**。
> 它不产生结论，也不提升任何结论的强度。接上 MCP 客户端不等于"服务化"，
> 更不等于"生产可用"；本仓库不承诺任何可用性、并发或安全属性。

相关文档：[`docs/semantics.md`](semantics.md)（承诺清单，本包**一条不改**）、
[`docs/design/2026-09-18-integrations.md`](design/2026-09-18-integrations.md)（设计）、
[`docs/design/2026-09-18-open-questions-answered.md`](design/2026-09-18-open-questions-answered.md)
（裁决：为什么把七步管线抽成 `harness/execution.py::ToolExecutor`）、
[`integrations/observability.md`](../integrations/observability.md)（可观测导出落地）。

---

## 1. 这是什么 / 不是什么

**是**：一个**本地、单用户、stdio** 的进程，把工具注册表暴露成 MCP 工具。
一个进程对应一个 run 目录。外部 agent 每次调用都走
`harness/execution.py::ToolExecutor` 的**七步管线**——审批门、含 branch 维度的幂等键、
outbox 三态、参数级安全域（ArgPolicy）、TOCTOU 复核、append-only 事件日志全部生效。

**不是**（设计文档 C §1 非目标）：不做多租户、不做鉴权、不监听非本地地址、
不是守护进程、没有实时流、不重写工具执行、不新增权威状态。

## 2. 装与跑

```bash
pip install -e ".[mcp]"            # 只有这一条路径需要 SDK；内核依赖仍然只有 pydantic

# 只列出会下发的工具与 schema（**不需要** [mcp] extra，claim 用的就是它）
.venv/bin/python -m integrations.mcp_server --run-dir /tmp/run1 --list-tools

# 跑服务：stdio，一个进程一个 run 目录
.venv/bin/python -m integrations.mcp_server --run-dir /tmp/run1 \
    --outbox on --dedup on --tool-idem on --probe on
```

* `--run-dir` 不存在则由本服务创建（run + 分支 + 一条**写明来源**的 `user_message`，
  `source=system`——它不假装有人类说话）。目录里已有 `ids.json` 时直接接续，
  格式与 `experiments/worker.py` 相同，两边可以互相接续同一个 run。
* 缺 `[mcp]` extra 时 `import integrations.mcp_server` 照常工作，只有真正要起服务时
  才给可读失败（SDK 的 import 全在函数体内，照 `harness/otel.py`）。

客户端配置示例（Claude Desktop / 任意 MCP 客户端）：

```json
{ "mcpServers": { "agent-consistency-lab": {
  "command": "/abs/path/.venv/bin/python",
  "args": ["-m", "integrations.mcp_server", "--run-dir", "/tmp/run1"] } } }
```

## 3. 下发了哪些工具

`tools/list` 由 `ToolRegistry` **直接映射**：`Tool.parameters`（JSON Schema）就是
`inputSchema`；未声明 schema 的工具回退到空参数表，而这个回退形状与下发给真实模型的
那一份**同源**（`harness/tools.py::tool_param_schema`，只有一处定义）。

在此之上另加两个**治理工具**（它们不是被治理的对象，而是审批门的显式接口）：

| 工具 | 作用 |
|---|---|
| `list_pending_approvals` | 列出当前 run 里等待人工审批的调用（谁卡在审批门、谁在后面排队、参数与参数哈希） |
| `approve` | 对某次调用作出决定：`approve` / `reject`；带 `approved_args` 表示**改参批准**。批准后立即续跑并把结果返回 |

工具数与清单是可再生的：`mcp_server --list-tools` 的输出里带 `tool_count`。

## 4. 一次写调用长什么样

默认形态**不依赖 elicitation**（客户端支持程度不一）：写操作把"待审批"当作**普通的工具结果**
返回，客户端据此调用 `approve`。

```
→ tools/call  scale_pool {"service": "payment", "size": 64}
← { "status": "pending_approval", "tool_call_id": "tc_…", "args_sha256": "…",
    "interrupt_id": "int_…", "interrupt_index": 0, "approval_ttl_seconds": 3600,
    "next": "调用 approve，参数 {'tool_call_id': 'tc_…', 'decision': 'approve'|'reject'}…" }

→ tools/call  approve {"tool_call_id": "tc_…", "decision": "approve"}
← { "status": "approved", "decision": "approved", "edited": false,
    "executed": [ { "status": "executed", "result": {...}, "replayed": false } ],
    "run_status": "running" }
```

三条要留意的语义（都是既有承诺在 MCP 形态下的样子，不是新规则）：

* **同一时刻至多一条 interrupt**（INV-004）。第二次写调用会**排队**而不是被丢弃：
  回执里同时给出"interrupt 属于谁"（`tool_call_id`）和"你这次调用叫什么"
  （`queued_tool_call_id`），排到它时再审批。
* **改参批准 = 新调用**：原调用闭合为 `superseded`，另起 `…__edit1` 的新 `tool_call_id`
  （因此是新幂等键），批准绑定到新调用与新参数。
* **批准绑定"某次调用 + 某组参数"**：`approve` 的 `tool_call_id` 与当前待审批调用不一致时
  直接拒绝，不会把批准给到别处。

## 5. 崩溃与恢复

服务进程**启动时自动做一次恢复**（`resume_open_calls`，**不驱动模型**）：把日志里
未闭合的调用重新驱动一次。是否需要探针对账、是否允许重跑，全部由管线按日志与
outbox 意图行决定——重启不会产生新语义，也不会盲目重跑。
"被强杀 → 重启 → 副作用恰好一次 / 对照组产生重复"的实测见
[`integrations/observability.md`](../integrations/observability.md) 同级的崩溃演示
（`python -m integrations.mcp_crash_demo`）。

**run 不会自行进入终态**：`completed` 是事件日志折叠出来的结果，需要有人写下 final
`agent_message`。MCP 驱动的 run 没有模型，因此它的状态在 `running` / `waiting_human`
之间——这是事实本身，不是缺陷。

## 6. 边界与口径（读之前请先读这一节）

1. **集成层不产生结论。** 这里没有新的语义、没有新的权威状态：服务端不缓存审批结论、
   不在内存里放 run 的权威副本、不设会话数据库。状态一律从事件日志折叠，
   客户端可断可续，服务被强杀也不产生新语义。
2. **服务端不做第二套参数校验。** 执行前唯一的闸门是管线里的 ArgPolicy 与工具自身；
   在下发 schema 与执行之间再插一个校验器，必然与管线分叉（同一个参数在两处得到不同判决）。
   schema 是**下发给客户端的自述**，不是第二道闸。
3. **不实现 elicitation。** 审批的默认形态就是唯一形态，因此"客户端不支持时自动退回"
   是恒等路径——没有可退的东西。有测试断言服务端从不向客户端发 elicitation 请求。
4. **不下发 `read_artifact`。** 它要求 artifact root 语义（卸载产物的目录归属），
   本包不引入该语义。未注册的工具本就不下发。
5. **一个进程一个 run。** 治理工具不带 `run_dir` 参数是有意的：带上等于让客户端指向别的 run，
   而本包不做鉴权——那就成了越权面。
6. **租约没有被接进写路径。** 与 `docs/semantics.md` §3 一致：今天双进程写同一 run
   仍不被任何检查拦住，后果由外部账本裁决。MCP 服务不改变这一点。
7. **`spent_usd` 恒为 0 是事实，不是记账缺失。** MCP 侧没有模型调用 ⇒ 不产生
   `budget_update` 事件。本包不发明"按会话分摊"或"工具成本"字段。
8. **不承诺**并发、多用户、远程访问、断线重连的时序，也不承诺任何性能数字。

## 7. 测试分层

| 层 | 位置 | 需要什么 |
|---|---|---|
| 治理语义（映射 / 审批 / 拒绝 / 改参 / 失败分类 / 恢复） | `tests/test_mcp_mapping.py` | 无（进程内，不装 extra 也跑） |
| 边界（依赖方向 / 延迟 import / 依赖清单 / 打包与门禁配置） | `tests/test_integrations_boundaries.py` | 无 |
| 一致性证明（两条驱动路径产出逐字相同的管线事件） | `tests/test_execution_parity.py` | 无 |
| 外部验收（官方 SDK 的 stdio 客户端驱动真实服务进程） | `tests/test_mcp_server.py` | `[mcp]` extra（`pytest -m mcp`，nightly 跑） |

`mcp` marker 默认被 `addopts` 过滤；用例内部用 `importorskip`，所以缺 extra 时是
skip 而不是 collect error（用例计数在装与不装 extra 的环境里一致）。
