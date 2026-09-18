# 开放问题裁决：设计文档 C §5（外围集成：MCP 工具服务 + 可观测导出）

* 状态：已裁决（其中 §5.5 由所有者于 2026-09-18 拍板）
* 裁决依据：源码定位 + 两次冒烟实验的实测输出（本文逐条给出复现片段）
* 纪律：本裁决**不改变任何既有外部契约**——`docs/semantics.md` 一条不动，
  `harness/loop.py` 的公开方法签名与事件落盘顺序逐字不变。
* 前置阅读：`docs/design/2026-09-18-integrations.md` §0/§5、`docs/semantics.md` §2.5。

本文件是设计文档 C 落地前的**第一步**：五条开放问题先答完，再动代码。
每条给「结论 + 依据」；涉及实现取舍的还给「影响面」与「怎么证明」。

---

## 一、C §5.1｜如何在 MCP 形态下复用七步执行管线（最要紧）

**结论：走 (b)——把 `harness/loop.py::_execute_tool` 的七步抽成
`harness/execution.py::ToolExecutor`，loop 与 MCP 服务共用同一实现。
不选 (a)（给 loop 加"无模型模式"）。**

### 依据（源码定位）

设计文档 §0.3 的七步管线（`harness/loop.py` 模块文档字符串里的那份清单）在代码里是
**一条直线函数**，没有任何一步依赖模型侧状态：

| 步 | 代码位置 | 读什么 | 与模型的关系 |
|---|---|---|---|
| 1 日志权威（重放） | `harness/loop.py::_execute_tool` 首段 | `store.effective_events` 折叠 | 无 |
| 2 未注册工具 | 同上 | `ToolRegistry.get` | 无 |
| 3 幂等/outbox 对账 | 同上 + `_resolve_pending` | `ToolCallStore` | 无 |
| 4 审批门 | 同上 + `_find_approval` / `_raise_interrupt` | `ApprovalBinding` | 无 |
| 5 TOCTOU 复核 | 同上 | 实际参数 hash | 无 |
| 5b 参数级安全域 | 同上 | `Tool.arg_policy` | 无 |
| 6 outbox 预写意图 | 同上 | `Tool.effect` | 无 |
| 7 执行与闭合 | 同上 + `_handle_tool_failure` | `tool.fn` / 事件 | 无 |

模型侧状态（`self._llm`、`self._builder`、`self._compactor`、`self._budget`、视图与消息）
**在整条工具路径上一处都没出现**；`Loop._commit` 只写 checkpoint、不追加事件
（`harness/loop.py::_commit`），因此它也不属于管线。

### 冒烟证据（一次实验的输出）

复现片段（可整段粘贴执行；`Poison` 一旦被碰到就炸，用来证明"没碰"）：

```python
from pathlib import Path

from fakeworld.tools import build_registry
from fakeworld.world import World
from harness.chaos import Chaos
from harness.ids import new_id
from harness.loop import InterruptSignal, Loop, _Counters, _Ctx
from harness.state import reduce_events
from harness.store import SqliteStore
from harness.tools import ToolCallRequest

class Poison:
    """毒药：任何一次模型调用或 saver 调用都会炸。"""
    def __getattr__(self, name):
        raise AssertionError(f"管线碰了不该碰的对象：{name}")

work = Path("/tmp/oq"); work.mkdir(parents=True, exist_ok=True)
store = SqliteStore(work / "runtime.db"); store.setup()
world = World(work / "world.db")
registry = build_registry(world, idempotent_impl=False, probe_enabled=True)
loop = Loop(store, Poison(), llm=Poison(), registry=registry, chaos=Chaos.disabled())

run_id, thread_id, branch_id = new_id("run"), new_id("thr"), new_id("br")
store.create_run(run_id, thread_id=thread_id)
store.create_branch(branch_id, run_id)
ctx, counters = _Ctx(run_id, thread_id, branch_id), _Counters()

# ① 只读调用：零模型直接执行
loop._execute_tool(ctx, ToolCallRequest(
    tool_call_id="tc_read", tool="query_metrics", args={"service": "payment"}), registry, counters)
# ② 需审批的写调用：应停在审批门
try:
    loop._execute_tool(ctx, ToolCallRequest(
        tool_call_id="tc_write", tool="scale_pool",
        args={"service": "payment", "size": 64}), registry, counters)
except InterruptSignal:
    pass
# ③ 审批 + 恢复段（Loop.resume 里那一段，不含 _drive）
loop.approve(run_id=run_id, thread_id=thread_id, branch_id=branch_id, actor="human:smoke")
state, _ = reduce_events(store.effective_events(branch_id))
for call_id in sorted(state.open_tool_calls):
    loop._execute_tool(ctx, loop._tool_request_from_log(ctx, call_id), registry, counters)
print("status:", reduce_events(store.effective_events(branch_id))[0].status,
      "effects:", world.total_effects(),
      "counters:", counters.executed, counters.rejected, counters.unknown)
```

