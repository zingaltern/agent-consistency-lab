"""缓存模型、预算账本、artifact 存储与压缩。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.artifacts import ArtifactStore, make_read_artifact_tool
from harness.budget import Bucket, BudgetExceeded, BudgetLedger, BudgetLimits
from harness.cache import CacheConfig, PrefixCacheModel, accounting, cache_read_ratio
from harness.compaction import CompactionPolicy, Compactor, deterministic_summary, iter_groups
from harness.events import NewEvent, Source, TreeEventType
from harness.store import SqliteStore
from harness.tokens import PriceTable, Usage, estimate_tokens

from .conftest import RunCtx


def _blocks(count: int, tokens_each: int) -> tuple[str, ...]:
    return tuple("x" * (tokens_each * 4) for _ in range(count))


# ------------------------------------------------------------------ token/价格


def test_estimator_counts_cjk_and_ascii() -> None:
    assert estimate_tokens("支付服务告警") == 6  # 每字 1 token
    assert estimate_tokens("a" * 40) == 10  # 4 字符 1 token


def test_usage_cost_accounting() -> None:
    price = PriceTable()
    usage = Usage(
        input_tokens=10_000, output_tokens=1_000, cache_read_tokens=6_000, cache_write_tokens=2_000
    )
    assert usage.billed_input_tokens == 2_000
    parts = accounting(usage, price)
    assert parts["total_usd"] == pytest.approx(usage.cost_usd(price))
    assert parts["cache_read_usd"] < parts["uncached_usd"]  # 读更便宜
    assert cache_read_ratio(usage) == pytest.approx(0.6)


# ---------------------------------------------------------------------- 缓存


def test_cold_call_has_no_reads() -> None:
    cache = PrefixCacheModel(CacheConfig(min_cacheable_tokens=10))
    outcome = cache.fetch(cache_key="m", blocks=_blocks(3, 20))
    assert outcome.read_tokens == 0
    assert outcome.write_tokens > 0  # 首次请求把前缀写进缓存


def test_repeat_call_reads_common_prefix() -> None:
    cache = PrefixCacheModel(CacheConfig(min_cacheable_tokens=10))
    blocks = _blocks(3, 20)
    cache.fetch(cache_key="m", blocks=blocks)
    outcome = cache.fetch(cache_key="m", blocks=blocks)
    assert outcome.common_blocks == 3
    assert outcome.read_tokens == sum(estimate_tokens(b) for b in blocks)


def test_changed_tail_keeps_prefix_hit() -> None:
    cache = PrefixCacheModel(CacheConfig(min_cacheable_tokens=10))
    original = _blocks(4, 20)
    cache.fetch(cache_key="m", blocks=original)
    mutated = (*original[:3], "y" * 80)
    outcome = cache.fetch(cache_key="m", blocks=mutated)
    assert outcome.common_blocks == 3
    assert outcome.read_tokens == sum(estimate_tokens(b) for b in original[:3])


def test_changed_head_kills_all_hits() -> None:
    """把动态信息放到前缀前面 = 每步全量未命中。"""
    cache = PrefixCacheModel(CacheConfig(min_cacheable_tokens=10))
    original = _blocks(4, 20)
    cache.fetch(cache_key="m", blocks=original)
    outcome = cache.fetch(cache_key="m", blocks=(original[0] + "!", *original[1:]))
    assert outcome.common_blocks == 0
    assert outcome.read_tokens == 0


def test_ttl_expiry_forces_miss() -> None:
    cache = PrefixCacheModel(CacheConfig(min_cacheable_tokens=10, ttl_seconds=0.0))
    blocks = _blocks(2, 20)
    cache.fetch(cache_key="m", blocks=blocks, now=1000.0)
    outcome = cache.fetch(cache_key="m", blocks=blocks, now=1001.0)
    assert outcome.read_tokens == 0
    assert outcome.miss_reason == "ttl_expired"


def test_cache_keys_are_isolated_per_model() -> None:
    cache = PrefixCacheModel(CacheConfig(min_cacheable_tokens=10))
    blocks = _blocks(2, 20)
    cache.fetch(cache_key="model-a", blocks=blocks)
    assert cache.fetch(cache_key="model-b", blocks=blocks).read_tokens == 0


def test_below_min_cacheable_produces_no_read_or_write() -> None:
    cache = PrefixCacheModel(CacheConfig(min_cacheable_tokens=10_000))
    blocks = _blocks(2, 5)
    outcome = cache.fetch(cache_key="m", blocks=blocks)
    assert outcome.read_tokens == 0 and outcome.write_tokens == 0
    assert outcome.uncached_tokens == sum(estimate_tokens(b) for b in blocks)


# ---------------------------------------------------------------------- 预算


def test_budget_is_persisted_as_events(store: SqliteStore, ctx: RunCtx) -> None:
    ledger = BudgetLedger(
        store, run_id=ctx.run_id, branch_id=ctx.branch_id, limits=BudgetLimits(total_usd=1.0)
    )
    ledger.charge(bucket=Bucket.MAIN, usage=Usage(input_tokens=1000, output_tokens=0))
    ledger.charge(bucket=Bucket.COMPACTION, usage=Usage(input_tokens=1000, output_tokens=0))
    snapshot = ledger.snapshot()
    assert snapshot.spent_usd > 0
    assert set(snapshot.per_bucket_usd) == {"main", "compaction"}
    # 新实例（等价于进程重启）从事件重建，数字必须一致
    rebuilt = BudgetLedger(
        store, run_id=ctx.run_id, branch_id=ctx.branch_id, limits=BudgetLimits(total_usd=1.0)
    )
    assert rebuilt.snapshot().spent_usd == snapshot.spent_usd


def test_budget_hard_stop(store: SqliteStore, ctx: RunCtx) -> None:
    ledger = BudgetLedger(
        store, run_id=ctx.run_id, branch_id=ctx.branch_id, limits=BudgetLimits(total_usd=0.01)
    )
    ledger.charge(bucket=Bucket.MAIN, usage=Usage(input_tokens=10_000, output_tokens=0))
    with pytest.raises(BudgetExceeded):
        ledger.check(Bucket.MAIN)


def test_budget_per_bucket_limit(store: SqliteStore, ctx: RunCtx) -> None:
    ledger = BudgetLedger(
        store,
        run_id=ctx.run_id,
        branch_id=ctx.branch_id,
        limits=BudgetLimits(total_usd=10.0, per_bucket={"compaction": 0.0001}),
    )
    ledger.charge(bucket=Bucket.COMPACTION, usage=Usage(input_tokens=10_000, output_tokens=0))
    with pytest.raises(BudgetExceeded):
        ledger.check(Bucket.COMPACTION)
    ledger.check(Bucket.MAIN)  # 其它桶不受影响


# ------------------------------------------------------------------ artifacts


def test_artifact_content_addressing(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "a")
    first = store.put("hello")
    second = store.put("hello")
    assert first.digest == second.digest
    assert store.get(first.digest) == "hello"
    assert list((tmp_path / "a").rglob("*")) != []


def test_artifact_slice_and_tool(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "a")
    ref = store.put("0123456789" * 10)
    tool = make_read_artifact_tool(store)
    result = tool.fn({"digest": ref.digest, "offset": 5, "limit": 10}, "k")
    assert result["content"] == "5678901234"
    assert result["truncated"] is True
    assert result["total_chars"] == 100


def test_artifact_orphan_count(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "a")
    ref = store.put("orphan-check")
    assert store.orphan_count({ref.digest}) == 0
    assert store.orphan_count(set()) == 1


# --------------------------------------------------------------------- 压缩


def _seed_groups(store: SqliteStore, ctx: RunCtx, count: int) -> list:
    for index in range(count):
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.AGENT_MESSAGE,
                source=Source.AGENT,
                payload={"text": f"第 {index} 轮", "final": False},
            )
        )
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_CALL,
                source=Source.AGENT,
                payload={"tool_call_id": f"c{index}", "tool": "fetch_logs", "args": {"i": index}},
            )
        )
        store.append(
            NewEvent.tree(
                run_id=ctx.run_id,
                branch_id=ctx.branch_id,
                type=TreeEventType.TOOL_RESULT,
                source=Source.TOOL,
                payload={"tool_call_id": f"c{index}", "status": "executed", "result": {"i": index}},
            )
        )
    return store.effective_events(ctx.branch_id)


def test_iter_groups_groups_by_model_turn(store: SqliteStore, ctx: RunCtx) -> None:
    events = _seed_groups(store, ctx, 3)
    groups = iter_groups(events)
    assert len(groups) == 3
    assert all(len(group) == 3 for group in groups)


def test_compaction_replaces_oldest_groups_and_is_deterministic(
    store: SqliteStore, ctx: RunCtx, tmp_path: Path
) -> None:
    artifacts = ArtifactStore(tmp_path / "a")
    events = _seed_groups(store, ctx, 5)
    compactor = Compactor(store, artifacts=artifacts, policy=CompactionPolicy(keep_recent_groups=2))
    record = compactor.compact(
        run_id=ctx.run_id, branch_id=ctx.branch_id, events=events, reason="t"
    )
    assert record is not None
    # 5 组里保留首组（用户任务）+ 最近 2 组 ⇒ 替换 2 组 × 3 事件
    assert len(record.replaces_event_ids) == 6
    assert artifacts.get(record.artifact_ref["digest"])
    # 再次压缩：剩余未替换组数已不足（3 组 = keep(2) + 1），返回 None 而不是重复压缩
    again = Compactor(store, artifacts=artifacts, policy=CompactionPolicy(keep_recent_groups=2))
    assert (
        again.compact(
            run_id=ctx.run_id,
            branch_id=ctx.branch_id,
            events=store.effective_events(ctx.branch_id),
            reason="t",
        )
        is None
    )


def test_compaction_skips_when_few_groups(store: SqliteStore, ctx: RunCtx, tmp_path: Path) -> None:
    compactor = Compactor(store, artifacts=ArtifactStore(tmp_path / "a"))
    events = _seed_groups(store, ctx, 2)
    assert (
        compactor.compact(run_id=ctx.run_id, branch_id=ctx.branch_id, events=events, reason="t")
        is None
    )


def test_summary_is_deterministic(store: SqliteStore, ctx: RunCtx) -> None:
    events = _seed_groups(store, ctx, 2)
    first, usage_a = deterministic_summary(events)
    second, usage_b = deterministic_summary(events)
    assert first == second and usage_a.input_tokens == usage_b.input_tokens
    assert "fetch_logs" in first


def test_should_compact_respects_thresholds(
    store: SqliteStore, ctx: RunCtx, tmp_path: Path
) -> None:
    from harness.context import Block, View
    from harness.llm import ModelWindow

    compactor = Compactor(store, artifacts=ArtifactStore(tmp_path / "a"))
    window = ModelWindow(
        context_limit_tokens=1000, max_output_tokens=100, compaction_buffer_tokens=0
    )
    small = View(
        blocks=(Block(role="system", section="prefix", content="x", tokens=100),),
        sections={"prefix": 100},
        total_tokens=100,
    )
    large = View(blocks=(), sections={"history": 950}, total_tokens=950)
    assert not compactor.should_compact(view=small, window=window)  # 11% < 85%
    assert compactor.should_compact(view=large, window=window)  # 105% ≥ 85%


def test_should_compact_triggers_on_buffer(store: SqliteStore, ctx: RunCtx, tmp_path: Path) -> None:
    """还没到比例阈值，但加上预留余量会撞墙 → 提前压缩。"""
    from harness.context import Block, View
    from harness.llm import ModelWindow

    compactor = Compactor(store, artifacts=ArtifactStore(tmp_path / "a"))
    window = ModelWindow(
        context_limit_tokens=1000, max_output_tokens=100, compaction_buffer_tokens=800
    )
    view = View(
        blocks=(Block(role="system", section="prefix", content="x", tokens=200),),
        sections={"prefix": 200},
        total_tokens=200,
    )
    assert compactor.should_compact(view=view, window=window)
