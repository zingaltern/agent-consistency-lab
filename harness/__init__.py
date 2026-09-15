"""agent-consistency-lab：面向崩溃一致性与治理语义的 Agent runtime 实验台。"""

from .approval import ApprovalBinding, ApprovalError
from .artifacts import ArtifactRef, ArtifactStore, make_read_artifact_tool
from .budget import Bucket, BudgetExceeded, BudgetLedger, BudgetLimits, BudgetSnapshot
from .cache import CacheConfig, CacheOutcome, PrefixCacheModel
from .chaos import WINDOWS, Chaos
from .compaction import CompactionPolicy, CompactionRecord, Compactor
from .context import Block, View, ViewBuilder
from .events import ArtifactEventType, Event, EventKind, NewEvent, Source, TreeEventType
from .llm import ContextOverflow, ModelResponse, ModelWindow, ScriptedLLMClient
from .loop import DEFAULT_SYSTEM_PROMPT, InterruptSignal, Loop, LoopError, RunOutcome
from .model import Model, ModelTurn
from .state import DerivedState, RunStatus, Violation, reduce_events
from .tokens import PriceTable, Usage, estimate_tokens
from .tools import (
    Effect,
    ProbeOutcome,
    ProbeResult,
    Tool,
    ToolCallRequest,
    ToolRegistry,
    canonical_args_sha256,
    canonical_json,
    idempotency_key,
)

__version__ = "0.4.0"

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "WINDOWS",
    "ApprovalBinding",
    "ApprovalError",
    "ArtifactEventType",
    "ArtifactRef",
    "ArtifactStore",
    "Block",
    "Bucket",
    "BudgetExceeded",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetSnapshot",
    "CacheConfig",
    "CacheOutcome",
    "Chaos",
    "CompactionPolicy",
    "CompactionRecord",
    "Compactor",
    "ContextOverflow",
    "DerivedState",
    "Effect",
    "Event",
    "EventKind",
    "InterruptSignal",
    "Loop",
    "LoopError",
    "Model",
    "ModelResponse",
    "ModelTurn",
    "ModelWindow",
    "NewEvent",
    "PrefixCacheModel",
    "PriceTable",
    "ProbeOutcome",
    "ProbeResult",
    "RunOutcome",
    "RunStatus",
    "ScriptedLLMClient",
    "Source",
    "Tool",
    "ToolCallRequest",
    "ToolRegistry",
    "TreeEventType",
    "Usage",
    "View",
    "ViewBuilder",
    "Violation",
    "__version__",
    "canonical_args_sha256",
    "canonical_json",
    "estimate_tokens",
    "idempotency_key",
    "make_read_artifact_tool",
    "reduce_events",
]
