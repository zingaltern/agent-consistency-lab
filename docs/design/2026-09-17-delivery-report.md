# 交付汇报：设计文档 A + B

* 日期：2026-09-17 ｜ 交付对象：设计文档 A（测试强化）与 B（功能扩展第一波）
* 分支模型：六个里程碑各自一个分支（堆叠），全部只推分支、由所有者在网页端 PR 合并
* 口径：本文件里的每个数字都能由命令再生；会漂移的数字（用例数等）一律引用 claim id
* **这是一份时点快照**（W9 交付提交 `49f9f51`）：其中的"用例数"等演进类数字此后
  可能已变，以 `reports/documented-facts.json` 的 claim 为准；后续修复见
  [`2026-09-17-review-response.md`](2026-09-17-review-response.md)

---

## 1. 目标达成（A-G1..G5 / B-G1..G6 逐条）

### A 包（测试强化）

| 目标 | 状态 | 证据 |
|---|---|---|
| G1 结论数字自动再算 + 成为 CI 门禁 | 达成 | `reports/documented-facts.json`（72 条 claim）；`scripts/check_facts.py`；CI job `facts`；`pytest tests/test_facts_gate.py`（15 条，含三类错误注入 self-test） |
| G2 命名窗口之外的随机时刻 SIGKILL 可判定 | 达成 | `experiments/chaos_fuzz.py` + `opsenv/oracle.py`；`reports/chaos_fuzz_report.{md,json}`（180 次注入、0 新类违例、敏感性自检通过） |
| G3 注入谱系扩到 ≥3 档 | 达成（4 档） | `scripts/probe_{sigterm,tornwrite,extra_ledger_row}.py`；结论与边界见 `docs/fault-spectrum.md` |
| G4 可调噪声人格使口径敏感性被激活 | 达成 | `--reasoner noisy`：strict 66.7% < cause_only 77.6%；`docs/noisy-reasoner.md`；`tests/test_noisy_reasoner.py`（15 条） |
| G5 变异测试常设化 | 达成 | `[tool.mutmut]` + `scripts/mutation_check.py` + nightly job `mutation`；基线 `reports/mutation_baseline.json`（1670 变异体 / 625 幸存 / 率 0.3774） |

### B 包（功能扩展）

| 目标 | 状态 | 证据 |
|---|---|---|
| G1 三模式模型接入 | 达成 | `scripts/replay_consistency.py` → `identical: true`；`docs/model-modes.md`；`tests/test_replay_model.py`（17 条） |
| G2 参数级审批策略 | 达成 | `harness/tools.py::ArgPolicy` + `loop._execute_tool` 的执行前闸门；`tests/test_arg_policy.py`（12 条，≥8 的验收） |
| G3 事件哈希链 + 离线验证器 | 达成 | schema v3 + `harness/state.py::verify_chain`（INV-008）+ `harness/audit_chain.py`；mirror test 定位到 seq=4 |
| G4 OTLP 显式启用、默认不变 | 达成 | `[otel]` extra + `harness/otel.py`；`tests/test_otel.py`（5 条，含"顶层不含 otel import"） |
| G5 artifact GC sweep（dry-run 默认） | 达成 | `harness/artifacts.py::sweep` + CLI；`tests/test_artifact_sweep.py`（5 条） |
| G6 lease 最小实现 + 语义同步 | 达成 | `harness/lease.py` + `tests/test_lease.py`（9 条，含双进程冒烟）；`docs/semantics.md` §3 已改写 |

## 2. 验收门结果

### A §6（里程碑验收）

| 阶段 | 验收 | 结果 |
|---|---|---|
| A-M1 | ≥20 claim；3 条错误注入 self-test 全红 | 72 条；`tests/test_facts_gate.py` 15 passed；退化注入实测（改 value → 退出 1） |
| A-M2 | 噪声人格可用 + mutation nightly 生效 | `--reasoner noisy` 全量可跑（1 条降级门禁如实报红）；mutation 基线入库，删一条基线即退出 1 |
| A-M3 | fuzz 30× 全绿 + ≥3 档探测器 + nightly | 180 次注入 0 新类违例；4 档探测器；nightly job `chaos-fuzz` |
| 每阶段 | ruff 全净；测试全绿；`opsenv.suite --gate` 14 条不变；不回退 | `collected 316`（默认集 312 passed + 1 skipped，`live` 主题 3 条由 `pytest -m live` 单跑——独立测试 P2-11 指出"passed"用词偏了）；`gate_summary {total:14, passed:14}`；`crash_matrix --repeats 5` 16 格 as-predicted |

### B §5（里程碑验收）

