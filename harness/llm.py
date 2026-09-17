"""LLM 客户端：整个 runtime 里**唯一**的模型调用出口。

把三件事收在一处，是预算与缓存这两条纪律能成立的前提：

1. **窗口检查**：``input + max_output > context_limit`` ⇒ 抛 ``ContextOverflow``，
   由 loop 决定"压缩后重试"还是失败。这是真实约束，不是质量指标。
2. **缓存计费**：请求的块序列交给前缀缓存模型，得到 读/写/普通 三类 token。
3. **预算记账**：dispatch 前硬停检查、响应后按实际用量计费（分桶）。

脚本模型仍然只依赖 ``step``，因此视图内容变化不会影响"模型说什么"——
本轮实验测的是成本与一致性，不是模型质量；这条边界写在 docs/w4-report.md 里。
"""

from __future__ import annotations

import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .budget import Bucket, BudgetLedger
from .cache import CacheConfig, CacheOutcome, PrefixCacheModel, usage_from_cache
from .cassette import (
    CassetteError,
    CassetteMeta,
    CassetteStore,
    RecordedTurn,
    describe_missing,
    normalize_usage,
    prompt_includes_cache,
    request_fingerprint,
)
from .context import View
from .model import Model, ModelTurn
from .tokens import PriceTable, Usage, estimate_tokens
from .tools import ToolCallRequest


class ContextOverflow(RuntimeError):
    def __init__(self, input_tokens: int, limit: int) -> None:
        super().__init__(f"context overflow: {input_tokens} + output > {limit}")
        self.input_tokens = input_tokens
        self.limit = limit


class ModelWindow(BaseModel):
    """窗口约束；``compaction_buffer_tokens`` 是提前压缩的余量（对齐 qwen-code 的做法）。"""

    context_limit_tokens: int = 200_000
    max_output_tokens: int = 4_096
    compaction_buffer_tokens: int = 13_000

    @property
    def usable_input_tokens(self) -> int:
        return self.context_limit_tokens - self.max_output_tokens

    def is_overflow(self, input_tokens: int) -> bool:
        return input_tokens + self.max_output_tokens > self.context_limit_tokens

    def usage_ratio(self, input_tokens: int) -> float:
        return input_tokens / max(1, self.usable_input_tokens)


class ModelResponse(BaseModel):
    turn: ModelTurn
    usage: Usage
    cache: CacheOutcome
    view_fingerprint: str
    context_tokens: int
    cost_usd: float = 0.0
    cache_config_version: str = ""
    price_version: str = ""


class LLMClient(Protocol):
    def complete(self, *, step: int, view: View) -> ModelResponse: ...


class ScriptedLLMClient:
    def __init__(
        self,
        model: Model,
        *,
        cache: PrefixCacheModel | None = None,
        window: ModelWindow | None = None,
        price: PriceTable | None = None,
        budget: BudgetLedger | None = None,
        cache_key: str = "scripted-model",
    ) -> None:
        self._model = model
        self.cache = cache or PrefixCacheModel(CacheConfig())
        self.window = window or ModelWindow()
        self.price = price or PriceTable()
        self._budget = budget
        self._cache_key = cache_key

    def complete(self, *, step: int, view: View) -> ModelResponse:
        if self._budget is not None:
            self._budget.check(Bucket.MAIN)

        blocks = view.block_texts()
        input_tokens = sum(view.sections.values())
        if self.window.is_overflow(input_tokens):
            raise ContextOverflow(input_tokens, self.window.context_limit_tokens)

        outcome = self.cache.fetch(cache_key=self._cache_key, blocks=blocks)
        turn = self._model.next_turn(step=step, view=view)
        usage = usage_from_cache(
            outcome=outcome, blocks=blocks, output_tokens=turn.usage.get("completion_tokens", 0)
        )
        response = ModelResponse(
            turn=turn,
            usage=usage,
            cache=outcome,
            view_fingerprint=view.fingerprint(),
            context_tokens=input_tokens,
            cache_config_version=self.cache.config.version,
            price_version=self.price.version,
        )
        if self._budget is not None:
            response.cost_usd = self._budget.charge(bucket=Bucket.MAIN, usage=usage)
            self._budget.enforce_after_charge(Bucket.MAIN)
        else:
            response.cost_usd = usage.cost_usd(self.price)
        return response


