"""录制物（cassette）：真实模型接入的**事实数据源**（R-B1）。

三条设计约束：

1. **录制必须能驱动工具循环**：loop 靠 ``tool_calls`` 前进，因此每条录制含
   ``text`` + ``tool_calls``（含 ``tool_call_id`` / ``tool`` / ``args``）+ ``usage`` 三段。
   只录 ``messages → (text, usage)`` 的实现接不了工具循环。
2. **usage 归一为 canonical 四段**：``prompt / completion / cache_read / cache_write``。
   各 SDK 的字段名不同，映射规则在这里写死并配单测；**原始 usage json 一并落盘**，
   便于日后核对映射是否漏字段。价格表只消费 canonical 段 + ``price_version``。
3. **录制是权威**：replay **不做二次重算**（真实服务端缓存账单无法本地复算；
   `harness/cache.py` 的 PrefixCacheModel 是对视图块的估算口径，两者不能混）。

命中键（见开放问题裁决 §五）：``(step, attempt)``。
``step`` 由事件日志驱动（= 已落盘的 ``agent_message`` 数），``attempt`` 是同一 step
在**本进程内**的第几次调用（上下文溢出重试会产生 attempt=1）。这个键与"跨进程全局序号"
在顺序上等价，且**崩溃-恢复后不漂移**：被崩溃打断的那次调用没有写 agent_message，
恢复后重发拿到同一个 (step, attempt)，于是 replay 能命中它。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .tools import ToolCallRequest, canonical_json

SCHEMA_VERSION = "cassette/v1"
CASSETTE_FILENAME = "cassette.json"

CANONICAL_USAGE_KEYS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


class CassetteError(RuntimeError):
    """录制物缺失/损坏/对不上——一律给出**可读**失败，绝不静默降级。"""


class CassetteMeta(BaseModel):
    """provenance：谁、什么时候、用什么参数录的（回放模式的结论必须能追溯到它）。"""

    schema_version: str = SCHEMA_VERSION
    model: str = ""
    provider: str = ""
    temperature: float = 0.0
    seed: int | None = None
    created_at: float = Field(default_factory=time.time)
    price_version: str = ""
    note: str = ""


class RecordedTurn(BaseModel):
    """一条录制：驱动一次模型响应的全部信息。"""

    step: int
    attempt: int = 0
    text: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)  # canonical 四段
    usage_raw: dict[str, Any] = Field(default_factory=dict)  # 原始 SDK 字段
    request_fingerprint: str = ""
    cost_usd: float = 0.0
    recorded_at: float = 0.0  # 只用于排查；一致性比较时**不比对**时间戳类字段


class Cassette(BaseModel):
    meta: CassetteMeta = Field(default_factory=CassetteMeta)
    entries: list[RecordedTurn] = Field(default_factory=list)

    def find(self, *, step: int, attempt: int) -> RecordedTurn | None:
        for entry in self.entries:
            if entry.step == step and entry.attempt == attempt:
                return entry
        return None


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    """把各家 SDK 的 usage 归一成 canonical 四段。

    映射规则（写死，配单测；发现新字段就往这里加，不允许在别处"顺手映射"）：

    * OpenAI 形态：``prompt_tokens`` / ``completion_tokens`` /
      ``prompt_tokens_details.cached_tokens`` → cache_read；OpenAI 不报 cache_write，
      且它的 ``prompt_tokens`` **已包含**缓存命中的部分。
    * Anthropic 形态：``input_tokens`` / ``output_tokens`` / ``cache_read_input_tokens`` /
      ``cache_creation_input_tokens``；它的 ``input_tokens`` **不含**缓存部分。

    两家的口径差异体现在 canonical 段里保留各自的原始语义（``prompt_tokens`` 是否含
    cache_read 由 provider 字段决定），因此**下游对账必须看 provider**——这条写在
    ``CassetteMeta.provider`` 里。
    """
    raw = raw or {}
    if "prompt_tokens" in raw or "completion_tokens" in raw:
        details = raw.get("prompt_tokens_details") or {}
        return {
            "prompt_tokens": int(raw.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(raw.get("completion_tokens", 0) or 0),
            "cache_read_tokens": int(details.get("cached_tokens", 0) or 0),
            "cache_write_tokens": 0,  # OpenAI 形态不报写入侧
        }
    if "input_tokens" in raw or "output_tokens" in raw:
        return {
            "prompt_tokens": int(raw.get("input_tokens", 0) or 0),
            "completion_tokens": int(raw.get("output_tokens", 0) or 0),
            "cache_read_tokens": int(raw.get("cache_read_input_tokens", 0) or 0),
            "cache_write_tokens": int(raw.get("cache_creation_input_tokens", 0) or 0),
        }
    raise CassetteError(
        "usage 无法归一：既没有 prompt_tokens/completion_tokens（OpenAI 形态），"
        f"也没有 input_tokens/output_tokens（Anthropic 形态）；实际字段：{sorted(raw)}"
    )


def prompt_includes_cache(raw: dict[str, Any] | None) -> bool:
    """``prompt_tokens`` 是否**已包含**缓存部分（决定 Input 总token 怎么算）。

    * OpenAI 形态：``prompt_tokens`` 含缓存命中 ⇒ True；
    * Anthropic 形态：``input_tokens`` 是**未命中缓存**的那部分 ⇒ False
      （总输入 = input_tokens + cache_read + cache_write）。

    这条差异会让"同一份账单"在两种口径下差一个缓存量级，所以它必须显式化、
    并且随 canonical usage 一起落盘——否则对账时会把口径差异读成行为差异。
    """
    raw = raw or {}
    return "prompt_tokens" in raw or "completion_tokens" in raw


def request_fingerprint(blocks: tuple[str, ...] | list[str]) -> str:
    """请求的归一化指纹：对"块序列"做规范化哈希（与视图指纹同源的确定性口径）。

    只用于**警告级**校验（提示词变了但序号没变），不参与命中键——见模块 docstring。
    """
    return hashlib.sha256(canonical_json(list(blocks)).encode("utf-8")).hexdigest()


class CassetteStore:
    """录制目录的读写：一个目录一份 cassette，追加写 + 原子替换。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / CASSETTE_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Cassette:
        if not self.path.exists():
            raise CassetteError(
                f"录制物不存在：{self.path}（record 模式用 --record-dir 指定目录）"
            )
        try:
            return Cassette.model_validate(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise CassetteError(f"录制物无法解析（{self.path}）：{exc}") from exc

    def save(self, cassette: Cassette) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(cassette.model_dump(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)  # 原子替换：读者永远看不到半份录制

    def append(self, turn: RecordedTurn, *, meta: CassetteMeta | None = None) -> Cassette:
        cassette = self.load() if self.exists() else Cassette(meta=meta or CassetteMeta())
        if meta is not None:
            cassette.meta = meta
        cassette.entries = [
            entry
            for entry in cassette.entries
            if not (entry.step == turn.step and entry.attempt == turn.attempt)
        ]
        cassette.entries.append(turn)
        cassette.entries.sort(key=lambda entry: (entry.step, entry.attempt))
        self.save(cassette)
        return cassette


def describe_missing(cassette_path: Path, *, step: int, attempt: int, expected: str) -> str:
    """miss 的报错文本：说明**缺哪份录制、期望的提示词长什么样**（可读失败）。"""
    return (
        f"replay 未命中：{cassette_path} 里没有 (step={step}, attempt={attempt}) 的录制。"
        f"期望的请求指纹（归一化提示词）为 {expected[:16]}…；"
        "常见原因：录制时这条路径没跑到（例如崩溃位置不同）、或录制目录指错了。"
        "要补录制：用 record 模式重跑同一条轨迹。"
    )