实测输出（本机 Python 3.14.6；把上面片段逐字执行所得）：

```
① + ② 之后
events: ['tool_call', 'tool_result', 'tool_call', 'interrupt']
counters executed/rejected/unknown: 1 0 0
effects: 0            # 写调用停在审批门，一行副作用都没有

③ 之后（llm 与 saver 同为毒药）
status: running       # 没有 final agent_message ⇒ 不进终态（见下方口径）
effects: 1            # 审批后由恢复段执行，恰好一次
counters executed: 2  # 只读一次 + 被审批的写一次
resumed: ['tc_write']  open_calls_after: []  violations: []
pipeline_events: [tool_result/executed, interrupt, approval, resume, tool_result/executed]
```

两条读数：**工具执行、审批决策、恢复段三件事都不需要模型，也不需要 checkpoint saver**。
这正是 MCP 形态要用到的全部——MCP 侧没有模型，也不该提交 checkpoint（那是 loop 的边界）。
反过来说，如果走 (a)，loop 会被迫承担第二种驱动模式，而它现在连"没有模型"都还表达不出来。

**顺带得到的一条口径**（写进 `docs/integrations.md`，不是缺陷）：MCP 驱动的 run
**不会自行进入终态**——`completed` 是事件日志折叠出来的，需要有人写下 final
`agent_message`；本包不伪造它。因此这类 run 的状态在 `running` / `waiting_human` 之间，
外部看板看到的就是事实本身。

### 抽取边界

```
harness/execution.py（新）
  RunContext            ← 原 loop._Ctx（run_id / thread_id / branch_id）
  InterruptRequest      ← 原样搬
  InterruptSignal       ← 原样搬（控制流靠异常，必须与 loop 共用同一个类）
  ToolOutcome           ← 新：每次执行的返回值（tool_call_id/status/result/error_class/replayed）
  CounterSink           ← 新：Protocol，7 个工具侧计数字段（鸭子类型，避免 execution→loop 反向 import）
  ToolExecutor
      execute(ctx, request, counters) -> ToolOutcome      ← 七步管线（原文搬迁）
      decide(ctx, *, decision, actor, ttl_seconds, approved_args, scope) -> ApprovalBinding
                                                          ← 原 Loop.approve 的主体
      resume_open_calls(ctx, counters) -> list[ToolOutcome]
                                                          ← 原 Loop.resume 的恢复段（按 tool_call_id 排序）
      pending_approvals(ctx) -> list[InterruptRequest]     ← 新：供"列待审批"工具
      + 私有：_find_approval / _resolve_pending / _handle_tool_failure / _raise_interrupt /
              _interrupt_request / _interrupt_count / _append_* / _state / _log / _has_result /
              _ensure_tool_call_event / _tool_request_from_log / _tool_result_from_log

harness/loop.py（保留）
  Loop 的模型侧：_drive / _call_model / _build_view / _dynamic_snapshot / _commit /
                 _checkpoint_cross_check（**没有一件进 execution.py**）
  _Ctx = RunContext 别名、_Counters（含 to_outcome，不进 execution.py——否则循环 import）
  _execute_tool / _find_approval：保留符号的薄委托（见下面"受影响面"）
  start / resume / approve：公开 API 原样
```

**为什么 registry 由 executor 持有、不再逐调用传入**：现在 `_drive` 与 `resume` 各传一次
`self._registry`，与 executor 自身持有的那份是同一个对象；两处来源意味着调用方可以传入
一个**不同的**注册表（管线里的幂等键、schema、ArgPolicy 都从它来），这是静默分叉面。
抽取时把它收敛成单一来源。

### 既有用例的受影响面（逐条，脚本化核对）

基数：用例数见 claim `tests-collected`（命令 `scripts/count_tests.py`）。

