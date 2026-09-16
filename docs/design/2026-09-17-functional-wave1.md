# 设计文档 B：功能性扩展第一波——真实模型接入与治理能力加深

* 版本：v1.2（2026-09-17；v1.2 = 吸收对抗审查第二轮 25 条发现：cassette schema 补
  tool_calls 与 id 再生、canonical usage 归一、一致性比较白名单、迁移 drop/重建
  触发器、fork 链挂接规则、arg_policy 执行点、sweep 引用枚举规则、lease 首版
  不做接管、pyproject live marker 注册、测试数门槛走 claim 机制）
* 目标读者：开发 Agent（自包含；先读 §0 与 §8 再动代码）
* 状态：待评审
* 关联背景：外部测试报告确认仓内机制承诺全部成立，但整个评测的地基是
  **确定性脚本模型**（usage 固定 `512+64*i`、取证决策完美、无噪声）——所有 W5 结论
  作用于"理想化推理器 + 固定 token 计数"的世界。本包抬高这块地基，并补齐三处
  已在接口上露头的治理能力缺口（审批粒度、完整性证明、租约占位）。

---

## 0. 第一性原理推导

1. **runtime 的承诺不依赖模型**。`docs/semantics.md` 的全部承诺（不重不漏、审批绑定、
   崩溃恢复、预算硬停）都以 harness 接口为界，模型是黑盒。因此接真实模型**不能触碰
   承诺层代码**，只新增满足 `harness/model.py` 协议的实现 + 测量口径迁移。
   这决定了 R-B1 的架构形态是录音-回放，而不是"重构适配层"。
2. **可复现性是资产，不可双输**。真实模型的最大风险是把"一条命令再生全部结论"
   变成"看 API 脸色"。推论：录制/回放是头等公民；在线调用是显式 opt-in 的例外路径；
   其成本与随机性必须被现有账本口径完整捕获。
3. **治理功能的本质问题是**"已有的承诺，如何让第三方低成本地验证"——
   审批绑定已有 `(tool, args_sha256)` + scope，参数级规则是同一承诺加细一维；
   append-only 触发器已经是拆改的物理墙，哈希链是同一承诺的**可离线证明**升级；
   `lease` 是已登记未实现、语义文档已起草的最后一块占位。
4. **不做服务化**。重试/降级、多租户、鉴权、对外可靠性用语出现即越界（§8）。

---

## 1. 目标 / 非目标

### 目标

* G1：同一 runtime 代码可运行于 `scripted`（现状，默认）、`record`（在线）、
  `replay`（回放）三种模型模式；语义层与评测层无感知；录制物成为新的事实数据源。
* G2：审批策略支持参数级规则（按字段值/区间声明允许域），并在 TOCTOU 改参路径
  与 session 复用路径上被强制执行。
* G3：事件日志具备静态哈希链，任一历史副本的删改（即使绕过触发器）可被
  离线验证器定位。
* G4：`harness.trace --otlp-endpoint` 显式启用真实 OTLP 导出（`[otel]` extra），
  默认行为与今天完全一致。
* G5：artifact 存储补上孤儿回收 sweep（显式 CLI，dry-run 默认）。
* G6：`lease` 事件类型得到最小实现并与语义文档同步。

### 非目标

* 不做模型选型 benchmark、prompt 评测、agent 行为优化——真实模型只是换被测对象，
  不改测量方法学。
* 不做模型 API 的重试/降级/路由/缓存（网关功能）；一次调用失败即 run failed（现语义）。
* 不做多租户、多用户、鉴权、外部化存储。
* 不替换 `ScriptedModel` 的默认地位；全部既有结论保持原口径可再生。

---

## 2. 需求列表

### R-B1｜真实模型：录制-回放接入（对应 G1）

* **模式**：
  * `scripted`（默认）：现状不动。
  * `record`：显式 `--model record --record-dir DIR` + `--model-key-env VAR`
    触发；官方 SDK 调真实 API，把每次调用写入录制目录；事件 payload 照常记录
    usage 与 `cost_usd`（真实 usage 过价格表）。
  * `replay`：从录制目录按确定性键检索；键 = 第几次 `LLMClient.complete` 调用
    （**跨 run/approve/resume 的总计数，语法定义见开放问题 1**）；并作 prompt
    归一化哈希的**警告级**校验（miss 时报"缺哪份录制、期望 prompt 是什么"）。
