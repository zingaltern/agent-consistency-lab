# agent-consistency-lab

面向**崩溃一致性与治理语义**的 Agent runtime 实验台。

不是一个"Agent 应用"，也不是 LangGraph 的替代品。它是一个**被测对象 + 测量仪器**：
用精简自研内核把"崩溃恢复、审批绑定、幂等边界、上下文压缩"这些语义做出来，
用命名的崩溃注入点把它们测出来，并与 LangGraph 的同任务等价图做对照。

运维故障处置（IT 运维场景）只是验证壳，可替换。

## 状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| W1 | 事件日志 + state 折叠 + checkpoint/writes 提交协议 + 语义文档 v1 | 进行中 |
| W2 | 崩溃注入器（命名窗口）+ 恢复矩阵 | 未开始 |
| W3 | interrupt/resume + 审批绑定 + 幂等 outbox | 未开始 |
| W4 | 上下文视图 + 压缩 + 卸载 + 缓存纪律 | 未开始 |
| W5 | 运维壳 + 场景集 + 三基线（含 LangGraph 对照） | 未开始 |
| W6 | 评测台 + 人工校准 + CI 门禁 | 未开始 |
| W7 | 缓存净收益 × 压缩冲突实验 | 未开始 |
| W8 | 长文 ×3 + 文档 + demo | 未开始 |

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
  store/
    schema.py            DDL、schema 版本、append-only 触发器
    sqlite_store.py      单写者事务、seq 分配、分叉解析
    checkpoints.py       checkpoint/writes 提交协议、恢复计划
tests/                   不变量与协议测试
docs/semantics.md        运行时语义 v1（承诺清单）
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## 边界声明

* 崩溃实验只对 **kill -9** 语义成立；掉电（`synchronous=NORMAL` 下可能丢最后一个
  事务）不在本轮范围内，见语义文档 §3。
* 存储层是单写者模型，跨进程并发需要上层租约（W3）。
* 本仓库不出现"生产级"字样：它是一个实验台，所有指标以"相对基线的 paired 差值 +
  置信区间"报告，不以绝对值宣称达标。