| 面 | 数量 | 处置 |
|---|---|---|
| 直接调用 `Loop._execute_tool` 的用例 | 1 处（`tests/test_arg_policy.py::test_rejected_call_does_not_write_a_side_effect`） | 调用实参去掉 `registry`（1 行） |
| `from harness.loop import _Ctx` 的用例 | 1 处（`tests/test_audit_regressions.py`） | **零改动**：`_Ctx` 保留为 `RunContext` 的别名 |
| `from harness.loop import _Counters` 的用例 | 1 处（同 `test_arg_policy.py`） | **零改动**：`_Counters` 留在 loop.py |
| 调用 `loop._find_approval(...)` 的回归用例 | 1 处（`tests/test_audit_regressions.py`） | **零改动**：保留薄委托（它守的是一个 P0，不动它） |
| 其余用例 | 全部 | **零改动** |
| `harness/__init__.py` 的公开面 | `__all__` 只增不改（新增 `RunContext`/`ToolExecutor`/`ToolOutcome`/`CounterSink`） | 无测试断言 `__all__` 内容 |

**容易漏的一条（变异门禁）**：`pyproject.toml::[tool.mutmut].only_mutate` 与
`scripts/mutation_check.py::DEFAULT_MODULES` 里写着 `harness/loop.py`；管线搬走后，
旧命名（`harness.loop.xǁLoopǁ_execute_tool__mutmut_*`）会全部失效、新模块零变异覆盖，
而门禁**不会变红**——这正是仓库明令禁止的"看起来还绿"。
因此抽取必须同时：把 `harness/execution.py` 加进两份模块清单、同步
`tests/test_mutation_gate.py` 的期望模块列表、重建 `reports/mutation_baseline.json`，
并在分支汇报里给出"抽取前 `_execute_tool` 幸存变异数 vs 抽取后 `harness/execution.py` 幸存变异数"。
同理 `[tool.mutmut].also_copy` 要加 `integrations`，否则变异工作目录里看不到集成代码。

### "两处调用点行为一致"的证明方式（可执行，不是眼过一遍）

1. **差分测试**（`tests/test_execution_parity.py`）：同一 `run_id`/`branch_id`、同一批
   `tool_call_id` 与参数，分别由 `Loop`（scripted 模型驱动）与 `ToolExecutor`（零模型直接驱动）
   在两个独立库里跑完整条链（执行 → 停审批 → 审批 → 恢复段）。
   只比较**工具路径事件子序列** `(kind, type, source, payload)`，把随机 id
   （`interrupt_id`/`approval_id`/`nonce`）与时间字段归一化后逐条断言相等；
   再断言两边外部账本的效果行一致。两边 `run_id`/`branch_id` 相同 ⇒ 幂等键、`args_sha256`
   等派生值必须逐字相等，逃不掉。
2. **委托证明**：把 `ToolExecutor.execute` 换成记录型包装（monkeypatch）后跑一次 `Loop`，
   断言包装确实被调用 —— loop 侧不存在第二条执行路径。
3. **结构证明 + 正对照**：源码级断言四个崩溃窗口字面量
   （`pre_tool_exec`/`post_tool_effect_pre_record`/`post_record_pre_commit`/`post_approval_pre_exec`）
   与 outbox 状态机调用只出现在 `harness/execution.py`；`integrations/**` 不得出现 `tool.fn(`。
   同时把一段**合成坏源码**喂给扫描函数，断言会被判违规——否则"扫描器永远绿"本身不可信。

### 为什么不选 (a)

* (a) 让 `Loop` 同时表达"模型驱动"与"外部驱动"两种模式：`_drive` / 恢复段 / 停止条件都要分叉，
  测试面翻倍，而且"外部驱动"这条路径会长期缺少来自真实使用的压力。
* (a) 把"没有模型"塞进 loop 的构造参数里，等于让一个**编译器无法检查**的布尔量决定语义分叉。
* (b) 的代价是把一个文件里的代码搬到另一个文件，且搬的是纯函数式的直线代码；
  代价集中在"变异基线要重建"这一件可核对的事上，比引入第二种驱动模式便宜得多。

---

## 二、C §5.2｜一次 MCP 会话与 run 的映射

**结论：run（严格说是 `(run_id, branch_id)`）是权威，会话只是句柄——客户端可断可续，
状态一律从事件日志折叠。服务进程不为会话保存任何状态。**

依据（源码定位）：

