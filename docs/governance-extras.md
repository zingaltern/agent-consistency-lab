# 三处治理能力补齐：OTLP 导出 / artifact GC / 单写者租约（R-B4 / R-B5 / R-B6）

* 代码：`harness/otel.py`、`harness/artifacts.py::sweep`、`harness/lease.py`
* 语义：`docs/semantics.md` §2.6（GC）、§2.7（OTLP）、§3（租约）
* 状态：首版；每一项都写明"不承诺什么"，因为这三处最容易滑向"服务化"口径

---

## 1. OTLP 导出（R-B4）

```bash
pip install -e ".[otel]"                       # 只有显式要导出时才装
python -m harness.trace --run-dir /tmp/demo --otlp-endpoint http://localhost:4318
# 没有 extra 也可以发（OTLP/JSON over HTTP，协议允许 JSON 编码）
python -m harness.trace --run-dir /tmp/demo --otlp-endpoint http://localhost:4318 --otlp-json
```

| 设计点 | 做法 | 为什么 |
|---|---|---|
| 默认行为零改变 | OTel 的 import **全部在函数体内**；不传 `--otlp-endpoint` 时 CLI 与既有行为逐字节相同 | 缺 extra 时 `import harness.trace` 必须照常工作 |
| 映射只有一个定义 | 复用 `Trace.to_otel()` 的 JSON 形状，`harness/otel.py` 只做翻译（trace/span/parent id、kind、纳秒起止、属性） | 两份定义并存迟早漂移；单测断言"名字序列与起止时间逐位一致" |
| 不做服务化 | endpoint 每次显式传入；**不启动守护、不做重试队列、不做批处理调优** | 导出失败就是可读错误；观测链路不进承诺层 |

**不承诺**：不做采样策略、不做后端可用性、不做 span 完整性（投影只覆盖 loop 里的关键动作）。

## 2. artifact 孤儿回收（R-B5）

```bash
python -m harness.artifacts --run-dir /tmp/demo            # dry-run（默认）
python -m harness.artifacts --run-dir /tmp/demo --apply    # 真的删
```

三条边界（每条都对应一个会丢数据的错法）：

1. **只能由显式 CLI 调用**：runtime loop 内绝不自动删——删除与"读到半个文件"之间没有事务可用，
   自动回收等于凭空造一个崩溃窗口。
2. **引用枚举必须全量**：扫描 `events.payload_json` + `checkpoints.state_json` +
   `checkpoint_writes.payload_json` 里出现过的每一个 `digest`。
   只扫事件表会把"仅被 checkpoint 引用"的对象误判成孤儿；只扫当前 run 目录会误删被别的 run 引用的对象。
3. **派生缓存可以删**：卸载（offload）产生的 artifact 是**渲染层的缓存**——结果是先内联写进
   `tool_result` 事件的，渲染时才落成 artifact。没被任何 payload 提到的可以安全回收：
   下一次渲染用同样的内容算出**同样的 digest** 并重新落盘（内容寻址 ⇒ 幂等）。
   这条有专门的用例钉住（`test_deleted_offload_artifact_is_regenerated_on_next_render`）。

**分类错误语义**：被回收后再 `read_artifact` ⇒ 可读失败（`FileNotFoundError` 带 digest），
不是空串、不是旧内容。

## 3. 单写者租约（R-B6）

```python
lease = SingleWriterLease(store, run_id=..., branch_id=..., owner="worker-a")
lease.acquire(ttl_seconds=60)     # 追加一条 lease 事件
lease.require()                   # 写入前的入场检查；不满足 ⇒ LeaseNotHeld（可读失败）
lease.renew(ttl_seconds=60)       # 续租（要求当前确实持有且未过期）
```

时序：**先证权、后写**——校验在 checkpoint 事务**之外**做（进程持有 vs 事务边界）。
把两者缠在一起会让"写租约事件本身也要持租约"变成自指。

| 已做 | 未做（写进语义文档的未决项） |
|---|---|
| 持有 / 续租 / 过期失活 | **过期接管**（本包只允许显式 `acquire`） |
| 未持有 / 被他人持有 / 已过期 ⇒ 可读失败 | 分布式协调、租约仲裁、时钟漂移处理 |
| 双进程 resume 冒烟（如实记录行为） | 并发安全的任何承诺 |

措辞纪律：**不写"从此支持并发"**。租约是**入场条件**，"两个进程同时写会不会坏"仍由外部账本裁决；
一次双进程冒烟不是并发证据（`tests/test_lease.py::test_two_process_smoke_records_the_actual_behaviour`
的 docstring 里写着这句话）。

## 4. 边界（本结论不适用的范围）

1. **OTLP**：不评价后端可用性与采样；导出失败不影响 runtime 语义（它是投影，不是权威）。
2. **GC**：只处理 artifact 文件；不动事件日志、不动 checkpoint；`--apply` 是不可逆操作，
   仓库里没有任何自动调用点。
3. **租约**：只覆盖"单进程持有 vs 未持有"这一档；跨进程并发、跨机器、时钟漂移均不在范围内。
4. 三者都**不是**"生产级"能力：它们是实验台里"让第三方能低成本验证既有承诺"的补件。
