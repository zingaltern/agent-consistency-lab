"""工具层：幂等键语义、参数规范化 golden hash、效果声明。"""

from __future__ import annotations

import pytest

from harness.tools import (
    Effect,
    Tool,
    ToolRegistry,
    canonical_args_sha256,
    canonical_json,
    idempotency_key,
)


class TestIdempotencyKey:
    def test_key_is_deterministic(self) -> None:
        assert idempotency_key("run_demo", "br_1", "tc_write_1") == idempotency_key(
            "run_demo", "br_1", "tc_write_1"
        )
        # golden：键的构造一旦改变，历史记录的键全部失效，因此钉死
        assert (
            idempotency_key("run_demo", "br_1", "tc_write_1")
            == "idem_715b38aa215930a13ce007cf517936e8"
        )

    def test_key_depends_on_run_branch_and_call_id(self) -> None:
        base = idempotency_key("run_a", "br_1", "tc_1")
        assert base != idempotency_key("run_b", "br_1", "tc_1")
        assert base != idempotency_key("run_a", "br_2", "tc_1")
        assert base != idempotency_key("run_a", "br_1", "tc_2")
        # 键不含 step、不含参数：改参是"新调用"，由新 tool_call_id 表达；
        # 但必须含 branch——分叉分支上的同一 tool_call_id 是另一次逻辑调用
        assert idempotency_key("run_a", "br_1", "tc_1") == base


class TestCanonicalJson:
    def test_key_order_and_whitespace_normalized(self) -> None:
        assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_unicode_is_preserved(self) -> None:
        assert canonical_json({"svc": "支付"}) == '{"svc":"支付"}'

    def test_nan_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})

    def test_args_sha256_golden(self) -> None:
        assert (
            canonical_args_sha256({"service": "payment", "size": 64})
            == "2b051fb7052f1f4522082a58aa51438b566729c84288f05a3a5b0bfda961f291"
        )

    def test_args_hash_detects_parameter_change(self) -> None:
        assert canonical_args_sha256({"size": 64}) != canonical_args_sha256({"size": 65})


class TestRegistry:
    def _tool(self, name: str, effect: Effect) -> Tool:
        return Tool(name=name, effect=effect, fn=lambda args, key: {"ok": True})

    def test_register_and_get(self) -> None:
        registry = ToolRegistry()
        registry.register(self._tool("scale_pool", Effect.WRITE_NONIDEMPOTENT))
        assert registry.names() == ["scale_pool"]
        assert registry.get("scale_pool").effect is Effect.WRITE_NONIDEMPOTENT

    def test_duplicate_registration_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(self._tool("a", Effect.READ))
        with pytest.raises(ValueError, match="duplicate tool"):
            registry.register(self._tool("a", Effect.READ))

    def test_unknown_tool_raises(self) -> None:
        with pytest.raises(KeyError, match="unknown tool"):
            ToolRegistry().get("nope")