* **cassette schema（对抗审查第 12 条）**：录制条目不是 `messages → (text, usage)`
  ——loop 靠 `tool_calls` 驱动，缺了它接不了工具循环。每条录制必须可序列化三段：
  `text`、`tool_calls`（含 `tool_call_id / tool / args`）、`usage`。
  **`tool_call_id` 的再生规则**同理关键：录制时落原始 id，回放同一 run 目录时
  原样回放；跨 run 重放时 id 重生成规则必须确定（建议沿用 Loop 现有 id 生成器，
  PR 里说明）。
* **usage 归一（对抗审查第 13 条）**：录制时同时落**原始 usage json** 与
  **canonical usage**（`prompt/completion/cache_read/cache_write` 四段，各 SDK 字段
  映射规则写死并带 PR 样例）；价格表消费 canonical 段 + `price_version`。
  **replay 的 usage = 录制的 canonical 值直接回放，不做二次重算**（对抗审查第
  17 条：`ScriptedLLMClient` 的 PrefixCacheModel 是对视图块的估算口径，live 的
  服务端缓存账单无法复算——两者口径谁权威必须写死：录制为权威，replay 不重算）。
* **一致性验收的比较白名单（对抗审查第 16 条）**：`created_at`、`run_id`、
  wall-clock 类字段天然不同。scripted/replay 一致性断言白名单 =
  `token / cost / 事件序列 / tool_calls 结构`；时间戳类字段不比较。
* **崩溃兼容性是本需求的验收核心**：`record`/`replay` 两模式都必须接受现有
  `CHAOS_WINDOWS` 注入（以及设计文档 A 的 `--kill-after-ms`，若已交付），
  outbox/探针/审批绑定/预算硬停语义**一个字不改**。
* **成本与预算**：record 模式必须实测触发一次预算硬停（`--budget-usd 0.001` 级别的
  小额 run）；禁止无 `--budget-usd` 的 live 启动。
* **密钥纪律**：从环境变量读 key；**绝不**把 key 写进仓库文件、报告或事件 payload。
* **录制物策略**：默认写 `/tmp`；正式样本入库 `reports/replays/<id>/` 并带 provenance
  （模型名、日期、温度、seed、成本），**永远不会**被评测命令默认拉用。
* **验收**：
  * scripted 与 replay 在同一份录制上产出**白名单字段一致**（token、cost、
    事件序列、tool_calls 结构；时间戳类不比对，见上）。
  * replay miss 的报错可读（指明缺哪份录制）。
  * 无 key 环境下 live 模式启动即给出可读失败并退出（CI 里用 `-m live` 反例测试；
    **对抗审查第 15 条**：`pyproject.toml` 须先注册 `markers = ["live: ..."]`，
    否则空选集下 `pytest -m live` 以 exit 5（no tests ran）退出，会把反例误判）。
  * CI 永不执行任何在线调用。
* **语义文档**：`docs/semantics.md` 增补"模型属于测量外部，承诺以 harness 接口为界"。

### R-B2｜参数级审批策略（对应 G2）

* **形态**：工具注册元数据在 `requires_approval` 之外新增 `arg_policy`：
  `{"field": "role", "allowed": ["api", "worker"], "forbidden": ["billing"]}`
  与数值区间变体。M2 只承诺一层字段；嵌套为扩展点，文档写明。
* **执行点（对抗审查第 20 条）**：现有校验链在"approval 时"比对
  `args_sha256`——参数一变即拒，所以 session 复用路径已天然被 hash 拒掉。
  arg_policy 的**增量校验点在执行前一刻**（执行 handler 前的最后一道门），
  输入是**实际 request.args**（而非已 hash 的审批记录）；否则会出现"审批时看不懂
  结构化参数、批准了被禁止值"的缝隙。PR 用例必须模拟"审批通过时 args 未含
  forbidden 值的认知、但实际执行含"这一档。
* **哲学定位**（语义文档同步改写）：arg_policy 依然是"工具自述风险"哲学——
  工具在注册时声明自己的安全域，gate 执行；它不是一张外置黑名单。
