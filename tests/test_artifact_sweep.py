"""artifact 孤儿回收（R-B5）：引用保留 / 孤儿回收 / dry-run 无副作用 / 删后重读报错。

一个必须先说清的性质：**卸载产生的 artifact 是"派生缓存"，不是唯一副本**。
结果是先内联写进 `tool_result` 事件的，渲染层（`ViewBuilder`）在结果过大时才把它
写进内容寻址的 artifact，并在视图里给出 digest。因此：

* 引用枚举扫的是**事件 / checkpoint / checkpoint_writes 里出现过的 digest**
  （包括模型某次 `read_artifact` 调用里带的那个）；
* 没被任何 payload 提到过的 artifact，删掉它是安全的——下一次渲染会用同样的内容
  算出同样的 digest 并重新落盘（内容寻址 = 幂等）。
  这一点由 `test_deleted_offload_artifact_is_regenerated_on_next_render` 钉住：
  "回收"不等于"丢内容"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.artifacts import (
    ArtifactStore,
    ArtifactSweepError,
    referenced_digests,
    sweep,
)
from harness.context import ViewBuilder
from harness.events import Event, EventKind, Source
from harness.tools import Effect, Tool, ToolRegistry


def _event(*, payload: dict, event_id: str = "e1", event_type: str = "tool_result") -> Event:
    return Event(
        event_id=event_id,
        run_id="run",
        branch_id="br",
        seq=0,
        kind=EventKind.TREE_NODE,
        type=event_type,
        source=Source.TOOL,
        payload=payload,
        created_at=0.0,
    )


def _store_with(contents: list[str], tmp_path: Path) -> tuple[ArtifactStore, list[str]]:
    """按**正常形态**建库：artifacts 落在 run 目录下（`<run>/artifacts`）。

    共享 root 是另一条路径，由 `test_apply_refuses_a_shared_root_without_force` 覆盖——
    那条保护正是评审 P1-2 要求的。
    """
    (tmp_path / "run").mkdir(exist_ok=True)
    store = ArtifactStore(tmp_path / "run" / "artifacts")
    digests = [store.put(content).digest for content in contents]
    return store, digests


def test_referenced_digests_scans_events_checkpoints_and_writes(tmp_path: Path) -> None:
    """三类来源缺一不可：只扫事件表会把"仅被 checkpoint 引用"的对象误判成孤儿。"""
    import sqlite3

    db = tmp_path / "runtime.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE events (event_id TEXT, payload_json TEXT);
        CREATE TABLE checkpoints (state_json TEXT);
        CREATE TABLE checkpoint_writes (payload_json TEXT);
        """
    )
    con.execute("INSERT INTO events VALUES('e1', ?)", (json.dumps({"digest": "from-event"}),))
    con.execute(
        "INSERT INTO checkpoints VALUES(?)",
        (json.dumps({"nested": [{"digest": "from-checkpoint"}]}),),
    )
    con.execute(
        "INSERT INTO checkpoint_writes VALUES(?)",
        (json.dumps({"channel": {"digest": "from-writes"}}),),
    )
    con.commit()
    con.close()
    found, counts, unreadable = referenced_digests(tmp_path)
    assert {"from-event", "from-checkpoint", "from-writes"} <= found
    assert set(counts) == {
        "events.payload_json",
        "checkpoints.state_json",
        "checkpoint_writes.payload_json",
    }
    assert unreadable == 0


def test_unreadable_payload_is_counted_not_swallowed(tmp_path: Path) -> None:
    """枚举"不能删的对象"时吞掉解析失败，等于把损坏 payload 里的引用当作"没有引用"。

    这条保护落在一个**计数**上：解析失败要在报告里可见（评审 P1-2 的第二处）。
    """
    import sqlite3

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    con = sqlite3.connect(run_dir / "runtime.db")
    con.executescript(
        "CREATE TABLE events (event_id TEXT, payload_json TEXT);"
        "CREATE TABLE checkpoints (state_json TEXT);"
        "CREATE TABLE checkpoint_writes (payload_json TEXT);"
    )
    con.execute("INSERT INTO events VALUES('e1', '{ 半个 JSON')")
    con.commit()
    con.close()
    _, counts, unreadable = referenced_digests(run_dir)
    assert unreadable == 1
    assert counts["events.payload_json:unreadable"] == 1


def test_apply_refuses_a_shared_root_without_force(tmp_path: Path) -> None:
    """**P1-2 的回归用例**：共享 artifacts root 时，别的 run 的引用看不见 ⇒ 拒绝删除。

    修复前会怎样：docstring 写着"全库扫描"，实现只扫一个 run 目录，
    `--run-dir A --artifacts-root SHARED --apply` 会删掉只被 run B 引用的对象。
    """
    import sqlite3

    shared = ArtifactStore(tmp_path / "shared-artifacts")
    digest = shared.put("referenced-by-another-run").digest
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    con = sqlite3.connect(run_dir / "runtime.db")
    con.executescript(
        "CREATE TABLE events (event_id TEXT, payload_json TEXT);"
        "CREATE TABLE checkpoints (state_json TEXT);"
        "CREATE TABLE checkpoint_writes (payload_json TEXT);"
    )
    con.commit()
    con.close()

    # dry-run 允许：它不删东西，但必须如实标出"这是共享 root"
    report = sweep(shared, run_dir=run_dir, dry_run=True)
    assert report.shared_root is True
    assert report.orphans_found == 1  # 从这个 run 的视角看，它确实是孤儿
    assert shared.path_for(digest).exists()

    with pytest.raises(ArtifactSweepError) as excinfo:
        sweep(shared, run_dir=run_dir, dry_run=False)
    assert "共享库" in str(excinfo.value)
    assert shared.path_for(digest).exists(), "拒绝时绝不删除"

    # 显式 --force（allow_shared_root）才放行：决定权交给人
    forced = sweep(shared, run_dir=run_dir, dry_run=False, allow_shared_root=True)
    assert forced.deleted == [digest]


