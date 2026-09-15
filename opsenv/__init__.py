"""运维壳与场景集：把 W1–W4 的 runtime 放到"真实一点的业务壳"上验证。

与 ``fakeworld`` 的分工：
* ``fakeworld``：崩溃一致性实验的受控世界（单场景、副作用账本、探针）；
* ``opsenv``  ：场景驱动的运维壳（多故障类型、四通道取证、带审批的处置动作），
  用于四类系统的对照评测（workflow / single-shot / LangGraph / harness）。
"""

from .environment import OpsEnvironment
from .policy import Disposition, ReasonerProfile, diagnose, rule_diagnose
from .scenario import (
    READ_CHANNELS,
    STATIC_DENY_LIST,
    WRITE_ACTIONS,
    Evidence,
    Fault,
    Scenario,
    build_catalog,
    summarise,
)
from .systems import SYSTEMS, Operator, RunResult, run_system

__all__ = [
    "READ_CHANNELS",
    "STATIC_DENY_LIST",
    "SYSTEMS",
    "WRITE_ACTIONS",
    "Disposition",
    "Evidence",
    "Fault",
    "Operator",
    "OpsEnvironment",
    "ReasonerProfile",
    "RunResult",
    "Scenario",
    "build_catalog",
    "diagnose",
    "rule_diagnose",
    "run_system",
    "summarise",
]