| 阶段 | 验收 | 结果 |
|---|---|---|
| B-M1 | scripted/replay 双路一致 + `CHAOS_WINDOWS` 语义复跑不变 + live 永不进 CI | `identical: true`；`tests/test_replay_model.py::test_replay_mode_accepts_chaos_injection`；`live` marker 默认过滤 + CI 只跑反例 |
| B-M2 | arg_policy ≥8 例（含 TOCTOU 改参与 session 复用）+ 哈希链 mirror test | 12 例（含"审批只看 hash、参数越界"）；mirror test 指出第一个断点 |
| B-M3 | OTLP 默认行为不变；GC dry-run 默认；lease 最小实现 + 语义同步 | `tests/test_otel.py::test_cli_without_endpoint_is_unchanged`；`sweep(dry_run=True)`；`docs/semantics.md` §2.6/§2.7/§3/§7 |
| 每阶段 | 新增测试全绿；测试数走 claim；14 条门禁不变；ruff 全净 | 见上 |

### claim 对账（整量）

```
$ .venv/bin/python scripts/check_facts.py --run verify    → 27/27 通过
$ .venv/bin/python scripts/check_facts.py --run nightly   → 45/45 通过
```

## 3. 测试与产物

* **用例数**：claim `tests-collected`（`scripts/count_tests.py` 再生）。
  本轮新增测试文件：`test_facts_gate.py`(15) / `test_noisy_reasoner.py`(15) /
  `test_mutation_gate.py`(5) / `test_chaos_fuzz.py`(11) / `test_replay_model.py`(17) /
  `test_live_model.py`(3, live marker) / `test_arg_policy.py`(12) / `test_hash_chain.py`(11) /
  `test_artifact_sweep.py`(5) / `test_lease.py`(9) / `test_otel.py`(5)。
* **新增/再生的 reports 产物**：`documented-facts.json`、`mutation_baseline.json`、
  `chaos_fuzz_report.{md,json}`、`noisy_reasoner.{md,json}`、`replay_consistency.json`；
  `w4_crash_matrix.*`、`w4_context_cost.*`、`w6_eval.*`、`w7_sweep.*` 全部重跑再生
  （差异只允许计时噪声：数值逐格未变，新增的是物化字段）。
* **CI 作业变化**：verify 新增 `facts`（轻 claim 集，约 5 秒）与 `pytest -m live`（反例）；
  新增 `nightly.yml`：`facts-nightly`（整量 claim）、`mutation`（变异门禁）、
  `chaos-fuzz`（随机注入 + 谱系探测器）。

## 4. 实现中发现的与设计文档不符之处（按纪律上报）

1. **`--kill-after-ms` 的注入窗口必须先标定**（设计文档未写）：worker 启动 + import 就要
   几十毫秒，不标定的话采样会全落在"进程早已退出"的区域，跑出来的"全绿"只是**没被杀到**。
   实现里加了线扫描标定（本机 run 180ms / resume 35ms），并把当次窗口写进报告。
   见 `experiments/chaos_fuzz.py::calibrate_kill_window`。
2. **窗口 2 的缝隙是微秒级**（设计文档默认随机注入能命中它）：随机墙钟注入几乎不可能落在
   "效果已提交、`tool_result` 未写"的那一段。因此该窗口的存在性由**确定性窗口注入**
   （敏感性自检）证明，随机注入主张的是相反方向（"别处没有新类违例"）。
3. **渲染层卸载产生的 artifact 是派生缓存**（设计文档把它当作"被引用的对象"）：
   引用只出现在**视图**里，不在事件 payload 里。已按"引用枚举全量扫描 + 内容寻址可重建"
   实现，并用一条用例钉住"删掉后下次渲染会重建同一 digest"。
4. **`mutmut 3.x` 的 `--all` 是带值选项**（`--all=true`）、`executescript` 会隐式 COMMIT
   （会破坏补链事务的原子性）——两处都写进了代码注释与失败信息，避免后来者重踩。
5. **README 的 W7 段混用了两行阈值**（`$0.11844/+40%/+39%` 来自 0.85 行，`+79%` 是选中行）：
   已统一到选中阈值行并给 0.85 行单独登记 claim。

## 5. 遗留与建议

| 项 | 状态 | 建议 |
|---|---|---|
| 过期租约接管、分布式协调 | **未做**（本包只允许显式 acquire） | 作为独立的一波做，需要先改语义文档 |
| 掉电语义（`synchronous=FULL` + fsync 计时） | 仍不在范围（只做探测器） | 需要新语义承诺，属于所有者的决定 |
| `-wal` 截断、位翻转 | 留在 A §5 待办 | 与"读侧快照纪律"分开做 |
| live 模型的工具调用 | 只下发工具名与描述（无参数 schema） | 需要为 `Tool` 建模参数 schema，属独立工作 |
| 噪声人格的混合比例 | `both` 按概率混合，小样本可能全落一种形态 | 报告里必须写样本量（已在 `docs/noisy-reasoner.md` §6 写明） |
| 变异基线 | 本轮因新增代码重算过一次（625 幸存 / 率 0.3774） | 每次改被变异模块都要重算并在 PR 说明原因 |
| 六个分支的合并 | 待所有者开 PR（堆叠：`docs/open-questions-answered` → `feat/facts-gate` → … → `feat/otel-sweep-lease`） | 建议按分支顺序合并；合并后 main 上的 CI 会同时跑 verify 的三条作业 |