def test_dry_run_reports_but_never_deletes(tmp_path: Path) -> None:
    store, digests = _store_with(["referenced", "orphan"], tmp_path)
    db_dir = tmp_path / "run"
    db_dir.mkdir(exist_ok=True)
    import sqlite3

    con = sqlite3.connect(db_dir / "runtime.db")
    con.executescript(
        "CREATE TABLE events (event_id TEXT, payload_json TEXT);"
        "CREATE TABLE checkpoints (state_json TEXT);"
        "CREATE TABLE checkpoint_writes (payload_json TEXT);"
    )
    con.execute("INSERT INTO events VALUES('e1', ?)", (json.dumps({"digest": digests[0]}),))
    con.commit()
    con.close()

    report = sweep(store, run_dir=db_dir, dry_run=True)
    assert report.dry_run is True
    assert report.orphans_found == 1
    assert report.kept_referenced == 1
    assert report.deleted == []
    assert store.path_for(digests[1]).exists(), "dry-run 绝不删除"


def test_apply_deletes_only_orphans_and_read_fails_readably(tmp_path: Path) -> None:
    store, digests = _store_with(["keep-me", "drop-me"], tmp_path)
    db_dir = tmp_path / "run"
    db_dir.mkdir(exist_ok=True)
    import sqlite3

    con = sqlite3.connect(db_dir / "runtime.db")
    con.executescript(
        "CREATE TABLE events (event_id TEXT, payload_json TEXT);"
        "CREATE TABLE checkpoints (state_json TEXT);"
        "CREATE TABLE checkpoint_writes (payload_json TEXT);"
    )
    con.execute("INSERT INTO events VALUES('e1', ?)", (json.dumps({"digest": digests[0]}),))
    con.commit()
    con.close()

    report = sweep(store, run_dir=db_dir, dry_run=False)
    assert report.deleted == [digests[1]]
    assert store.path_for(digests[0]).exists()
    assert not store.path_for(digests[1]).exists()
    # 分类错误语义：被回收后重读给出**可读失败**（不是空串、不是旧内容）
    try:
        store.get(digests[1])
    except FileNotFoundError as exc:
        assert digests[1] in str(exc)
    else:  # pragma: no cover
        raise AssertionError("删后重读必须报错")


def test_deleted_offload_artifact_is_regenerated_on_next_render(tmp_path: Path) -> None:
    """回收不丢内容：卸载 artifact 是派生缓存，下一次渲染会重新落盘（内容寻址 ⇒ 幂等）。"""
    store = ArtifactStore(tmp_path / "artifacts")
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="query_metrics",
            effect=Effect.READ,
            fn=lambda args, key: {},
            description="读指标",
        )
    )
    builder = ViewBuilder(system_prompt="系统提示", registry=registry, artifacts=store,
                          max_inline_tokens=1)
    big_result = {"entries": [f"line-{index}" for index in range(50)]}
    # 必须带配对的 tool_call：视图的结构不变式要求调用与结果一一对应，
    # 孤立的 tool_result 会被当作违规整段丢弃（那时当然也没有卸载可言）
    call_event = _event(
        payload={"tool_call_id": "tc1", "tool": "query_metrics", "args": {}},
        event_id="e0",
        event_type="tool_call",
    )
    result_event = _event(
        payload={"tool_call_id": "tc1", "status": "executed", "result": big_result},
        event_id="e1",
    )
    builder.build(events=[call_event, result_event])
    digests = [path.name for path in store.root.rglob("*") if path.is_file()]
    assert digests, "渲染层应当把大结果卸载成 artifact"
    digest = digests[0]
    # 没人引用它 ⇒ 回收掉
    store.path_for(digest).unlink()
    assert not store.path_for(digest).exists()
    # 再渲染一次：同样的内容算出同样的 digest，重新落盘
    builder.build(events=[call_event, result_event])
    assert store.path_for(digest).exists(), "内容寻址 ⇒ 重新渲染必须重建同一份 artifact"


def test_read_artifact_call_reference_keeps_the_artifact(tmp_path: Path) -> None:
    """模型读过一次 ⇒ 它的 digest 就出现在事件 payload 里 ⇒ 从此不许回收。"""
    store, digests = _store_with(["read-once"], tmp_path)
    db_dir = tmp_path / "run"
    db_dir.mkdir(exist_ok=True)
    import sqlite3

    con = sqlite3.connect(db_dir / "runtime.db")
    con.executescript(
        "CREATE TABLE events (event_id TEXT, payload_json TEXT);"
        "CREATE TABLE checkpoints (state_json TEXT);"
        "CREATE TABLE checkpoint_writes (payload_json TEXT);"
    )
    call = {"tool_call_id": "tc9", "tool": "read_artifact", "args": {"digest": digests[0]}}
    con.execute("INSERT INTO events VALUES('e9', ?)", (json.dumps(call),))
    con.commit()
    con.close()
    report = sweep(store, run_dir=db_dir, dry_run=False)
    assert report.orphans_found == 0
    assert store.path_for(digests[0]).exists()