# --------------------------------------------------------------- 三种模型模式（R-B1）

ClientMode = Literal["scripted", "record", "replay"]


class RawCompletion(BaseModel):
    """transport 的原始返回：``text`` + ``tool_calls`` + 原始 usage json。

    只放这三样：loop 靠 tool_calls 前进，计费靠 usage，其余（finish_reason 等）不进
    承诺层——它们属于供应商细节，录下来会制造"看着丰富但没人用"的字段。
    """

    text: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    usage_raw: dict[str, Any] = Field(default_factory=dict)


def total_input_tokens(segments: dict[str, int], raw: dict[str, Any] | None) -> int:
    """把 canonical 段还原成"总输入 token"（与 ``Usage.input_tokens`` 同口径）。

    两家的 ``prompt_tokens`` / ``input_tokens`` 含义不同（见 ``prompt_includes_cache``），
    这个函数是**唯一**的换算点：别处再算一遍就会出现"同一份账单两个总输入"。
    """
    # 用 .get 而不是 []：手工写的/更早版本产出的录制可能缺某些段，
    # 那时按 0 处理是合理退化——缺字段不该让整条回放崩在换算上。
    if prompt_includes_cache(raw):
        return int(segments.get("prompt_tokens", 0))
    return (
        int(segments.get("prompt_tokens", 0))
        + int(segments.get("cache_read_tokens", 0))
        + int(segments.get("cache_write_tokens", 0))
    )


class Transport(Protocol):
    """真实 API 的调用口；测试用内存实现替换它（CI 永不发起在线调用）。"""

    def complete(self, *, blocks: tuple[str, ...], step: int) -> RawCompletion: ...


class ScriptedTransport:
    """把脚本模型包成 transport：让 record/replay 的**端到端**路径可以在无网络下被测试。

    这不是"mock 掉关键机制"：它替换的是**外部供应商**，而本项目要测的是 runtime 对
    模型调用的处理（录制、回放、计费、崩溃兼容）。真实 transport 在
    ``harness/live_transport.py``，只在显式 ``--model record --transport live`` 下使用。

    它按 **Anthropic 口径**报账（``input_tokens`` 不含缓存段 + 两个 cache 字段）：
    这样"供应商报的缓存读写"与脚本模式下的 `PrefixCacheModel` 估算是同一个量，
    record/replay 的一致性比较才有意义（否则两边比的根本不是同一个口径）。
    """

    def __init__(self, model: Model, *, cache: PrefixCacheModel | None = None) -> None:
        self._model = model
        self._cache = cache or PrefixCacheModel(CacheConfig())

    def complete(self, *, blocks: tuple[str, ...], step: int) -> RawCompletion:
        turn = self._model.next_turn(step=step, view=None)
        outcome = self._cache.fetch(cache_key="scripted-model", blocks=tuple(blocks))
        total_input = sum(estimate_tokens(block) for block in blocks)
        billed = max(0, total_input - outcome.read_tokens - outcome.write_tokens)
        return RawCompletion(
            text=turn.text,
            tool_calls=list(turn.tool_calls),
            usage_raw={
                "input_tokens": billed,
                "output_tokens": int(turn.usage.get("completion_tokens", 0) or 0),
                "cache_read_input_tokens": outcome.read_tokens,
                "cache_creation_input_tokens": outcome.write_tokens,
            },
        )


