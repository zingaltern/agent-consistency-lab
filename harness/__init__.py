"""agent-consistency-lab：面向崩溃一致性与治理语义的 Agent runtime 实验台。"""

from .chaos import WINDOWS, Chaos
from .events import ArtifactEventType, Event, EventKind, NewEvent, Source, TreeEventType
from .loop import Loop, Message, Model, ModelTurn, RunOutcome
from .state import DerivedState, RunStatus, Violation, reduce_events
from .tools import (
    Effect,
    Tool,
    ToolCallRequest,
    ToolRegistry,
    ToolResult,
    canonical_args_sha256,
    canonical_json,
    idempotency_key,
)

__version__ = "0.2.0"

__all__ = [
    "WINDOWS",
    "ArtifactEventType",
    "Chaos",
    "DerivedState",
    "Effect",
    "Event",
    "EventKind",
    "Loop",
    "Message",
    "Model",
    "ModelTurn",
    "NewEvent",
    "RunOutcome",
    "RunStatus",
    "Source",
    "Tool",
    "ToolCallRequest",
    "ToolRegistry",
    "ToolResult",
    "TreeEventType",
    "Violation",
    "__version__",
    "canonical_args_sha256",
    "canonical_json",
    "idempotency_key",
    "reduce_events",
]
