"""Token 估算与版本化价格表。

估算器刻意是**确定性 + 可解释**的启发式，而不是 tiktoken：本项目的所有成本结论
都建立在"同一估算器下的相对比较"上（A/B 只改一个变量），绝对金额不是结论。
真实接入时把 ``estimate_tokens`` 换成 provider 的 tokenizer 即可，价格表随之一并版本化。

估算规则：CJK 字符 ≈ 1 token/字，其余 ≈ 1 token/4 字符；每条消息 +4（role/分隔开销）。
"""

from __future__ import annotations

import math

from pydantic import BaseModel

# 覆盖常用 CJK 区段（含中日韩统一表意文字、扩展 A、假名、全角标点）
_CJK_RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)

MESSAGE_OVERHEAD_TOKENS = 4


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """确定性估算：CJK 1 token/字，其余 1 token/4 字符（向上取整）。"""
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def estimate_message_tokens(content: str) -> int:
    return estimate_tokens(content) + MESSAGE_OVERHEAD_TOKENS


class PriceTable(BaseModel):
    """美元/千 token。cache 单价由基础单价推导，避免两处漂移。"""

    version: str = "price-v1"
    usd_per_1k_input: float = 0.003
    usd_per_1k_output: float = 0.015
    cache_write_multiplier: float = 1.25
    cache_read_multiplier: float = 0.1

    @property
    def usd_per_1k_cache_write(self) -> float:
        return self.usd_per_1k_input * self.cache_write_multiplier

    @property
    def usd_per_1k_cache_read(self) -> float:
        return self.usd_per_1k_input * self.cache_read_multiplier


class Usage(BaseModel):
    """一次模型调用的计费口径；provider 报的数字优先，估算仅用于本地对照。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated: bool = True

    @property
    def billed_input_tokens(self) -> int:
        """未命中缓存的输入 = 总输入 − 命中读 − 写入（写入另按倍率计费）。"""
        return max(0, self.input_tokens - self.cache_read_tokens - self.cache_write_tokens)

    def cost_usd(self, price: PriceTable) -> float:
        return (
            self.billed_input_tokens * price.usd_per_1k_input / 1000
            + self.cache_write_tokens * price.usd_per_1k_cache_write / 1000
            + self.cache_read_tokens * price.usd_per_1k_cache_read / 1000
            + self.output_tokens * price.usd_per_1k_output / 1000
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            estimated=self.estimated and other.estimated,
        )


def net_cost_usd(usage: Usage, price: PriceTable) -> float:
    """净成本：命中读按折扣、写入按溢价，全部计入——不是"命中率越高越省钱"。"""
    return usage.cost_usd(price)