class RecordingLLMClient:
    """``record``：调 transport、把每次调用写进录制物；计费口径 = 录制下来的 canonical usage。

    与 ``ScriptedLLMClient`` 的差别只有一个，但必须说清：**缓存账不重算**。
    真实供应商的服务端缓存命中是它自己的账单，本地复算不可能一致；因此这里直接用
    transport 报的 usage（canonical 四段）过价格表。
    """

    def __init__(
        self,
        transport: Transport,
        *,
        store: CassetteStore,
        window: ModelWindow | None = None,
        price: PriceTable | None = None,
        budget: BudgetLedger | None = None,
        meta: CassetteMeta | None = None,
    ) -> None:
        self._transport = transport
        self._store = store
        self._meta = meta or CassetteMeta()
        self.window = window or ModelWindow()
        self.price = price or PriceTable()
        self._budget = budget
        self._attempts: dict[int, int] = {}
        self.recorded_cost_usd = 0.0

    def _next_attempt(self, step: int) -> int:
        attempt = self._attempts.get(step, 0)
        self._attempts[step] = attempt + 1
        return attempt

    def complete(self, *, step: int, view: View) -> ModelResponse:
        if self._budget is not None:
            self._budget.check(Bucket.MAIN)
        blocks = view.block_texts()
        input_tokens = sum(view.sections.values())
        if self.window.is_overflow(input_tokens):
            raise ContextOverflow(input_tokens, self.window.context_limit_tokens)

        attempt = self._next_attempt(step)
        raw = self._transport.complete(blocks=blocks, step=step)
        usage_segments = normalize_usage(raw.usage_raw)
        usage = Usage(
            input_tokens=total_input_tokens(usage_segments, raw.usage_raw),
            output_tokens=usage_segments["completion_tokens"],
            cache_read_tokens=usage_segments["cache_read_tokens"],
            cache_write_tokens=usage_segments["cache_write_tokens"],
            estimated=False,  # 真实用量：不是估算
        )
        response = ModelResponse(
            turn=ModelTurn(
                text=raw.text, tool_calls=list(raw.tool_calls), usage=dict(raw.usage_raw)
            ),
            usage=usage,
            cache=CacheOutcome(
                read_tokens=usage.cache_read_tokens,
                write_tokens=usage.cache_write_tokens,
                uncached_tokens=usage.billed_input_tokens,
                common_blocks=0,
                miss_reason="live-usage-authoritative",
            ),
            view_fingerprint=view.fingerprint(),
            context_tokens=input_tokens,
            cache_config_version="live-authoritative",
            price_version=self.price.version,
        )
        if self._budget is not None:
            response.cost_usd = self._budget.charge(bucket=Bucket.MAIN, usage=usage)
            self._budget.enforce_after_charge(Bucket.MAIN)
        else:
            response.cost_usd = usage.cost_usd(self.price)
        self.recorded_cost_usd += response.cost_usd
        self._store.append(
            RecordedTurn(
                step=step,
                attempt=attempt,
                text=raw.text,
                tool_calls=list(raw.tool_calls),
                usage=usage_segments,
                usage_raw=raw.usage_raw,
                request_fingerprint=request_fingerprint(blocks),
                cost_usd=response.cost_usd,
                recorded_at=time.time(),
            ),
            meta=self._meta.model_copy(update={"price_version": self.price.version}),
        )
        return response


