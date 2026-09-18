"""工具层：效果声明、幂等键、参数规范化。

三条约束（对应评审对原设计的修正）：

1. **幂等键 = hash(run_id, tool_call_id)**，不含 step、不含参数。理由：
   step 在分叉/重试时会漂移；参数一旦被审批改写，语义上就是一次新调用
   （Stripe 口径：改参即换键），把参数放进键会让"改参"变成"重放"。
2. **参数 hash 只作旁路校验**（``args_sha256``）：用于检测"同键不同参"，
   即下游去重会掩盖参数变更的那类事故。
3. **效果类别由工具声明**，不从错误文本推断：
   ``read`` / ``write_idempotent`` / ``write_nonidempotent``。

``write_nonidempotent`` 的工具在崩溃窗口 2（效果已发生、记录未落盘）下必然产生
重复副作用——这正是 W2 要测出来的结论，也是 W3 outbox 的动机。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Effect(StrEnum):
    READ = "read"
    WRITE_IDEMPOTENT = "write_idempotent"
    WRITE_NONIDEMPOTENT = "write_nonidempotent"


class ToolCallRequest(BaseModel):
    """模型提出的一次工具调用请求。"""

    tool_call_id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ProbeOutcome(StrEnum):
    """下游按键读回的三种结论；UNKNOWN 表示"读不出真实状态"。"""

    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


class ProbeResult(BaseModel):
    outcome: ProbeOutcome
    detail: dict[str, Any] = Field(default_factory=dict)


ToolFn = Callable[[dict[str, Any], str], dict[str, Any]]
"""工具实现签名：(args, idempotency_key) -> result。

幂等实现应当用 ``idempotency_key`` 做 upsert；非幂等实现忽略它并直接追加效果。
"""

ProbeFn = Callable[[dict[str, Any], str], ProbeResult]
"""探针签名：(args, idempotency_key) -> ProbeResult。