* 三身份在代码里是显式的：`harness/loop.py::_Ctx`（`run_id`/`thread_id`/`branch_id`），
  `experiments/worker.py` 把三者在 `--mode run` 时写进 `run_dir/ids.json`，
  `--mode approve` / `--mode resume` 是**另外的进程**，靠重读 `ids.json` 接续；
  崩溃恢复结论全部建立在这条链上（`docs/semantics.md` §2.5）。
* `thread_id` 只是会话持久身份：`harness/store/sqlite_store.py::latest_branch_for_thread`
  用它查"该 thread 最近的 branch"，日志与恢复都以 `branch_id` 为准（分叉语义见 §2.3）。
* 事件日志已经是 append-only 权威，折叠函数 `harness/state.py::reduce_events` 是纯函数；
  服务进程重启后重新折叠即可，**没有任何东西需要"记住"**。

落地口径：

* MCP 服务是**一进程一 run 目录**（`--run-dir`），进程生命周期与会话（stdio 连接）绑定；
  客户端断开、服务被强杀，都不产生新语义——下一次连接重新折叠日志。
* 服务**不设**会话数据库、不缓存审批结论、不在内存里放 run 的权威副本
  （设计文档 §1 非目标；违反即越界）。
* 会话级"上下文"只允许存在于客户端一侧；服务端唯一允许的内存状态是单次调用内的局部变量。

---

## 三、C §5.3｜预算与成本归属

**结论：按 run（与既有语义一致）；会话不做二次记账，MCP 层不新增任何成本事件。**

依据（源码定位）：

* `BudgetLedger` 的构造签名是 `(store, run_id=..., branch_id=...)`
  （`experiments/worker.py` 的两次构造），账本本身是 `budget_update` 事件的折叠物——
  归属天然是 run/branch，不是会话。
* `Loop` 里 `self._budget` 只在两处出现：`_call_model`（派发前检查、响应后复核、超限 fatal）
  与 `_dynamic_snapshot`（把余额塞进视图 tail）。**工具路径一处都不碰**
  （`harness/loop.py` 的 `_execute_tool` 及其全部子过程）。
* 由此推出 MCP 形态的准确口径：MCP 侧没有模型调用 ⇒ **不产生 `budget_update` 事件**
  ⇒ `spent_usd` 保持 0，这是事实而不是"记账缺失"。本包**不**发明"按会话分摊"或
  "工具成本"字段——那会是第二套记账，且没有权威。
* `docs/semantics.md` §2.6 列有 `tools` 分桶，但当前**没有生产者**；把工具成本接进账本
  是另一件事（要有真实计价口径），不在本包内，登记为边界。

---

## 四、C §5.4｜崩溃演示的判定口径

**结论：杀 MCP 服务进程自身（真 `SIGKILL`，由既有的命名窗口 `post_tool_effect_pre_record`
在**服务进程内**触发，`occurrence=1`）；判定用外部账本 `world.db`（连 `-wal`/`-shm` 快照）；
对照组取既有矩阵里已实测过的那两格——**单变量只差 outbox**。**

* **杀谁**：MCP 服务进程。窗口命中点在 `harness/loop.py` 的管线第 7 步
  （`post_tool_effect_pre_record`：效果已发生、任何记录未落盘），与 W2/W3 的结论窗口**同一个**。
  不新增窗口、不改 `harness/chaos.py`。
* **怎么证明**：三条证据缺一不可——① 子进程退出码 `-9`（真 SIGKILL）；
  ② `crash_marker.json` 含 `window` / `occurrence` / `pid` / `counts`（崩溃位置证明）；
  ③ 外部账本按幂等键计数（`opsenv.oracle.read_effects` 经
  `harness/store/snapshot.py::snapshot_db` 快照，**绝不裸读主库**）。
* **对照组配置**（两格，都 `--tool-idem off` 让下游非幂等、`--probe on`）：
  只差 `--outbox`。这是既有矩阵的主对照，实测输出如下（本机复现
  `tests/test_crash_matrix.py::test_primary_contrast_outbox_vs_baseline` 的两格）：

  | 格 | 外部账本 | `max_per_key` | 对账次数 | 判分 |
  |---|---|---|---|---|
  | `outbox=off, idem=off` | 重复 `1/1` | 2 | 0 | `as-predicted`（重复） |
  | `outbox=on, idem=off` | 重复 `0/1` | 1 | 1 | `as-predicted`（恰好一次） |

  两格都带崩溃证据：`exit_code=-9`、`window=post_tool_effect_pre_record`、`occurrence=1`。