class ReplayLLMClient:
    """``replay``：按 (step, attempt) 检索录制；usage 直接回放，**不做二次重算**。

    两条刻意的行为：

    * **未命中 ⇒ 可读错误**（说清缺哪份录制、期望的提示词指纹），绝不"猜一个"或静默走默认；
    * **请求指纹不一致 ⇒ 警告级**（记进 ``replay_warnings``，仍然返回录制值）。
      为什么不是错误：崩溃恢复会**合法地**改变视图（探针重建的结果与首次执行不同），
      把它判失败会让"崩溃轨迹的回放"永远无法通过——那是把正常语义误报成缺陷。
    """

    def __init__(
        self,
        *,
        store: CassetteStore,
        window: ModelWindow | None = None,
        price: PriceTable | None = None,
        budget: BudgetLedger | None = None,
    ) -> None:
        self._store = store
        self.window = window or ModelWindow()
        self.price = price or PriceTable()
        self._budget = budget
        self._attempts: dict[int, int] = {}
        self._cassette = store.load()
        self.replay_warnings: list[dict[str, Any]] = []
        self.hits = 0
        if self._cassette.meta.price_version and (
            self._cassette.meta.price_version != self.price.version
        ):
            # 评审 P2-12：录制时的价目表与当前不同，成本会差一个版本；
            # 这是**提示**而不是失败（成本口径本来就允许换版本重算），但必须留痕。
            self.replay_warnings.append(
                {
                    "kind": "price_version_mismatch",
                    "recorded": self._cassette.meta.price_version,
                    "current": self.price.version,
                    "note": "录制与回放的价目表版本不同：成本不可直接对比",
                }
            )

    def _next_attempt(self, step: int) -> int:
        attempt = self._attempts.get(step, 0)
        self._attempts[step] = attempt + 1
        return attempt

    def complete(self, *, step: int, view: View) -> ModelResponse:
        if self._budget is not None:
            self._budget.check(Bucket.MAIN)
        blocks = view.block_texts()
        input_tokens = sum(view.sections.values())
        if self.window.is_overflow(input_tokens):
            raise ContextOverflow(input_tokens, self.window.context_limit_tokens)

        attempt = self._next_attempt(step)
        entry = self._cassette.find(step=step, attempt=attempt)
        if entry is None:
            raise CassetteError(
                describe_missing(
                    self._store.path,
                    step=step,
                    attempt=attempt,
                    expected=request_fingerprint(blocks),
                )
            )
        fingerprint = request_fingerprint(blocks)
        if entry.request_fingerprint and entry.request_fingerprint != fingerprint:
            self.replay_warnings.append(
                {
                    "step": step,
                    "attempt": attempt,
                    "expected": entry.request_fingerprint,
                    "actual": fingerprint,
                    "note": "提示词与录制时不一致（崩溃恢复会合法改变视图），按警告记录",
                }
            )
        usage = Usage(
            input_tokens=total_input_tokens(entry.usage, entry.usage_raw),
            output_tokens=int(entry.usage.get("completion_tokens", 0)),
            cache_read_tokens=int(entry.usage.get("cache_read_tokens", 0)),
            cache_write_tokens=int(entry.usage.get("cache_write_tokens", 0)),
            estimated=False,
        )
        self.hits += 1
        response = ModelResponse(
            turn=ModelTurn(
                text=entry.text, tool_calls=list(entry.tool_calls), usage=dict(entry.usage)
            ),
            usage=usage,
            cache=CacheOutcome(
                read_tokens=usage.cache_read_tokens,
                write_tokens=usage.cache_write_tokens,
                uncached_tokens=usage.billed_input_tokens,
                common_blocks=0,
                miss_reason="replayed-from-cassette",
            ),
            view_fingerprint=view.fingerprint(),
            context_tokens=input_tokens,
            cache_config_version="replayed",
            price_version=self.price.version,
        )
        if self._budget is not None:
            response.cost_usd = self._budget.charge(bucket=Bucket.MAIN, usage=usage)
            self._budget.enforce_after_charge(Bucket.MAIN)
        elif entry.cost_usd:
            # 直接回放录制的成本：不做二次重算（录制的 canonical usage 是权威）
            response.cost_usd = entry.cost_usd
        else:
            # 录制里没有成本（或**确实为 0**）时才按当前价目表算。
            # 旧写法 `entry.cost_usd or usage.cost_usd(...)` 会把"确实 0 成本"
            # 当成缺值（评审 P2-12）。
            response.cost_usd = usage.cost_usd(self.price)
        return response