只有下游支持"按键读回"时才可能存在；没有探针的非幂等写，在崩溃后只能标记
``unknown`` 交人工对账，绝不自动重跑。
"""


class ArgPolicyError(ValueError):
    """策略定义本身不合法（在**注册期**就报错，不留到执行期）。"""


class ArgPolicy(BaseModel):
    """工具自述的**参数安全域**（R-B2）：一层字段 + 三类约束。

    哲学定位与 ``requires_approval`` 一致：**工具在注册时声明自己的安全域**，
    审批门负责执行它——这不是一张外置的黑名单（那会让"谁有权定义危险"变成运维策略问题，
    而不是工具契约问题）。

    支持（M2 承诺的深度）：

    * ``allowed``：字段值必须在集合内（**缺字段即拒**：无法核对时不能默认放行）；
    * ``forbidden``：字段命中集合即拒（缺字段视为通过——没有危险值出现）；
    * ``min`` / ``max``：数值区间（含端点；非数值或缺失即拒）。

    不支持（扩展点，语义文档写明）：嵌套字段、类型强转、正则、跨字段约束。
    """

    field: str
    allowed: list[Any] | None = None
    forbidden: list[Any] | None = None
    min: float | None = None
    max: float | None = None
    note: str = ""

    @model_validator(mode="after")
    def _validate_definition(self) -> ArgPolicy:
        if not self.field or not isinstance(self.field, str):
            raise ArgPolicyError("arg_policy 必须给出非空的 field")
        if not any(
            item is not None
            for item in (self.allowed, self.forbidden, self.min, self.max)
        ):
            raise ArgPolicyError(
                "arg_policy 至少要声明 allowed / forbidden / min / max 之一，否则等于没有策略"
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ArgPolicyError(f"arg_policy 区间非法：min={self.min} > max={self.max}")
        for name, value in (("min", self.min), ("max", self.max)):
            if value is not None and not isinstance(value, (int, float)):
                raise ArgPolicyError(f"arg_policy 的 {name} 必须是数字")
        return self

    @staticmethod
    def _matches(value: Any, candidates: list[Any]) -> bool:
        """集合成员判断，但**布尔只与布尔相等**。

        独立测试 P2-8：`Trueness == 1` 在 Python 里成立，于是 `allowed=[1]` 会把 `True`
        放进白名单（`forbidden=[1]` 也会顺带把 `True` 拦下——方向相反但同源）。
        数值白名单里混进布尔值属于"用真值穿透类型"，在安全域判定里必须显式拒绝。
        """
        if isinstance(value, bool):
            return any(isinstance(item, bool) and item is value for item in candidates)
        return any(not isinstance(item, bool) and item == value for item in candidates)

    def evaluate(self, args: Mapping[str, Any]) -> tuple[bool, str]:
        """返回 ``(是否放行, 原因)``；原因会进错误事件与 tool_result，必须是稳定标识串。"""
        present = self.field in args
        value = args.get(self.field)
        if self.forbidden is not None and present and self._matches(value, self.forbidden):
            return False, f"arg_policy_forbidden_value:{self.field}={value!r}"
        if self.allowed is not None:
            if not present:
                return False, f"arg_policy_field_missing:{self.field}"
            if not self._matches(value, self.allowed):
                return False, f"arg_policy_value_not_allowed:{self.field}={value!r}"
        if self.min is not None or self.max is not None:
            if not present:
                return False, f"arg_policy_field_missing:{self.field}"
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, f"arg_policy_not_numeric:{self.field}={value!r}"
            if self.min is not None and float(value) < float(self.min):
                return False, f"arg_policy_below_min:{self.field}={value!r}<{self.min}"
            if self.max is not None and float(value) > float(self.max):
                return False, f"arg_policy_above_max:{self.field}={value!r}>{self.max}"
        return True, "ok"

    def describe(self) -> str:
        parts = []
        if self.allowed is not None:
            parts.append(f"allowed={self.allowed}")
        if self.forbidden is not None:
            parts.append(f"forbidden={self.forbidden}")
        if self.min is not None:
            parts.append(f"min={self.min}")
        if self.max is not None:
            parts.append(f"max={self.max}")
        return f"{self.field}: " + ", ".join(parts)


@dataclass(frozen=True)
class Tool:
    name: str
    effect: Effect
    fn: ToolFn
    description: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)
    requires_approval: bool = False
    probe: ProbeFn | None = None
    # 参数级安全域（R-B2）：声明在工具上，在执行前一刻用**实际参数**核对
    arg_policy: ArgPolicy | None = None
    # 下发给真实模型的参数 schema（JSON Schema 子集，R-B1 的后续补全）。
    # 只有 live 路径消费它；scripted 路径不读这个字段，留 None 时下发空参数表。
    # 声明必须与实现接受的字段一致——它是"工具自述"的一部分，不是文档。
    parameters: dict[str, Any] | None = None


class ToolRegistry:
    def __init__(self, tools: Mapping[str, Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = dict(tools or {})

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        if tool.arg_policy is not None and not isinstance(tool.arg_policy, ArgPolicy):
            # 允许注册时用 dict 声明，但**立刻**校验：策略写错必须在注册期就炸，
            # 否则会变成"某次执行时才拒绝"——那是把配置错误伪装成运行时事故。
            tool = replace(tool, arg_policy=ArgPolicy.model_validate(tool.arg_policy))
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


# 未声明 ``parameters`` 的工具下发空参数表——**这一处是唯一定义**：
# 下发给模型的 schema（``harness/live_transport.py``）与下发给 MCP 客户端的
# ``inputSchema``（``integrations/mcp_server.py``）都取 ``tool_param_schema``，
# 避免"两个地方各写一份回退形状"然后悄悄分叉。
EMPTY_PARAM_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def tool_param_schema(tool: Tool) -> dict[str, Any]:
    """工具的参数 schema：声明了就用声明，没声明就回退空参数表。"""
    return tool.parameters or EMPTY_PARAM_SCHEMA


def idempotency_key(run_id: str, branch_id: str, tool_call_id: str) -> str:
    """确定性的幂等键 = hash(run_id, branch_id, tool_call_id)。

    为什么含 branch：分叉出的分支会**重新决策**同一个 tool_call_id（例如"拒绝并
    人工处置"分支上模型提出同样的调用），那是两次不同的逻辑调用，键必须不同。
    早期版本只用 (run_id, tool_call_id)，结果子分支会命中父分支的去重行，
    静默复用另一组参数的执行结果、并跳过审批门（审计实测的 P0）。
    键仍然不含 step（会漂移）与参数（改参由新 tool_call_id 表达）。
    """
    digest = hashlib.sha256(f"{run_id}\x1f{branch_id}\x1f{tool_call_id}".encode())
    return f"idem_{digest.hexdigest()[:32]}"


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、无多余空白、保留 Unicode、拒绝 NaN。

    规范化规则一旦改动，历史事件的 hash 全部失效，因此这里配 golden hash 测试。
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def canonical_args_sha256(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(args)).encode("utf-8")).hexdigest()