* **验收**：`tests/test_arg_policy.py` ≥ 8 条：allowed 通过、forbidden 拒绝、
  执行前一刻校验（审批记录里没有 forbidden 值但实际参数含）、session 复用 hash
  改变被拒、数值区间边界值、策略缺失回退布尔语义、非法策略定义显式报错；
  `docs/semantics.md` 审批节同步修订。

### R-B3｜事件哈希链（对应 G3）

* **形态**：`events` 表新增 `event_hash` / `prev_hash`；
  `event_hash = sha256(prev_hash ‖ record_bytes)`，genesis 为常量；
  record_bytes = `json.dumps(record, sort_keys=True, ensure_ascii=False)`
  （唯一正则化定义，写进 semantics）。
* **迁移（对抗审查第 14 条）**：schema 版本 +1；存量库一次性补链。**关键坑**：
  `events` 的 append-only 触发器对任何 UPDATE 都 RAISE ABORT——迁移必须在事务内
  先 `DROP TRIGGER` 补链、再原样重建触发器（`events_no_update`/`events_no_delete`），
  且迁移自测必须断言"迁移后触发器存在且再 UPDATE 仍被拒"。这不是绕过边界纪律
  （payload 与排序未被触碰），但**必须显式写进迁移实现**，否则实现者会硬卡住
  或误以为违反 §8 边界纪律。
* **fork 语义下的链挂接规则（对抗审查第 18 条）**：子分支链的第一个事件的
  `prev_hash` 指向**分叉后最后一条祖先事件**的 `event_hash`（即
  `effective_events` 祖先前缀截断点的尾部），链校验按同一规则沿 branch-id 展开；
  规则写进 semantics，`audit_chain` 与 INV-008 都以此为准，否则 mirror test
  无法写出。
* **新增 INV-008**：链不可续/不对齐 = 外部篡改指纹；`harness/state.py`
  供增量校验（只查新 seq），离线 CLI `harness/audit_chain.py` 供全量校验。
* **审计读取纪律**：validator 必须连 `-wal`/`-shm` 一起快照（外部审计实测踩过的坑，
  见 `docs/tester-prompt.md`）。
* **不改**：append-only 触发器保留（纵深防御）；`(branch_id, seq)` 排序、
  分叉语义不变；链按 branch 内 seq 顺序挂接。
* **验收**：mirror test（复制副本 → 篡改一行历史 → `audit_chain` 非零退出并指出
  第一个断点）；genesis/回填/正常/断链四类单元测试。

### R-B4｜OTLP 导出（对应 G4）

* 新增 extra `[otel]`（`opentelemetry-sdk` + exporter）；
  `harness.trace --run-dir X --otlp-endpoint URL` 显式启用。
* 单元测试用内存内 mock exporter 验证映射正确性，**不引入**任何外部 collector
  进程；`to_otel` 的映射复用现有实现的形状。
* **验收**：未装 extra 时 `import harness.trace` 与全部现有测试行为不变
  （不因缺依赖产生 ImportError）；装了 extra 的测试环境下，
  span 树的关键字段对齐 `to_otel()` 的 today 输出。

### R-B5｜artifact GC sweep（对应 G5）

* `harness/artifacts.py` 增加 `sweep(dry_run=True)`：按引用图删除无引用 artifact，
  返回回收前后 `orphan_count` 与删除清单。
* **引用枚举的权威规则（对抗审查第 21 条）**：sweep 的引用收集 = **全量扫描
  事件库 payload 中的 artifact 引用**（`compaction` 的 replaces、`read_artifact`
  结果引用等；`orphan_count(referenced:...)` 已有接口 `harness/artifacts.py:77`
  可复用），**不限于当前 run 目录**——同一 artifact 库被多个 run 引用时必须
  全库一次扫描后再判孤儿，**禁止只扫单 run**（否则会误删被其它 run 引用的 artifact）。
* 同时**禁止**回收"仅被 checkpoint/checkpoint_writes payload 引用"的对象——
  枚举规则必须把 checkpoint 类事件也纳入扫描（PR 附引用来源清单）。
* 分类错误处理：被删文件再次 `read_artifact` 时给出**可读失败**（分类错误语义）。
* **只能**由显式 CLI 调用；runtime loop 内**绝不自动删**——避免引入新的崩溃窗口。
* **验收**：≥4 条测试（引用保留、孤儿回收、dry-run 无副作用、删后重读报错）。

