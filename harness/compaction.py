"""压缩：以事件追加的方式缩短上下文，而不是改写历史。

两档（对齐真实实现的层级）：

* **微压缩 / 卸载**（无模型调用）：视图渲染时把超阈值的历史工具结果替换成
  artifact 引用——这一步发生在渲染层，日志不变。
* **摘要压缩**（有模型调用）：把最老的一批 turn group 换成一段摘要，
  以 ``compaction`` artifact 事件追加到日志尾部；被替换的事件**仍然在日志里**，
  只是不再进入视图。

三条纪律：

1. **摘要放尾部**：历史块保持 append-only，摘要追加在历史之后。删中间一定会击穿
   缓存前缀，所以压缩的代价是"一次性 miss"，把摘要放尾部能让**新的前缀立刻稳定**。
2. **原子性**：先写 artifact、再追加事件。崩溃要么什么都没发生，要么压缩完整生效；
   孤儿 artifact 可被 GC，绝不会出现"引用了不存在的 artifact"。
3. **确定性 id**：``compaction_id = sha256(被替换事件集合)``。崩溃后重算得到同一个 id，
   因此"重复压缩"可被检测并跳过。

摘要调用本身也要花 token：它走 ``bucket=compaction`` 计费——不记账的压缩会掩盖真实成本。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence

from pydantic import BaseModel, Field

from .artifacts import ArtifactStore
from .budget import Bucket, BudgetLedger
from .cache import CacheConfig
from .chaos import Chaos
from .context import View
from .events import ArtifactEventType, Event, EventKind, NewEvent, TreeEventType
from .llm import ModelWindow
from .store.sqlite_store import SqliteStore
from .tokens import Usage, estimate_tokens

GROUP_START_TYPES = (TreeEventType.AGENT_MESSAGE.value, TreeEventType.USER_MESSAGE.value)


class CompactionPolicy(BaseModel):
    trigger_fraction: float = 0.85
    keep_recent_groups: int = 2
    summary_token_ratio: float = 0.2


class CompactionRecord(BaseModel):
    compaction_id: str
    replaces_event_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    artifact_ref: dict = Field(default_factory=dict)
    tokens_before: int = 0
    tokens_after: int = 0
    reason: str = ""
    round: int = 0


Summarizer = Callable[[Sequence[Event]], tuple[str, Usage]]


def iter_groups(events: Sequence[Event]) -> list[list[Event]]:
    """按"一个模型轮次"分组：agent_message 开启新组，其后的 tool/result 归入本组。"""
    groups: list[list[Event]] = []
    for event in events:
        if not event.is_tree_node:
            continue
        if event.type in GROUP_START_TYPES or not groups:
            groups.append([event])
        else:
            groups[-1].append(event)
    return groups


def deterministic_summary(replaced: Sequence[Event]) -> tuple[str, Usage]:
    """确定性摘要桩：真实实现应调用模型；这里保证可复现，并把花销按同价目表记账。"""
    tools: dict[str, int] = {}
    last_agent_text = ""
    for event in replaced:
        if event.type == TreeEventType.TOOL_CALL.value:
            name = str(event.payload.get("tool"))
            tools[name] = tools.get(name, 0) + 1
        elif event.type == TreeEventType.AGENT_MESSAGE.value:
            last_agent_text = str(event.payload.get("text", ""))
    tool_stats = ", ".join(f"{name}×{count}" for name, count in sorted(tools.items())) or "无"
    summary = (
        f"已压缩 {len(replaced)} 个历史事件；工具调用统计：{tool_stats}。"
        f"最近一次结论：{last_agent_text[:120]}"
    )
    input_tokens = sum(estimate_tokens(str(event.payload)) for event in replaced)
    return summary, Usage(
        input_tokens=input_tokens,
        output_tokens=estimate_tokens(summary),
        estimated=True,
    )


class Compactor:
    def __init__(
        self,
        store: SqliteStore,
        *,
        artifacts: ArtifactStore,
        policy: CompactionPolicy | None = None,
        summarizer: Summarizer | None = None,
        chaos: Chaos | None = None,
        budget: BudgetLedger | None = None,
    ) -> None:
        self._store = store
        self._artifacts = artifacts
        self.policy = policy or CompactionPolicy()
        self._summarize = summarizer or deterministic_summary
        self._chaos = chaos or Chaos.disabled()
        self._budget = budget

    # ------------------------------------------------------------------ 触发

    def should_compact(self, *, view: View, window: ModelWindow) -> bool:
        ratio = window.usage_ratio(view.total_tokens)
        buffered = (
            view.total_tokens + window.compaction_buffer_tokens
        ) > window.usable_input_tokens
        return ratio >= self.policy.trigger_fraction or buffered

    # ------------------------------------------------------------------ 执行

    def existing_rounds(self, events: Sequence[Event]) -> int:
        return sum(1 for event in events if event.type == ArtifactEventType.COMPACTION.value)

    def compact(
        self, *, run_id: str, branch_id: str, events: Sequence[Event], reason: str
    ) -> CompactionRecord | None:
        already_replaced: set[str] = set()
        for event in events:
            if event.type == ArtifactEventType.COMPACTION.value:
                already_replaced.update(event.payload.get("replaces_event_ids", []))

        groups = [
            group
            for group in iter_groups(events)
            if all(item.event_id not in already_replaced for item in group)
        ]
        if len(groups) <= self.policy.keep_recent_groups + 1:
            return None

        replaceable = groups[1:]  # 首组（用户任务）永远保留
        victims = replaceable[: max(0, len(replaceable) - self.policy.keep_recent_groups)]
        victim_ids = [item.event_id for group in victims for item in group]
        if not victim_ids:
            return None

        tokens_before = sum(
            estimate_tokens(str(item.payload)) for group in groups for item in group
        )
        summary, usage = self._summarize([item for group in victims for item in group])
        ref = self._artifacts.put_json(
            {
                "replaces_event_ids": victim_ids,
                "summary": summary,
                "events": [
                    {"type": item.type, "payload": item.payload}
                    for group in victims
                    for item in group
                ],
            }
        )
        if self._budget is not None:
            self._budget.charge(bucket=Bucket.COMPACTION, usage=usage, note="summarize")

        # 窗口 5：artifact 已落盘、compaction 事件尚未追加
        self._chaos.hit("during_compaction")

        compaction_id = (
            "cmp_" + hashlib.sha256("\x1f".join(sorted(victim_ids)).encode()).hexdigest()[:16]
        )
        tokens_after = estimate_tokens(summary)
        record = CompactionRecord(
            compaction_id=compaction_id,
            replaces_event_ids=victim_ids,
            summary=summary,
            artifact_ref=ref.model_dump(),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            reason=reason,
            round=self.existing_rounds(events) + 1,
        )
        self._store.append(
            NewEvent.artifact(
                run_id=run_id,
                branch_id=branch_id,
                type=ArtifactEventType.COMPACTION,
                payload=record.model_dump(),
            )
        )
        return record

    def already_compacted(self, events: Sequence[Event], record: CompactionRecord) -> bool:
        return any(
            event.type == ArtifactEventType.COMPACTION.value
            and event.payload.get("compaction_id") == record.compaction_id
            for event in events
        )


def compaction_events(events: Sequence[Event]) -> list[Event]:
    return [e for e in events if e.type == ArtifactEventType.COMPACTION.value]


def replaced_ids(events: Sequence[Event]) -> set[str]:
    out: set[str] = set()
    for event in compaction_events(events):
        out.update(event.payload.get("replaces_event_ids", []))
    return out


def view_tokens_of(events: Sequence[Event]) -> int:
    """粗略对照：所有树节点的 token 量（不减去已压缩部分）。"""
    return sum(
        estimate_tokens(str(event.payload)) for event in events if event.kind is EventKind.TREE_NODE
    )


__all__ = [
    "CacheConfig",
    "CompactionPolicy",
    "CompactionRecord",
    "Compactor",
    "compaction_events",
    "deterministic_summary",
    "iter_groups",
    "replaced_ids",
    "view_tokens_of",
]
