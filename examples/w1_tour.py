"""W1 演示：一次运维故障处置 run 的事件日志、派生状态、分叉与恢复计划。

运行：``.venv/bin/python -m examples.w1_tour``
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from harness.events import ArtifactEventType, NewEvent, Source, TreeEventType
from harness.ids import new_id
from harness.state import reduce_events
from harness.store import SqliteCheckpointSaver, SqliteStore, Write, WriteIdx


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "demo.db"
        with SqliteStore(path) as store:
            run_id, thread_id, branch_id = new_id("run"), new_id("thr"), new_id("br")
            store.create_run(run_id, thread_id=thread_id)
            store.create_branch(branch_id, run_id)

            def tree(event_type: TreeEventType, source: Source, **payload):
                return NewEvent.tree(
                    run_id=run_id, branch_id=branch_id, type=event_type,
                    source=source, payload=payload,
                )

            events = store.append_many(
                [
                    tree(TreeEventType.USER_MESSAGE, Source.USER, text="支付服务 P99 告警"),
                    tree(TreeEventType.AGENT_MESSAGE, Source.AGENT, text="先看指标与日志"),
                    tree(TreeEventType.TOOL_CALL, Source.AGENT, tool_call_id="tc_1",
                         tool="query_metrics", args={"service": "payment"}, effect="read"),
                    tree(TreeEventType.TOOL_RESULT, Source.TOOL, tool_call_id="tc_1",
                         status="executed", result={"p99_ms": 2100, "pool_wait_ms": 1800}),
                    tree(TreeEventType.AGENT_MESSAGE, Source.AGENT,
                         text="数据库连接池等待导致排队，建议扩容连接池"),
                    tree(TreeEventType.TOOL_CALL, Source.AGENT, tool_call_id="tc_2",
                         tool="scale_pool", args={"service": "payment", "size": 64},
                         effect="write_nonidempotent"),
                    tree(TreeEventType.INTERRUPT, Source.AGENT, interrupt_id="int_1",
                         reason="写操作需人工审批", request={"tool_call_id": "tc_2"}),
                ]
            )
            store.append(
                NewEvent.artifact(
                    run_id=run_id, branch_id=branch_id, type=ArtifactEventType.BUDGET_UPDATE,
                    payload={"bucket": "main", "tokens_in": 4210, "cost_usd": 0.031},
                )
            )

            state, violations = reduce_events(store.effective_events(branch_id))
            print("== 派生态 ==")
            print(f"status={state.status.value} step={state.step} "
                  f"open_tool_calls={dict(state.open_tool_calls)} "
                  f"pending_interrupt={state.pending_interrupt_id}")
            print(f"violations={[v.code for v in violations]}")
            print(f"fingerprint={state.fingerprint()[:16]}…")

            saver = SqliteCheckpointSaver(store)
            checkpoint = saver.put(
                thread_id=thread_id, branch_id=branch_id,
                channel_values={"step": state.step, "status": state.status.value},
                source="loop", step=state.step,
            )
            saver.put_writes(
                thread_id=thread_id, checkpoint_id=checkpoint.id,
                writes=[
                    Write(task_id="diagnose", idx=0, channel="node_output",
                          payload={"root_cause": "db_pool_exhaustion"}),
                    Write(task_id="scale_pool", idx=WriteIdx.INTERRUPT, channel="interrupt",
                          payload={"interrupt_id": "int_1"}),
                ],
            )
            plan = saver.recovery_plan(thread_id)
            print("\n== 恢复计划 ==")
            print(f"checkpoint={plan.checkpoint.id[:12] if plan.checkpoint else None}… "
                  f"task_outcomes={plan.task_outcomes} "
                  f"replay={[w.task_id for w in plan.replay_writes]} "
                  f"resume_from_seq={plan.resume_from_seq}")

            forked = new_id("br")
            store.create_branch(
                forked, run_id, parent_branch_id=branch_id, fork_event_id=events[-1].event_id
            )
            print("\n== 在审批点分叉（例如走“拒绝并人工处置”分支） ==")
            print(f"forked_len={len(store.effective_events(forked))} "
                  f"root_len={len(store.effective_events(branch_id))}")
            child_state, _ = reduce_events(store.effective_events(forked))
            print(f"forked_status={child_state.status.value} "
                  f"forked_pending_interrupt={child_state.pending_interrupt_id}")

            print("\n== 事件日志（前 6 条，含 kind 分离） ==")
            for event in store.effective_events(branch_id)[:6]:
                print(f"  seq={event.seq:<2} {event.kind.value:<9} {event.type:<14} "
                      f"{event.source.value}")


if __name__ == "__main__":
    main()
