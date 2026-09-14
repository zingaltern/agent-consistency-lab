"""agent-consistency-lab：面向崩溃一致性与治理语义的 Agent runtime 实验台。"""

from .events import ArtifactEventType, Event, EventKind, NewEvent, Source, TreeEventType
from .state import DerivedState, RunStatus, Violation, reduce_events

__version__ = "0.1.0"

__all__ = [
    "ArtifactEventType",
    "DerivedState",
    "Event",
    "EventKind",
    "NewEvent",
    "RunStatus",
    "Source",
    "TreeEventType",
    "Violation",
    "__version__",
    "reduce_events",
]
