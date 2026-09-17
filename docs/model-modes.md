# 模型接入的三种模式：scripted / record / replay（R-B1）

* 代码：`harness/llm.py`（三种客户端）、`harness/cassette.py`（录制物 schema）、
  `harness/live_transport.py`（真实 HTTP transport）、`experiments/worker.py`（CLI）
* 对账脚本：`scripts/replay_consistency.py`（scripted vs replay 的白名单比对）
* 语义：`docs/semantics.md` §2.7「模型属于测量外部」
* 状态：首版；live 路径**不是**默认路径，CI 永不执行在线调用

---

## 1. 三种模式

| 模式 | 用途 | 命令 |
|---|---|---|
| `scripted`（默认） | 现状不变：确定性脚本模型，既有全部结论的口径 | `python -m experiments.worker --run-dir /tmp/d ...` |
| `record` | 调真实 API 并录制（也可用脚本 transport 做无网络的端到端验证） | `... --model record --transport live --record-dir /tmp/cassette --budget-usd 0.05` |
| `replay` | 在同一份录制上重跑（接口回归、崩溃兼容验证） | `... --model replay --record-dir /tmp/cassette` |

三条模式走的是**同一条 loop**：审批、outbox、事件日志、checkpoint、压缩、预算一行不改。
换掉的只是"模型那一侧"，因此"崩溃语义在真实模型下是否成立"这个问题的答案不依赖于模型。

## 2. 录制物（cassette）的 schema

```json
{
  "meta": {"schema_version": "cassette/v1", "model": "...", "provider": "live|scripted-transport",
           "temperature": 0.0, "seed": null, "created_at": 0.0, "price_version": "price-v1"},
  "entries": [
    {"step": 0, "attempt": 0,
     "text": "...",
     "tool_calls": [{"tool_call_id": "tc1", "tool": "scale_pool", "args": {"size": 64}}],
     "usage":        {"prompt_tokens": 200, "completion_tokens": 50,
                      "cache_read_tokens": 900, "cache_write_tokens": 100},
     "usage_raw":    {"input_tokens": 200, "output_tokens": 50,
                      "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100},
     "request_fingerprint": "sha256(归一化提示词)",
     "cost_usd": 0.000123, "recorded_at": 0.0}
  ]
}
```

三条不能省的字段：

* **`tool_calls`**：loop 靠它前进；只录 `messages → (text, usage)` 的实现接不了工具循环。
* **`usage` 四段（canonical）**：价格表只消费它；`usage_raw` 同时保留，
  便于日后核对映射是否漏字段。
* **`request_fingerprint`**：提示词的归一化哈希，用于**警告级**校验（见 §4）。

**provider 口径差异**（会差一个缓存量级，必须显式化）：
OpenAI 形态的 `prompt_tokens` **已包含**缓存命中；Anthropic 形态的 `input_tokens`
**不含**缓存段。`harness/cassette.py::prompt_includes_cache` 是唯一的判据，
`total_input_tokens` 是唯一的换算点——别处再算一遍就会出现"同一份账单两个总输入"。

## 3. 命中键：`(step, attempt)`

* `step` 由事件日志驱动（= 已落盘的 `agent_message` 数），**崩溃-恢复后不漂移**：
  被崩溃打断的那次调用没写 `agent_message`，恢复后重发拿到同一个 `(step, attempt)`，
  于是 replay 能命中它（论证与实测见
  [开放问题裁决 §五](design/2026-09-17-open-questions-answered.md)）；
* `attempt` 是**同一 step 在本进程内**的第几次调用——上下文溢出重试会产生 `attempt=1`，
  少了这一维，重试会把两次不同的请求压成一条录制。

## 4. 失败语义：miss 是错误，指纹不一致是警告

