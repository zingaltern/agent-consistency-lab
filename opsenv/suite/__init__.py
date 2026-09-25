"""评测套件：同一批场景跑四类系统，产出可比的架构指标。

指标定义（每条都能从 RunResult 逐行复算）：

* ``correct``           诊断与动作都与 ground truth 一致
* ``resolved``          环境判定故障已解除（= 执行了正确动作）
* ``red_line``          执行了场景声明的红线动作（含静态拒绝列表之外的**新动作**）
* ``novel_red_line``    执行的正是"规则集里没有的新破坏性动作"
* ``gated``             写操作被交给人工（而不是直接执行）
* ``blocked``           写操作被人工拦下
* ``sufficient``        取证覆盖了决定性通道
* ``steps / cost``      步数与账本口径成本（读证据 + 推理输出，四类系统同一价格表）

两个推理器人格共用同一批场景：
* ``competent-honest``：能力 0.9，证据不足时承认不知道（不乱动）
* ``weak-guesser``    ：能力 0.6，证据不足时按最强信号硬猜（更容易出事）


本包是"门面 + 五个职责单一的子模块"：`run`（跑批与聚合）、`stats`（评分口径与配对统计）、
`gates`（可执行门禁）、`report`（渲染）、`cli`（命令行）。下面把拆分前
``opsenv/suite.py`` 的**全部公开符号**显式 re-export，因此：

* ``python -m opsenv.suite`` 仍然可用（``__main__.py`` 调 ``cli.main``）；
* ``from opsenv.suite import X`` 对拆分前存在的每个名字都仍然可用；
* 依赖方向单向 ``run`` ← ``stats`` ← ``gates`` ← ``report`` ← ``cli``，无循环 import。

``PROFILES`` 是唯一的例外：它定义在 `run.py`，但 ``noisy_profiles`` 与 ``main`` 通过
``run._live_profiles()`` **在调用时**从本门面读取——保留"改 ``opsenv.suite.PROFILES``
即改口径"这一既有行为（``docs/independent-test-2026-09-17/commands.sh`` 依赖它换
``noise_seed``）。
"""
from __future__ import annotations

from harness.tokens import PriceTable

# 以下 import 组是"拆分前 suite.py 自己 import 进来的名字"：它们当时就在
# `opsenv.suite` 的命名空间里，为不打断任何既有 import 而一并保留。
from ..policy import Disposition, NoiseFlavor, ReasonerProfile, noise_rng_for
from ..scenario import Scenario, build_catalog, summarise
from ..stats import (
    Interval,
    cohens_kappa,
    kappa_label,
    min_detectable_effect,
    paired_bootstrap_diff,
    paired_min_detectable_effect,
    wilson_interval,
)
from ..systems import SYSTEMS, LazyOperator, Operator, RunResult, run_system
from .cli import main
from .gates import MIN_RUNS_PER_CELL, NOISE_ALLOWED_RED, GateResult, check_gates, render_gates
from .report import (
    findings,
    grader_sensitivity,
    proxy_section,
    render_markdown,
    render_reasoner_banner,
    split_breakdown,
    statistical_notes,
)
from .run import PROFILES, Cell, aggregate, group_by_profile, noisy_profiles, run_suite
from .stats import (
    GRADERS,
    discordant_pairs,
    grade,
    grader_rates,
    pair_key,
    paired_compare,
    proxy_calibration,
    rate_with_interval,
)

__all__ = [
    "GRADERS",
    "MIN_RUNS_PER_CELL",
    "NOISE_ALLOWED_RED",
    "PROFILES",
    "SYSTEMS",
    "Cell",
    "Disposition",
    "GateResult",
    "Interval",
    "LazyOperator",
    "NoiseFlavor",
    "Operator",
    "PriceTable",
    "ReasonerProfile",
    "RunResult",
    "Scenario",
    "aggregate",
    "build_catalog",
    "check_gates",
    "cohens_kappa",
    "discordant_pairs",
    "findings",
    "grade",
    "grader_rates",
    "grader_sensitivity",
    "group_by_profile",
    "kappa_label",
    "main",
    "min_detectable_effect",
    "noise_rng_for",
    "noisy_profiles",
    "pair_key",
    "paired_bootstrap_diff",
    "paired_compare",
    "paired_min_detectable_effect",
    "proxy_calibration",
    "proxy_section",
    "rate_with_interval",
    "render_gates",
    "render_markdown",
    "render_reasoner_banner",
    "run_suite",
    "run_system",
    "split_breakdown",
    "statistical_notes",
    "summarise",
    "wilson_interval",
]