### R-B6｜lease 事件类型最小实现（对应 G6）

* 范围严格限于语义文档已声明的方向：**单写者租约**——本进程持锁续租、过期失活、
  未持有租约的写入路径按文档口径给出**可读失败**。**不做**分布式协调，
  `state_update` 事件类型保持"已登记未实现"并在文档里明确本包不承诺。
* 措辞纪律：文档不得写"从此支持并发"；lease 只是**入场条件**
  （持有效租约才允许写），真正的一致性配合仍由外部账本裁决。
* **交互时序**：lease 持有校验发生在 checkpoint 事务**外**
  （先证权、后写），PR 需带一张时序图说明两层的边界。
* **验收（对抗审查第 22 条收敛）**：首版只验两条——持有效租约写入正常、
  未持有/已过期租约的写入给出**可读失败（拒绝接管也是合法实现）**；
  "过期接管"是第二步能力，本包不做，端到端多进程接管列开放问题。
  `tests/test_lease.py` 附加双进程 resume 冒烟：复用外部审计"未定义行为要如实
  记录"的纪律——本包把它变成"已定义行为需如实验证"。

---

## 3. 接口与布局

```
harness/
  llm.py               扩展：三种模式 + 录制物 schema
  approval.py          扩展：arg_policy 校验进入绑定链
  tools.py             扩展：arg_policy 声明
  artifacts.py         扩展：sweep(dry_run=True)
  lease.py             新增：单写者租约
  audit_chain.py       新增：链校验 CLI（db + -wal）
  trace.py             扩展：OTLP 桥
  store/schema.py      扩展：version+1 迁移 + events 两列 + 回填
docs/semantics.md      扩展：模型边界 / 参数级审批 / 哈希链 / lease 四段
pyproject.toml         扩展：[otel] extra + pytest live marker 注册
tests/
  test_arg_policy.py     新增
  test_hash_chain.py     新增
  test_lease.py          新增
  test_artifact_sweep.py 新增
  test_replay_model.py   新增（fake transport / in-process exporter）
```

---

## 4. 风险与对抗式审查记录（reviewer 攻击与结论）

| # | 攻击 | 结论 |
|---|---|---|
| 1 | cassette 会把"录制质量"变成新的自证——录谁的手笔谁 owns 结论 | 录制物必须带 provenance；回放模式只用于接口回归，任何对外结论必须注明数据生成于 live。**成立，已吸收**。 |
| 2 | 预算硬停在 live 模式下是真实烧钱 | 无 `--budget-usd` 拒绝启动 live；验收只允许 ≤10 次调用的小额 smoke。**成立，已吸收**。 |
| 3 | hash 链回填"改写了历史"，表面违反 append-only | 回填只写新哈希列、不改 payload，一次性迁移并有自测；文档写明迁移路径。**成立，已吸收**。 |
| 4 | arg_policy 的 forbidden 列表像黑名单，与"工具自述风险"哲学冲突 | 定性为同一推进维（工具继续自述自己的安全域），语义文档同步改写这个否决点。**成立，已吸收**。 |
| 5 | lease 之后会被读成"支持并发" | 措辞纪律写死（§2 R-B6）；无任何并发承诺。**成立，已吸收**。 |
| 6 | OTLP 接入暗示部署/运维面 | endpoint 每次显式传入；未传时行为与今天一致；不启动守护。**成立，已吸收**。 |
| 7 | G1–G6 一起做太大 | §5 给出可独立收口的里程碑；R-B1 允许跨 M2/M3。**成立，已吸收**。 |
| 8 | （本轮新增预判）回放键若只用调用序号，在分支/恢复形态下可能错位 | 已列为开放问题 1：首版序号键 + 归一化 hash 警告校验给出 sentinel，升级为复合键前必须有 PR 论证。 |

**第二轮对抗审查（v1.1 → v1.2）已吸收的关键条目**：cassette 必须含 `tool_calls`
与 `tool_call_id` 再生规则、usage 归一为 canonical 四段且 replay 不重算、
scripted/replay 一致性改为白名单字段比对、events 迁移须事务内 drop/重建 append-only
触发器并建自测、fork 下 `prev_hash` 指向祖先前缀尾部事件、arg_policy 校验点移到
执行前一刻、sweep 引用枚举 = 全库事件 payload 扫描且纳入 checkpoint 引用、
lease 首版不做过期接管、pyproject 注册 live marker（防 exit 5 误判）、
测试数门槛改走 claim 计数机制、§7 共享面修正为两处。