| 情况 | 行为 | 为什么 |
|---|---|---|
| 未命中 | **可读错误**：说清缺哪份录制、`(step, attempt)`、期望的请求指纹 | 静默返回"随便什么"会让回放结论不可信 |
| 请求指纹与录制不一致 | **警告**（记进 `replay_warnings`，仍返回录制值） | 崩溃恢复会**合法地**改变视图（探针重建的结果与首次执行不同）。把这条判失败，等于宣布"崩溃轨迹永远不可回放" |
| 价格表版本与录制不同 | **警告**（`price_version_mismatch`） | 成本会差一个版本；提示但不失败，口径写在 `semantics.md` §2.7 |

**警告的出口**（独立测试 P2-6 后补）：`replay_warnings` 会出现在
①`outcome_<mode>.json` 的 `replay_warnings` 字段（机器可读）、
②worker 的 stdout 摘要（人可读）、③stderr 的逐条打印。
**不判失败**是刻意的（理由见上表），"有出口"与"判失败"是两件事。
| 录制物缺失/损坏 | 可读错误（`CassetteError`） | 不许静默降级成 scripted |

## 5. 一致性验收（scripted vs replay）

```bash
.venv/bin/python scripts/replay_consistency.py --json-out /tmp/consistency.json
```

流程：`record`（脚本 transport，无网络）→ `scripted` → `replay`，然后按**白名单**比对：

| 比较 | 不比较 |
|---|---|
| `outcome` 的 status/step/各计数/cost/token 段 | `created_at` / `recorded_at` / `at`（时间戳） |
| 事件序列（kind:type 逐位） | run/thread/branch/event/tool_call 之外的 id 生成 |
| `tool_calls` 结构（id/tool/args） | `view_fingerprint`（恢复路径合法改变视图） |
| **模型文本**（`agent_message` / `user_message` 的 `text`） | —（独立测试 P2-7 后补：等 token 的文本篡改也必须被抓到） |
| 每个 `agent_message` 的 context/cache/cost | `avg_wall_ms` 等计时噪声 |

**为什么是白名单**：全量比对必然产出永远红的测试（三次运行的 id 与时间戳天然不同），
而"白名单 + 明确列出不比较什么"是可审阅的口径。

实测：`reports/replay_consistency.json`（入库）——`identical: true`，录制 3 条，
含 tool_calls 与 canonical 四段。

## 6. live 路径的纪律

1. **key 只从环境变量读**（`--model-key-env`，默认 `OPENAI_API_KEY`）；绝不写进仓库文件、
   报告或事件 payload；缺失时给可读失败（说清设哪个变量）。
2. **无 `--budget-usd` 拒绝启动 live 录制**：真实调用会花钱；预算硬停由 `BudgetLedger`
   负责，并有单测证明它真的会拦住（`--budget-usd 0.0005` 的 run 以 `failed` 结束、
   事件日志留下 `budget_exceeded(fatal=True)`）。
3. **CI 永不执行在线调用**：`pyproject.toml` 注册 `live` marker，默认 `addopts` 过滤掉它；
   CI 用 `pytest -m live` 跑其中的**反例**（没有 key 时必须失败）。
4. **录制物默认写 `/tmp`**；要入库的样本必须带 provenance（模型名、日期、温度、seed、成本），
   并且**永远不会**被评测命令默认拉用。

## 7. 边界（本结论不适用的范围）

1. **live 的能力边界**：本 transport 把视图块拼成一条 user 消息，并以 `tools` 参数下发
   工具名与描述；本仓库**没有**为真实模型定义工具的参数 schema，因此"真实模型自主选择工具"
   只在与供应商都支持时才跑通。接真实模型的第一版用途是**接口回归与录制**，不是质量评测。
2. **不做模型质量结论**：本仓库的任何数字都不评价模型好不好；`replay` 的结论只对
   "同一条录制"成立。
3. **replay 不能证明语义**：它证明的是"runtime 在给定模型输入输出下行为一致"，
   不证明真实模型下的性能、成本或事故率。
4. **录制质量归录制者**：cassette 带 provenance，任何对外结论必须注明数据生成于 live
   还是脚本 transport。
5. **`--kill-after-ms` 落在模型调用内部时**，该次调用可能没有留下录制条目，
   于是这条轨迹不可回放（表现为可读的 miss）。这是刻意保留的：宁可显式失败，
   不要"猜一个返回值"。
