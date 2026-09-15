# agent-consistency-lab

面向**崩溃一致性与治理语义**的 Agent runtime 实验台。

不是一个"Agent 应用"，也不是 LangGraph 的替代品。它是一个**被测对象 + 测量仪器**：
用精简自研内核把"崩溃恢复、审批绑定、幂等边界、上下文压缩"这些语义做出来，
用命名的崩溃注入点把它们测出来，并与 LangGraph 的同任务等价图做对照。

运维故障处置（IT 运维场景）只是验证壳，可替换。

## 状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| W1 | 事件日志 + state 折叠 + checkpoint/writes 提交协议 + 语义文档 v1 | 完成 |
| W2 | 最小 loop + 命名崩溃注入器 + 崩溃窗口矩阵（窗口 1-3） | 完成 |
| W3 | interrupt/resume + 审批绑定 + 幂等 outbox + unknown 对账 | 未开始 |
| W4 | 上下文视图 + 压缩 + 卸载 + 缓存纪律 | 未开始 |
| W5 | 运维壳 + 场景集 + 三基线（含 LangGraph 对照） | 未开始 |
| W6 | 评测台 + 人工校准 + CI 门禁 | 未开始 |
| W7 | 缓存净收益 × 压缩冲突实验 | 未开始 |
| W8 | 长文 ×3 + 文档 + demo | 未开始 |

## 已产出的实测结论

W2 崩溃矩阵（60 次 SIGKILL + 60 次恢复，8.4 秒，见
[docs/w2-crash-windows.md](docs/w2-crash-windows.md)）：

* "效果已发生、记录未落盘"是唯一产生重复副作用的窗口：下游不幂等时 5/5 复现，
  下游幂等时 0/5；
* **runtime 侧去重开关在该窗口完全无效**（12 格对比无一改变）——崩溃点在记录之前，
  去重表里没有可查的行；
* 三个窗口下"日志不重不漏 + 不变量零违反"均为 5/5，孤儿 writes 不造成危害。

## 核心设计

先读 [`docs/semantics.md`](docs/semantics.md)：它是 runtime 的承诺清单，
代码与测试都以它为口径。三条最关键的设计约束：

1. **事件日志是唯一权威**：扁平 append-only（`(branch_id, seq)`），历史改写由
   SQLite 触发器物理禁止；状态永远是日志的折叠物。
2. **TREE_NODE 与 ARTIFACT 分离**：只有对话节点进上下文视图，预算/租约/审批/压缩
   等控制事实一律为 ARTIFACT，禁止混入树。
3. **checkpoint 与 writes 分离**：task 完成即写 writes，checkpoint 只在 super-step
   边界提交；恢复 = 最新 checkpoint + 重放 writes（不重跑副作用）+ 未完成 task 重跑。

## 布局

```
harness/
  events.py              事件模型（扁平日志、kind 分离、type 约束）
  state.py               事件 → 派生状态折叠 + 不变量（INV-001..006）
  tools.py               效果声明、幂等键、参数规范化
  chaos.py               命名崩溃窗口 + 确定性 SIGKILL
  loop.py                最小 agent loop（super-step + 边界 checkpoint + resume）
  store/
    schema.py            DDL、schema 版本与迁移、append-only 触发器
    sqlite_store.py      单写者事务、seq 分配、分叉解析
    checkpoints.py       checkpoint/writes 提交协议、恢复计划
    tool_calls.py        工具调用记录（运行时去重表）
fakeworld/               受控仿真：副作用账本 + 仿真工具 + 脚本化模型
experiments/
  worker.py              单次 run / resume 的子进程入口
  crash_matrix.py        崩溃窗口矩阵 runner（跑真实 kill -9 子进程）
tests/                   不变量、协议、loop 与矩阵小样本
docs/semantics.md        运行时语义（承诺清单）
docs/w2-crash-windows.md W2 崩溃矩阵实验报告
reports/                 矩阵原始数据与自动生成的报告
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                        # 全部测试（含矩阵小样本）

# 完整崩溃矩阵：3 窗口 × 去重 × 下游幂等 × 5 次
.venv/bin/python -m experiments.crash_matrix --repeats 5 \
    --json-out reports/w2_crash_matrix.json --md-out reports/w2_crash_matrix.md
```

单次崩溃可手工复现：

```bash
CHAOS_WINDOWS="post_tool_effect_pre_record:2" .venv/bin/python -m experiments.worker \
    --run-dir /tmp/demo --mode run --tool-idem off     # 进程被 SIGKILL（退出码 137）
.venv/bin/python -m experiments.worker --run-dir /tmp/demo --mode resume --tool-idem off
```

## 边界声明

* 崩溃实验只对 **kill -9** 语义成立；掉电（`synchronous=NORMAL` 下可能丢最后一个
  事务）不在本轮范围内，见语义文档 §3。
* 统计口径：W2 每格 n=5，0/5 只把失败率上界压到约 60%（rule of three），
  **不能**据此声称"永不重复"；W6 会提到 30+ 次并报置信区间。
* 存储层是单写者模型，跨进程并发需要上层租约（W3）。
* 本仓库不出现"生产级"字样：它是一个实验台，所有指标以"相对基线的 paired 差值 +
  置信区间"报告，不以绝对值宣称达标。
