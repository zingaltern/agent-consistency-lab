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

from typing import Protocol

from pydantic import BaseModel

from .budget import Bucket, BudgetLedger
from .cache import CacheConfig, CacheOutcome, PrefixCacheModel, usage_from_cache
from .context import View
from .model import Model, ModelTurn
from .tokens import PriceTable, Usage


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
        )
        if self._budget is not None:
            response.cost_usd = self._budget.charge(bucket=Bucket.MAIN, usage=usage)
            self._budget.enforce_after_charge(Bucket.MAIN)
        else:
            response.cost_usd = usage.cost_usd(self.price)
        return response
