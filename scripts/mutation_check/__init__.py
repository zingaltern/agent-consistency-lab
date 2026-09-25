"""变异测试门禁（R-A5）：把"测试盲区"变成一条会红的门禁。

语义：**幸存的变异 = 测试盲区**。变异测试与 `tests/test_audit_regressions.py` 互补——
审计回归防已知的 P0 复活，变异测试探没人写过的分支。

为什么是"防倒退基线"而不是"追求某个绝对值"：
* 这几个模块里相当一部分路径由**子进程驱动**的崩溃矩阵与四系统评测覆盖（`experiments/`、
  `opsenv/`），它们不在 `pytest` 进程内，mutmut 看不见——因此幸存清单里必然包含
  "其实被更外层验证保护着"的变异。把幸存率当绝对质量分会得出错误结论。
* 真正要防的是：**新增代码把盲区扩大**。所以首版把当前幸存清单冻结成基线
  （`reports/mutation_baseline.json`），之后只判"有没有**新增**幸存变异"。

## 全状态记账（2026-09-18 修，来源：独立验证报告 P0-1 / P0-2）

首版判据只看本轮 `survived` 里有没有新名字。这有两个后果，都被独立验证的**反例**证伪过：

* **P0-1｜"缺项"被当成好消息。** 任何**从本轮结果里消失**的基线幸存变异
  （未评估 / `segfault` / `timeout` / `no tests`）都会落进 `fixed`，
  被打印成"已被杀死（好事）"并**退出 0**。判据结构上缺项**不可能**导致红灯。
  活体场景：`harness/store/checkpoints.py` 逐字未改，76 条基线幸存变异在某轮里
  被 mutmut 改判成 `segfault`——`segfault` 原先是"不计入任何计数"的垃圾桶。
* **P0-2｜抓不到"新增的、完全没被测的代码"。** 新增的变异体被判为 `no tests`，
  而判据只看 `survived` ⇒ 覆盖率为零的新代码**不会变红**，与 AGENTS.md 红线 5 冲突。

因此本脚本现在把 mutmut 的**全部状态**分三类记账，**不再静默丢弃任何一个**：

| 类 | 状态 | 含义 |
|---|---|---|
| 判定类 | `killed` / `survived` | 真的得到了判决 |
| 未覆盖类 | `no tests` | 连覆盖都没有（另一类盲区） |
| 无结论类 | 见下 | 跑了但**判决不可信**，或根本没判决 |

无结论类 = `segfault` / `timeout` / `suspicious` / `skipped` / `not checked` /
`caught by type check` / `check was interrupted by user`
（名单在代码里是 `INCONCLUSIVE_STATUSES`，来自 `mutmut/stats.py::status_by_exit_code`）。

后两类合起来是**不可见空间**：它们不参与 `survivor_rate`，每次运行都打印其规模——
`survivor_rate` **不是覆盖率**，别读成覆盖率。

### 六条红灯条件（2026-09-18 起；第七条见下）

1. 新增幸存变异（保留首版行为；基线里 `killed` 的变异体变成 `survived` 也算新增）；
2. 基线里是**判定类**的变异体，本轮变成**非判定类**（P0-1 的活体场景）；
3. 基线里的变异体本轮**完全缺失**（未评估 / 改名 / 缓存缺项）；
4. 新增 `no tests`（P0-2：对"新增的、完全没被测的代码"不敏感）；
5. 本轮**没有任何变异体**、或**没有任何一条被判定的变异体**（"跑不起来"≠"没有盲区"）；
6. **无结论集合较基线增长**（有新名字进入不可见空间）。

外加一条 fail-closed：出现**本脚本不认识的状态名** ⇒ 判失败（mutmut 升版带来的新分类
必须先被归类，否则就是新一轮"静默丢数据"）。

### 第七条：同名不同指纹（2026-09-26 加）

判据只按"名字 + 状态"对账时，有一格是空的（独立验证 2026-09-18 · 报告 §5-3）：
同一函数体内的**等量改写**（常数改值、语句换序等既不增删条数、也不移位编号的改动）会让
同一批名字指向**不同**的变异，而门禁看不出差别——可能**静默绿**。
现在基线为每个变异体存一条**内容指纹**（`sha256` of 规范化后的变异 diff），
对账时"同名不同指纹"报 `[mutant-content-changed]` 并退出 1。

指纹的口径（两句话）：

* 内容 = mutmut 自己渲染的变异函数 diff（`get_diff_for_mutant`，与 `mutmut show` 的
  diff 部分**逐字相同**，本机实测 40 条样本零差异）；取它在**进程内**算，
  2050 条约 17 秒——用 `mutmut show` 起子进程要 219 ms/条（2050 条约 448 秒），
  且逐条重扫 `find_mutant`；
* 规范化 = 逐行去尾空格 + 丢弃首尾空行（行尾空格与文件末尾换行不构成"内容变了"）。
  指纹只回答"这条变异还是不是原来那条"，**不是**质量分。

指纹拿不到时（`mutants/` 缺文件、`.meta` 里没有这个名字）如实记 `None` 并打印条数：
**不比对、不假装比对过**。刷新基线时若一条都取不到，直接判失败（不许写一份没有指纹的基线，
那等于把这条判据静默关掉）。


用法::

    .venv/bin/python scripts/mutation_check.py                     # 判定（CI nightly 用）
    .venv/bin/python scripts/mutation_check.py --update-baseline   # 显式刷新基线（PR 里说明）
    .venv/bin/python scripts/mutation_check.py --module harness/approval.py --timeout 300
    .venv/bin/python scripts/mutation_check.py --module "harness.approval.*" --timeout 300

纪律：

* **超时即失败**：跑不完不许当成功（"没跑完"和"没发现回归"是两件事）；
* 新增幸存变异 → 退出 1，并逐条打印 `mutmut show` 的 diff（失败必须可读）；
* 基线只能由 `--update-baseline` 改写，且必须在 PR 描述里写明为什么放宽；
* **`--update-baseline` 必须从干净缓存开始**：`mutants/` 还在就整体移开
  （见 `baseline_refresh_plan`）。要沿用旧缓存得显式写 `--allow-incremental-refresh`，
  并且它会大声警告——2026-09-19 实测过一次"混合了旧判决的假基线"：
  14.7 秒"跑完"、`no tests` 从 18 虚增到 241。

## 拆包（2026-09-26，纯代码搬移）

本包是把拆包前的单文件 `scripts/mutation_check.py`（1126 行）按职责切成 7 个模块：

| 模块 | 装什么 |
|---|---|
| `config.py` | 路径与词表：`PROJECT_ROOT` / `MUTANTS_DIR` / 状态三分类；**唯一一份可变开关** |
| `status.py` | 读 mutmut 状态表并折算"全状态记账"摘要 |
| `fingerprint.py` | 变异体内容指纹（条件 7 的原料） |
| `baseline.py` | 基线的读写、刷新计划、"更宽即拒绝"守卫 |
| `gate.py` | 七条红灯条件 + 未知状态 fail-closed |
| `reporting.py` | 产物落盘与"判定作废"出口 |
| `runtime.py` | runner + 命令行（`main` 与它调用的全局名都在这里） |

**门面契约**：拆包前 `scripts.mutation_check` 命名空间里的名字（含 import 进来的
`subprocess` / `time` / `Path` 这类）**一个不少**地 re-export，因此
`python -m scripts.mutation_check` 与 8 条 claim 的
`{python} -m scripts.mutation_check --summary-only` 全部继续可用。
要加新代码就加到对应子模块，不要往门面里塞逻辑。

**测试改开关时改哪里**：`main` 与它调用的东西都在 `runtime`，所以
`tests/test_mutation_gate.py` 的 `_load_script()` 返回 `runtime` 子模块——
`MUTANTS_DIR` / `run_mutmut` / `collect_status` / `compute_mutant_fingerprints` 都在那里被替换。
门面上的同名名字是**只读副本**，改它们不会生效（与 `opsenv.suite.PROFILES` 同一个坑）。

---
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

from .baseline import (
    _summary_payload,
    apply_baseline_refresh_plan,
    baseline_refresh_plan,
    baseline_widening,
    load_baseline,
    status_map_from_payload,
)
from .config import (
    DECIDED_STATUSES,
    DEFAULT_BASELINE,
    DEFAULT_MODULES,
    INCONCLUSIVE_STATUSES,
    KNOWN_STATUSES,
    MUTANTS_DIR,
    PROJECT_ROOT,
    UNCOVERED_STATUSES,
)
from .fingerprint import (
    FINGERPRINT_ALGORITHM,
    _chdir,
    compute_mutant_fingerprints,
    fingerprint_comparison_lines,
    fingerprint_report,
    mutant_paths_in_cache,
    normalize_fingerprint_source,
)
from .gate import (
    _invisible_space_lines,
    _report_line,
    gate_verdict,
)
from .reporting import (
    _abort,
    _write_json_out,
)
from .runtime import (
    _mutmut_argv,
    main,
    normalize_module_filter,
    run_mutmut,
)
from .status import (
    all_mutant_names,
    collect_status,
    mutation_summary,
    show_mutant,
)

__all__ = [
    "DECIDED_STATUSES",
    "DEFAULT_BASELINE",
    "DEFAULT_MODULES",
    "FINGERPRINT_ALGORITHM",
    "INCONCLUSIVE_STATUSES",
    "KNOWN_STATUSES",
    "MUTANTS_DIR",
    "PROJECT_ROOT",
    "UNCOVERED_STATUSES",
    "Any",
    "Iterator",
    "NoReturn",
    "Path",
    "_abort",
    "_chdir",
    "_invisible_space_lines",
    "_mutmut_argv",
    "_report_line",
    "_summary_payload",
    "_write_json_out",
    "all_mutant_names",
    "apply_baseline_refresh_plan",
    "argparse",
    "baseline_refresh_plan",
    "baseline_widening",
    "collect_status",
    "compute_mutant_fingerprints",
    "contextmanager",
    "fingerprint_comparison_lines",
    "fingerprint_report",
    "gate_verdict",
    "hashlib",
    "json",
    "load_baseline",
    "main",
    "mutant_paths_in_cache",
    "mutation_summary",
    "normalize_fingerprint_source",
    "normalize_module_filter",
    "os",
    "run_mutmut",
    "show_mutant",
    "status_map_from_payload",
    "subprocess",
    "sys",
    "time",
]
