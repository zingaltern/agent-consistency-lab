"""受控仿真世界：副作用账本 + 仿真工具 + 脚本化模型。

这里刻意不叫 "fakeworld 引擎"：它只提供 fixture 级别的确定性环境，
所有 ground truth（效果次数）由账本提供，而不是由仿真器"扮演"。
"""

from .model import ScriptedModel
from .tools import (
    SCENARIO_LONG_INCIDENT,
    SCENARIO_POOL_EXHAUSTION,
    SCENARIO_TWO_WRITES,
    build_registry,
    scripted_turns,
)
from .world import World

__all__ = [
    "SCENARIO_LONG_INCIDENT",
    "SCENARIO_POOL_EXHAUSTION",
    "SCENARIO_TWO_WRITES",
    "ScriptedModel",
    "World",
    "build_registry",
    "scripted_turns",
]
