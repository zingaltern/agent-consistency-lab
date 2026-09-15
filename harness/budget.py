"""预算：按桶计费、事件持久化、硬停。

参考实现（OpenHands/codex/qwen-code）给出的三条要点，本模块全部落实：

1. **唯一出口**：计费只在 LLM client 装饰器与工具结果估算两处发生，
   不允许任何地方"顺手调一次模型"而不记账。
2. **分桶**：main / compaction / judge / tools——摘要模型、判分模型的花费必须可见，
   否则"预算没超"只是主模型没超。
3. **持久化跨进程**：账本以 ``budget_update`` artifact 事件落盘，恢复后重新折叠得到，
   而不是活在内存里（崩溃就丢的预算等于没有预算）。

硬停语义：dispatch 前检查一次（避免明知超限还发请求），响应后再记账一次
（流式响应会 overshoot，事后检查不可省）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .events import ArtifactEventType, Event, NewEvent
from .store.sqlite_store import SqliteStore
from .tokens import PriceTable, Usage


class Bucket(StrEnum):
    MAIN = "main"
    COMPACTION = "compaction"
    JUDGE = "judge"
    TOOLS = "tools"


class BudgetLimits(BaseModel):
    total_usd: float = 1.0
    per_bucket: dict[str, float] = Field(default_factory=dict)

    def limit_for(self, bucket: str) -> float:
        return self.per_bucket.get(bucket, self.total_usd)


class BudgetExceeded(RuntimeError):
    def __init__(self, bucket: str, spent: float, limit: float) -> None:
        super().__init__(f"budget exceeded in {bucket}: {spent:.4f} >= {limit:.4f}")
        self.bucket = bucket
        self.spent = spent
        self.limit = limit


class BudgetSnapshot(BaseModel):
    spent_usd: float
    limit_usd: float
    per_bucket_usd: dict[str, float] = Field(default_factory=dict)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)


class BudgetLedger:
    """事件即账本：每次计费追加一条 budget_update，状态由日志折叠而来。"""

    def __init__(
        self,
        store: SqliteStore,
        *,
        run_id: str,
        branch_id: str,
        limits: BudgetLimits,
        price: PriceTable | None = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._branch_id = branch_id
        self._limits = limits
        self._price = price or PriceTable()

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    def _events(self) -> list[Event]:
        return [
            event
            for event in self._store.effective_events(self._branch_id)
            if event.type == ArtifactEventType.BUDGET_UPDATE.value
        ]

    def snapshot(self) -> BudgetSnapshot:
        per_bucket: dict[str, float] = {}
        total = 0.0
        for event in self._events():
            bucket = str(event.payload.get("bucket", Bucket.MAIN))
            cost = float(event.payload.get("cost_usd", 0.0))
            per_bucket[bucket] = per_bucket.get(bucket, 0.0) + cost
            total += cost
        return BudgetSnapshot(
            spent_usd=round(total, 6),
            limit_usd=self._limits.total_usd,
            per_bucket_usd={k: round(v, 6) for k, v in per_bucket.items()},
        )

    def spent_usd(self, bucket: str | None = None) -> float:
        snapshot = self.snapshot()
        if bucket is None:
            return snapshot.spent_usd
        return snapshot.per_bucket_usd.get(bucket, 0.0)

    def check(self, bucket: str) -> None:
        """dispatch 前的硬停检查：**总量与分桶都要过**。

        早期实现用 ``per_bucket.get(bucket, total)``，于是给 main 设了大额分桶额度
        就等于把总量上限关掉了（审计实测：total=0.001 也拦不住）。
        """
        snapshot = self.snapshot()
        if snapshot.spent_usd >= self._limits.total_usd:
            raise BudgetExceeded("total", snapshot.spent_usd, self._limits.total_usd)
        if bucket in self._limits.per_bucket:
            spent = snapshot.per_bucket_usd.get(bucket, 0.0)
            limit = self._limits.per_bucket[bucket]
            if spent >= limit:
                raise BudgetExceeded(bucket, spent, limit)

    def charge(self, *, bucket: str, usage: Usage, note: str = "") -> float:
        cost = usage.cost_usd(self._price)
        self._store.append(
            NewEvent.artifact(
                run_id=self._run_id,
                branch_id=self._branch_id,
                type=ArtifactEventType.BUDGET_UPDATE,
                payload={
                    "bucket": bucket,
                    "cost_usd": round(cost, 8),
                    "price_version": self._price.version,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "note": note,
                },
            )
        )
        return cost

    def enforce_after_charge(self, bucket: str) -> None:
        """响应后检查：流式响应可能已经 overshoot。"""
        self.check(bucket)