## 5. 里程碑

| 阶段 | 内容 | 验收门 |
|---|---|---|
| M1 | R-B1（三种模式 + 成本捕获 + 崩溃兼容，live 永不进 CI） | scripted/replay 双路一致 + `CHAOS_WINDOWS` 语义复跑不变 |
| M2 | R-B2 + R-B3 | arg_policy ≥8 例 + hash 链 mirror test |
| M3 | R-B4 + R-B5 + R-B6 + 语义文档同步 | 全部新增测试全绿；测试数门槛引用 A-R1 的演进类 claim（`pytest --collect-only` 计数，禁手写绝对数——对抗审查第 23 条）；`opsenv.suite --gate` 14 条不变；`ruff` 全净 |
| 每阶段 | **本包交付的任何对外结论数字必须登记为 claim**（入 `reports/documented-facts.json`，依赖 A 的机制；对抗审查第 25 条） | — |

## 6. 开放问题（开发代理先答再动手）

1. **回放命中键**：调用序号在 fork/恢复形态下是否唯一？PR 里必须给出论证或升级为
   `(序号, 视图 fingerprint 前缀)` 复合键。
2. **payload 正则化**：`event_hash` 的输入必须字节级稳定（sort_keys 定义）；
   需在 semantics 写进 inv 定义。
3. **arg_policy 的支持深度**：M2 只承诺一层字段；嵌套、类型转换等列为扩展点。
4. **lease 与 checkpoint 提交协议的时序**：实现前先画时序图（进程持有 vs 事务边界）。
5. **live 测试的 CI 摆放**：`pytest -m live` 标记 + 无 key 下的可读失败断言。

## 7. 与文档 A 的关系

* 独立交接、独立收口；共享面有二（对抗审查第 24 条修正）：其一，R-B1 的崩溃兼容
  依赖 A 的 `--kill-after-ms`——**A 开放问题 1（新增 flag 还是扩展
  `CHAOS_WINDOWS` 语法）的裁决结果必须回写本文档**，两包不能各自假设不一致的
  注入面；其二，B 的验收数字依赖 A 的 claim 再生机制（见 §5）。
* 建议顺序：A 的 M1（数字再生门禁）先行——B 每一步都会产出新的"结论数字"，
  先立起再生机制，B 的验收数字天然被覆盖。

**A §4.1 裁决回写（2026-09-17，裁决全文见
[2026-09-17-open-questions-answered.md](2026-09-17-open-questions-answered.md) §一）**：

> **新增独立 flag `experiments.worker --kill-after-ms <ms>`；`CHAOS_WINDOWS` 的语法与语义一字不改。**
> 理由：`CHAOS_WINDOWS` 是 HANDOFF §一 记录的外部契约（`harness/chaos.py::parse_spec` 的
> `name[:occurrence]` 语法 + 封闭窗口集合），把墙钟毫秒塞进同一字符串必须改解析器；
> 且"第 N 次命中窗口"与"墙钟定时"是两个不同的注入维度。
> 新注入族在 `crash_marker.json` 里以 `injection_kind: "window" | "time_hit"` 区分
> （`time_hit` 额外记 `kill_after_ms`；`window` 的既有字段只增不改）。

因此 B-M1 的"崩溃兼容"验收按**两条独立路径**做，两包对注入面的假设一致：

1. `CHAOS_WINDOWS`（既有命名窗口，按现有语义复跑不变）；
2. `--kill-after-ms`（新路径，A-M3 交付后可用；交付前该条验收以第 1 条为准并在 PR 说明）。

## 8. 边界纪律（触碰即停、上报）

* 出现"生产级 / 高可用 / 服务化"口径或对外可靠性承诺；
* 需要真实 API key 的测试混入默认 `pytest`（live 必须显式打 `-m live`）；
* `docs/semantics.md` 承诺未经同步修订；
* 预算 / 幂等 / 审计三层的语义被"顺手"放宽；
* 录制文件或 key 被写进任何仓库路径。