* **为什么必须带对照组**：只报"1 次"无法区分"机制在起作用"与"什么都没发生"；
  W3 的教训（`docs/HANDOFF.md` §六 第 7 条）就是"没有坏事发生 ≠ 机制在工作"。
* **演示不得引入模拟**：真子进程 + `os.kill(SIGKILL)`（复用 `harness/chaos.py`），
  判分只认 `world.db`，runtime 自己的日志不作为"发生了几次"的证据。

---

## 五、C §5.5｜可观测后端的交付方式（**待所有者确认 → 已拍板**）

**结论（所有者 2026-09-18 拍板）：交付"仓库内 stdlib 本地接收器"作为**本机可实测**路径，
另给两条未实测路径与其前置条件；三条口径写死在文档顶部。**

候选与前置条件（本机实测状态如实标注，本机无 Docker、外网对部分域名不稳定）：

| 路径 | 前置条件 | 本机状态 | 能看什么 |
|---|---|---|---|
| 本地 stdlib 接收器（`integrations/otlp_local_sink.py`） | 无（标准库 + 本机回环） | **已实测** | span 名/父子/属性是否成形；`harness.*` 属性是否带全 |
| Jaeger（`docker run jaeger`） | Docker + 可拉镜像 | **未实测（本机无 Docker，`which docker` 退出 1）** | 时间轴视图、span 树形展开 |
| 任意 OTLP/HTTP 后端（如 Grafana Cloud） | 账号 + token + 外网 | **未实测** | 留存、查询、与他人共享 |

**任何一条都不是验收门的前置条件**（设计文档 §0.5）：可观测那一条坏了、后端不可达、
Docker 不存在，都不影响 M1/M2/M3 的任何一条验收。

**三条口径**（与 `harness/otel.py` / `harness/trace.py` 的既有实现一致，写进文档顶部）：

1. **时长是推导值**（`harness.duration_is_derived=true`）：工具 span 是真实墙钟窗口，
   模型 span 是事件间隔推导——**不得用来讲性能**；
2. **事后导入**，不是实时流（MCP 是请求-响应；本包不做"边跑边看"）；
3. **trace 里没有账本**：审批、outbox、"发生了几次"都在事件日志与 `world.db` 里，
   trace 只回答"这次调用的时序形状长什么样"。

---

## 六、与设计文档的两处已知偏离（已上报，非静默）

| # | 设计文档原文 | 本包实现 | 理由 |
|---|---|---|---|
| 1 | R-C2 的工具签名写作 `list_pending_approvals(run_dir)` / `approve(tool_call_id, ...)` | 两个治理工具**都不带 `run_dir` 参数**（run 由 `--run-dir` 在进程启动时固定） | 服务与 run 一对一（R-C1 已定），参数里再带 `run_dir` 等于让客户端能指向**别的** run；而 §1 非目标明确不做鉴权/多租户，那就成了无鉴权的越权面。参数省掉后语义更强：一个进程只能操作一个 run。 |
| 2 | R-C1 提到 `read_artifact`（PR #5 补的 schema） | `tools/list` **不下发** `read_artifact` | 它要求 `ArtifactStore` 与 artifact root 语义（卸载产物的目录归属），本包不引入该语义。工具注册表里未注册的工具本就不下发（`ToolRegistry` 映射即全部），因此这是"少一个工具"而不是"绕过管线"。登记为边界，M3 的文档里写明。 |

两条都属于"实现比设计文档更收窄"，不放松任何承诺，因此按 §8 边界纪律**登记而不是停下**。

---

## 七、本包对 `docs/semantics.md` 的影响

**零改动。** 逐条核对：

* §2.5 的七步顺序与落盘顺序：搬迁是原文搬运，顺序一字未动；新增的 `ToolOutcome`
  是返回值，不改变任何一次落盘。
* §2.4.1 事件哈希链、§4.0 参数级安全域、§4.1 审批四约束：均在 `execution.py` 内原样保留。
* §3 的租约"写路径未接入"：本包**不改变**这一点——MCP 服务与 loop 一样，
  今天双进程写同一 run 仍只由外部账本裁决。
* 唯一需要新写的承诺面是"集成层不产生结论、不新增权威状态"，它属于
  `docs/integrations.md` 的边界声明，不进 `semantics.md`。

若实现阶段发现需要改 `semantics.md`，按设计文档 §8 停下并上报。
