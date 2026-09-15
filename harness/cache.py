"""前缀缓存模型：把"缓存命中"变成可计算、可复现的量。

真实供应商的 prompt cache 是**前缀匹配**：服务端缓存上一次请求的内容块序列，
本次请求与缓存的最长公共前缀部分按"读"计费，超出部分按"写"计费（溢价），
两者之间的普通 token 按原价。本模块把这个语义显式建模，而不是假装测过真实 API：

* 命中长度 = 与上一次请求的**最长公共内容块前缀**（块级比较，字符串完全相等）；
* 缓存有 TTL；过期则视为全部未命中；
* 低于最小可缓存长度的部分不产生读写费用，按普通输入计费；
* 换模型 = 换 cache key（不同模型的缓存不共享）。

因此所有"命中率/净成本"结论都是**在这个模型下**的相对比较；报告里必须带上这条边界。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pydantic import BaseModel

from .tokens import PriceTable, Usage, estimate_tokens

CACHE_CONFIG_V1 = "cache-v1"


class CacheConfig(BaseModel):
    version: str = CACHE_CONFIG_V1  # 会随每次调用写进 agent_message，便于成本归因
    min_cacheable_tokens: int = 1024
    ttl_seconds: float = 300.0
    enabled: bool = True


class CacheOutcome(BaseModel):
    read_tokens: int = 0
    write_tokens: int = 0
    uncached_tokens: int = 0
    common_blocks: int = 0
    miss_reason: str = ""


@dataclass
class _Entry:
    blocks: tuple[str, ...]
    created_at: float


class PrefixCacheModel:
    """按 cache_key（通常是模型名）隔离的缓存模型。"""

    def __init__(self, config: CacheConfig | None = None) -> None:
        self.config = config or CacheConfig()
        self._entries: dict[str, _Entry] = {}
        self.stats = {"requests": 0, "hits": 0, "misses": 0, "expired": 0}

    def fetch(
        self, *, cache_key: str, blocks: tuple[str, ...], now: float | None = None
    ) -> CacheOutcome:
        now = time.time() if now is None else now
        self.stats["requests"] += 1
        tokens = [estimate_tokens(block) for block in blocks]
        total = sum(tokens)

        if not self.config.enabled:
            return CacheOutcome(uncached_tokens=total, miss_reason="cache_disabled")

        entry = self._entries.get(cache_key)
        miss_reason = "" if entry else "cold"
        if entry is not None and now - entry.created_at > self.config.ttl_seconds:
            entry = None
            miss_reason = "ttl_expired"
            self.stats["expired"] += 1

        common = 0
        if entry is not None:
            for previous, current in zip(entry.blocks, blocks, strict=False):
                if previous != current:
                    break
                common += 1
        read_tokens = sum(tokens[:common])
        # 只有达到最小可缓存长度的前缀才会真正被缓存服务端保留
        cacheable = read_tokens >= self.config.min_cacheable_tokens or common == 0
        if common and not cacheable:
            miss_reason = "below_min_cacheable"
            common, read_tokens = 0, 0
        tail_tokens = total - read_tokens
        write_tokens = (
            tail_tokens if cacheable and tail_tokens >= self.config.min_cacheable_tokens else 0
        )
        if common:
            self.stats["hits"] += 1
        else:
            self.stats["misses"] += 1
            miss_reason = miss_reason or "prefix_changed"

        self._entries[cache_key] = _Entry(blocks=blocks, created_at=now)
        return CacheOutcome(
            read_tokens=read_tokens,
            write_tokens=write_tokens,
            uncached_tokens=tail_tokens - write_tokens,
            common_blocks=common,
            miss_reason="" if common else miss_reason,
        )


def usage_from_cache(
    *,
    outcome: CacheOutcome,
    blocks: tuple[str, ...],
    output_tokens: int,
) -> Usage:
    total_input = sum(estimate_tokens(block) for block in blocks)
    return Usage(
        input_tokens=total_input,
        output_tokens=output_tokens,
        cache_read_tokens=outcome.read_tokens,
        cache_write_tokens=outcome.write_tokens,
        estimated=True,
    )


def cache_read_ratio(usage: Usage) -> float:
    if usage.input_tokens == 0:
        return 0.0
    return usage.cache_read_tokens / usage.input_tokens


def accounting(usage: Usage, price: PriceTable) -> dict[str, float]:
    """净成本分解：普通输入 / 缓存写 / 缓存读 / 输出。"""
    return {
        "uncached_usd": usage.billed_input_tokens * price.usd_per_1k_input / 1000,
        "cache_write_usd": usage.cache_write_tokens * price.usd_per_1k_cache_write / 1000,
        "cache_read_usd": usage.cache_read_tokens * price.usd_per_1k_cache_read / 1000,
        "output_usd": usage.output_tokens * price.usd_per_1k_output / 1000,
        "total_usd": usage.cost_usd(price),
    }
