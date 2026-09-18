# 可观测导出：怎么起后端、怎么导入、看什么／不要看什么

> **这是外围集成，不改变任何结论的适用范围。** 接上 Grafana 或 Jaeger **不等于**本项目
> 具备可观测性，更不等于"生产可用"。trace 回答的只是"这次调用的时序形状长什么样"，
> 它**不是**本项目任何一条结论的证据来源——结论来自 `docs/semantics.md` 的语义与
> `opsenv/` 的测量方法学。

---

## 0. 先读三条口径（其余都可以不看，这三条不行）

1. **时长是推导值，不得用来讲性能。** 准确说法分两半：
   * **模型 span**（`chat …`）的起止**没有独立事件**，是"上一个事件之后到本条
     agent_message"的事件间隔推导出来的，投影会给它打上
     `harness.duration_is_derived=true`（`harness/trace.py::build_trace`）。
   * **工具 span / 审批 span** 的起止来自两个**真实事件**（`tool_call`→`tool_result`、
     `interrupt`→`resume`），是真实墙钟窗口——但它们**仍然不能用来讲性能**：
     单次运行、没有对照、窗口里包含框架自身的开销。本仓库的性能类结论一律以
     `opsenv/` 的配对统计为准。
2. **事后导入，不是实时流。** 路径是"跑完 → 从事件日志投影 → POST 一次"。
   没有"边跑边看"，也不会因为后端不可达而影响任何一次运行。
3. **trace 里没有账本。** 审批结论、outbox 三态、"副作用发生了几次"都在**事件日志**与
   外部账本 **`world.db`** 里。trace 里连一个"发生过几次"的字段都不该有——
   `otlp_local_sink` 会显式检查这一点（`has_ledger_attributes` 必须为 `false`）。

一句话：**trace 用来看形状，不用来下结论。**

## 1. 三条路径与前置条件（本机实测状态如实标注）

| 路径 | 前置条件 | 本机状态 | 能看什么 |
|---|---|---|---|
| **① 本地接收器**（`integrations/otlp_local_sink.py`） | 无（标准库 + 本机回环） | **已实测**（本文件所有数字都出自它） | span 名/父子/属性是否成形；三条口径是否真的成立 |
| ② Jaeger（`docker run jaeger`） | Docker + 能拉镜像 | **未实测：本机没有 `docker`（`which docker` 退出 1）** | 时间轴视图、span 树展开、按 trace 检索 |
| ③ 任意 OTLP/HTTP 后端（如 Grafana Cloud） | 账号 + token + 外网 | **未实测：需要凭据与外网** | 留存、查询、与他人共享 |

**没有任何一条是验收门的前置条件。** 后端不可达、Docker 不存在、外网不稳，
都不影响任何里程碑的验收（设计文档 C §0.5）。

## 2. 路径 ①：本机一条命令跑通（已实测）

```bash
.venv/bin/python -m integrations.otlp_local_sink --selftest --work-root /tmp/otel-demo
```

它做四件事：在 `/tmp/otel-demo` 跑一个最小的**模型驱动** run（脚本模型 + fakeworld）→
从事件日志投影出 span 树 → POST 到本机 `127.0.0.1` 上的接收器（`POST /v1/traces`）→
打印"收到了什么、能看出什么"。**不需要 `[otel]` extra、不联网、不需要 Docker**
（走的是 `harness/otel.py::post_otlp_json` 的 OTLP/JSON over HTTP 降级路径）。

实测输出（本机 Python 3.14.6）：

```
span_count=7  tool_span=2  model_span=3  derived=3  derived_flag_matches_model_spans=true
span_names: invoke_agent run_…, chat scripted-model, execute_tool query_metrics,
            chat scripted-model, execute_tool scale_pool, human_approval, chat scripted-model
request_paths: ['/v1/traces']       # 恰好一次 POST —— 事后导入，不是流
has_ledger_attributes: False        # 账本不在 trace 里（显式检查过）
```

已有一个 run 目录时直接用：

```bash
.venv/bin/python -m integrations.otlp_local_sink --run-dir /tmp/run1
```

**注意一个会让人困惑的现象**：MCP 驱动的 run 导出后 `model_span=0`、
`derived_duration_span_count=0`。这不是缺陷——**MCP 服务进程里没有模型**，
所以没有模型 span，也就没有"推导时长"。工具 span 仍然在。

## 3. 路径 ②：Jaeger（**本机未实测**）

```bash
docker run --rm -d --name jaeger -p 16686:16686 -p 4318:4318 \
    jaegertracing/all-in-one:latest                     # OTLP/HTTP 默认在 4318

# 装了 [otel] extra 走 SDK 导出（注意 endpoint 只写到端口，/v1/traces 由 exporter 补）
pip install -e ".[otel]"
.venv/bin/python -m harness.trace --run-dir /tmp/run1 --otlp-endpoint http://localhost:4318

# 没装 extra 走 OTLP/JSON 降级路径
.venv/bin/python -m harness.trace --run-dir /tmp/run1 --otlp-json --otlp-endpoint http://localhost:4318
```

然后打开 <http://localhost:16686> 按服务 `agent-consistency-lab` 检索。
**未实测的原因如实登记**：本机没有 Docker，所以这条路径在这里没有被执行过一次。

## 4. 路径 ③：任意 OTLP/HTTP 后端（**本机未实测**）

```bash
pip install -e ".[otel]"
OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer <token>" \
  .venv/bin/python -m harness.trace --run-dir /tmp/run1 \
      --otlp-endpoint https://<your-otlp-endpoint>
```

前置条件：一个 OTLP/HTTP 端点 + 凭据 + 外网。**本机未实测**（需要账号与凭据，
且本机网络对部分域名不稳定）。`--otlp-endpoint` 每次显式传入：本仓库**不启动守护、
不做重试队列**，导出失败就是一条可读错误（`harness/otel.py` 模块纪律 3）。

## 5. 看什么 / 不要看什么

**可以看的**：

* 一次调用链的**形状**：根 span → 工具 span / 模型 span / 审批 span 的父子关系；
* 工具 span 上的 `harness.idempotency_key`、`harness.args_sha256`、`harness.result_status`
  ——它们说明"这次调用带着哪个幂等键、结出了什么结论"；
* 审批 span 上的 `harness.approval.decision` / `harness.approval.actor`
  ——"谁批的、批没批"；
* **不存在**没归类的时长：`harness.duration_is_derived` 恰好标在模型 span 上，不多不少。

**不要看的**：

* **不要用 span 时长讲性能**（第 0 节第 1 条）；
* **不要在 trace 里找"副作用发生了几次"**——那是 `world.db` 的职责，
  trace 里根本没有这个字段（有测试断言它不存在，见下）；
* **不要以为接了后端就有了"实时监控"**——这是事后导入；
* **不要把 trace 当证据链**：本仓库所有一致性结论都以事件日志 + 外部账本为准。

## 6. 这一层怎么被守住

| 用例 | 守什么 |
|---|---|
| `tests/test_observability_sink.py`（默认 pytest，不需要任何 extra） | 恰好一次 POST 到 `/v1/traces`；`derived` 标记恰好落在模型 span 上；trace 里**没有**账本属性；非 JSON 请求体被拒（接收器的正对照） |
| `tests/test_otel.py`（既有） | 顶层不引入新依赖；缺 extra 时可读失败；`--otlp-endpoint` 分支有覆盖 |
| `scripts/check_facts.py` 的 claim | 上表数字可被 `--selftest` 一条命令再生 |
