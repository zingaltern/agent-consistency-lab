"""四类系统在同一批场景上的实现：

* ``workflow``   ：确定性规则 + **静态拒绝列表**（无模型调用）
* ``single_shot``：一次性读全部通道 + 单次推理 + 直接执行（无 gate）
* ``langgraph``  ：LangGraph 图（collect → diagnose → 静态断点人工审批 → act），带 MemorySaver
* ``harness``    ：本项目的 Loop（自适应取证 + 工具自述风险触发审批 + 事件日志）

四者共用同一个推理器（``policy.diagnose``）、同一个环境（``OpsEnvironment``）与同一套
token 估算，因此差异只能来自**架构**：取证策略、审批形态、以及"模型出错时会不会变成事故"。

取证策略（agent 类系统共用，理想化）：按 metrics → resources → logs → changes 的顺序
逐通道取证，当某个通道的异常分超过阈值就停止。异常分只在**决定性通道**上超过阈值
（其余通道给出的是指向混淆项的弱信号），因此"取证是否充分"完全由故障类型决定。
评测衡量的是架构在"完美/不完美推理"下的表现，不是一个真实 agent 的取证能力——
这条假设写在 docs/w5-report.md 的边界一节。


本包按"共用件 + 四条路线"拆成六个模块：

* `base.py`            四类系统共用的底件（RunResult / Operator / 取证与收尾辅助）
* `workflow.py`        路线 1：确定性规则 + 静态拒绝列表（无模型调用）
* `single_shot.py`     路线 2：一次性读全部通道 + 单次推理 + 直接执行（含反事实基线 rule_full）
* `langgraph_system.py` 路线 3：LangGraph 图 + MemorySaver + 静态断点审批
* `harness_system.py`  路线 4：本项目的 Loop（自适应取证 + 工具自述风险 + 事件日志）
* `__init__.py`        （本文件）门面：SYSTEMS / ABLATION_SYSTEMS / run_system 与全部公开符号

依赖方向单向：`base` ← 四条路线 ← `__init__`，无循环 import。下面把拆分前
``opsenv/systems.py`` 命名空间里的名字显式 re-export（含私有辅助），因此旧的 import
路径全部继续可用；真正需要逐行读某个机制时，直接看对应子模块。
"""

from __future__ import annotations

from typing import Any

from harness.artifacts import ArtifactStore
from harness.chaos import Chaos
from harness.llm import ModelWindow, ScriptedLLMClient
from harness.loop import DEFAULT_SYSTEM_PROMPT, Loop
from harness.model import ModelTurn
from harness.store.checkpoints import SqliteCheckpointSaver
from harness.store.sqlite_store import SqliteStore
from harness.tokens import PriceTable, Usage, estimate_tokens
from harness.tools import Effect, Tool, ToolCallRequest, ToolRegistry

# 以下两组是"拆分前 systems.py 自己 import 进来的名字"：它们当时就在 `opsenv.systems`
# 的命名空间里，为不打断任何既有 import 而一并保留（私有辅助也保留，理由相同）。
from ..environment import OpsEnvironment
from ..policy import Diagnosis, ReasonerProfile, diagnose, rule_diagnose
from ..scenario import (
    ALL_ACTIONS,
    CHANGE_RECENCY_PREFIX,
    FAULT_SPECS,
    READ_CHANNELS,
    STATIC_DENY_LIST,
    WRITE_ACTIONS,
    FaultSpec,
    Scenario,
)
from .base import (
    ANOMALY_THRESHOLD,
    CHANNEL_ORDER,
    PROMPT_OVERHEAD_TOKENS,
    TOOL_SCHEMA_TEXT,
    LazyOperator,
    Operator,
    RunResult,
    adaptive_channels,
    anomaly_score,
    cumulative_input_tokens,
)
from .base import _cost as _cost
from .base import _finish as _finish
from .base import _tokens_for_reads as _tokens_for_reads
from .harness_system import OpsPolicyModel, build_ops_registry, run_harness
from .harness_system import _agent_output_tokens as _agent_output_tokens
from .harness_system import _pending_action as _pending_action
from .harness_system import _run_state as _run_state
from .langgraph_system import LANGGRAPH_OUTPUT_TOKENS, GraphState, run_langgraph
from .single_shot import (
    SINGLE_SHOT_OUTPUT_TOKENS,
    rule_diagnose_full,
    run_rule_full,
    run_single_shot,
)
from .single_shot import _decisive_channel_of as _decisive_channel_of
from .workflow import WORKFLOW_CHANNELS, run_workflow

# 主对照四条路线；rule_full 是反事实基线（默认不参与主表，由消融脚本单独跑）
SYSTEMS = ("workflow", "single_shot", "langgraph", "harness")


ABLATION_SYSTEMS = (*SYSTEMS, "rule_full")


def run_system(name: str, **kwargs: Any) -> RunResult:
    if name == "workflow":
        return run_workflow(**kwargs)
    if name == "rule_full":
        return run_rule_full(**kwargs)
    if name == "single_shot":
        return run_single_shot(**kwargs)
    if name == "langgraph":
        return run_langgraph(**kwargs)
    if name == "harness":
        return run_harness(**kwargs)
    raise ValueError(f"unknown system: {name}")


__all__ = [
    "ABLATION_SYSTEMS",
    "ALL_ACTIONS",
    "ANOMALY_THRESHOLD",
    "CHANGE_RECENCY_PREFIX",
    "CHANNEL_ORDER",
    "DEFAULT_SYSTEM_PROMPT",
    "FAULT_SPECS",
    "LANGGRAPH_OUTPUT_TOKENS",
    "PROMPT_OVERHEAD_TOKENS",
    "READ_CHANNELS",
    "SINGLE_SHOT_OUTPUT_TOKENS",
    "STATIC_DENY_LIST",
    "SYSTEMS",
    "TOOL_SCHEMA_TEXT",
    "WORKFLOW_CHANNELS",
    "WRITE_ACTIONS",
    "ArtifactStore",
    "Chaos",
    "Diagnosis",
    "Effect",
    "FaultSpec",
    "GraphState",
    "LazyOperator",
    "Loop",
    "ModelTurn",
    "ModelWindow",
    "Operator",
    "OpsEnvironment",
    "OpsPolicyModel",
    "PriceTable",
    "ReasonerProfile",
    "RunResult",
    "Scenario",
    "ScriptedLLMClient",
    "SqliteCheckpointSaver",
    "SqliteStore",
    "Tool",
    "ToolCallRequest",
    "ToolRegistry",
    "Usage",
    "adaptive_channels",
    "anomaly_score",
    "build_ops_registry",
    "cumulative_input_tokens",
    "diagnose",
    "estimate_tokens",
    "rule_diagnose",
    "rule_diagnose_full",
    "run_harness",
    "run_langgraph",
    "run_rule_full",
    "run_single_shot",
    "run_system",
    "run_workflow",
]
