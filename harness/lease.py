"""单写者租约（R-B6）：把"谁有权写"变成一条可校验的持久事实。

范围**严格限于**语义文档已声明的方向：

* 单写者租约：持有者续租、过期失活、未持有租约的写入路径按文档口径给出**可读失败**；
* **不做**分布式协调、不做"过期接管"（那是第二步能力），`state_update` 仍是"已登记未实现"。

三条纪律：

1. **权威仍是事件日志**：租约状态 = 日志里**最后一条 `lease` 事件**的折叠物
   （`ArtifactEventType.LEASE` 的 payload 形状早已登记为 `{owner, expires_at}`），
   不新增第二张权威表。租约只是**入场条件**，不是新的状态权威。
2. **先证权、后写**：校验发生在 checkpoint 事务**之外**（进程持有 vs 事务边界，
   见模块末尾的时序说明），事务内不重复校验。
3. **不做隐式接管**：过期租约不自动转给下一个进程；后来者拿到的是可读失败，
   由运维决定是否显式 `acquire`（这一档在报告里写明"未做"）。

措辞纪律：本模块**不承诺并发安全**。它只回答"当前进程有没有写权限"，
而"两个进程同时写会不会坏"仍由外部账本裁决（语义文档 §3）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .events import ArtifactEventType, Event, NewEvent, Source
from .ids import new_id
from .store.sqlite_store import SqliteStore


class LeaseError(RuntimeError):
    """租约相关的可读失败基类。"""


class LeaseNotHeld(LeaseError):
    """未持有有效租约——调用方必须**停下来**，而不是"再试一次就好"。"""


class LeaseState(BaseModel):
    owner: str
    expires_at: float
    acquired_at: float = 0.0
    lease_id: str = ""

    def is_active(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.expires_at


@dataclass(frozen=True)
class LeaseCheck:
    ok: bool
    state: LeaseState | None
    reason: str


def fold_lease(events: list[Event]) -> LeaseState | None:
    """日志折叠：最后一条 lease 事件即当前租约状态（没有就是没有）。"""
    state: LeaseState | None = None
    for event in events:
        if event.type != ArtifactEventType.LEASE.value:
            continue
        payload = event.payload
        state = LeaseState(
            owner=str(payload.get("owner", "")),
            expires_at=float(payload.get("expires_at", 0.0)),
            acquired_at=float(payload.get("acquired_at", event.created_at)),
            lease_id=str(payload.get("lease_id", event.event_id)),
        )
    return state


class SingleWriterLease:
    """单写者租约：acquire / renew / validate 三个动作，全部以 lease 事件为权威。"""

    def __init__(self, store: SqliteStore, *, run_id: str, branch_id: str, owner: str) -> None:
        self._store = store
        self._run_id = run_id
        self._branch_id = branch_id
        self.owner = owner

    # ------------------------------------------------------------------ 读

    def current(self) -> LeaseState | None:
        return fold_lease(self._store.effective_events(self._branch_id))

    def check(self, *, now: float | None = None) -> LeaseCheck:
        """判定当前进程是否有写权限；原因是稳定标识串（进错误事件与测试断言）。"""
        state = self.current()
        moment = now if now is not None else time.time()
        if state is None:
            return LeaseCheck(False, None, "lease_not_acquired")
        if state.owner != self.owner:
            return LeaseCheck(False, state, "lease_held_by_other")
        if not state.is_active(moment):
            return LeaseCheck(False, state, "lease_expired")
        return LeaseCheck(True, state, "ok")

    def require(self, *, now: float | None = None) -> LeaseState:
        """写入路径的入口：不满足就抛**可读失败**（绝不放行、绝不静默续期）。"""
        check = self.check(now=now)
        if not check.ok:
            detail = {
                "lease_not_acquired": "尚未获取租约：先 acquire(owner, ttl_seconds)",
                "lease_held_by_other": f"租约在 {check.state.owner if check.state else '?'} 手里",
                "lease_expired": "租约已过期：过期**不会**自动接管，需要显式重新 acquire",
            }[check.reason]
            raise LeaseNotHeld(f"{check.reason}：{detail}（owner={self.owner}）")
        assert check.state is not None
        return check.state

    # ------------------------------------------------------------------ 写

    def acquire(self, *, ttl_seconds: float = 60.0) -> LeaseState:
        """获取/续租：追加一条 lease 事件（幂等语义不适用——每次都是一条新事实）。"""
        if ttl_seconds <= 0:
            raise LeaseError(f"ttl_seconds 必须为正，收到 {ttl_seconds}")
        now = time.time()
        state = LeaseState(
            owner=self.owner,
            expires_at=now + ttl_seconds,
            acquired_at=now,
            lease_id=new_id("lease"),
        )
        self._store.append(
            NewEvent.artifact(
                run_id=self._run_id,
                branch_id=self._branch_id,
                type=ArtifactEventType.LEASE,
                source=Source.SYSTEM,
                payload=state.model_dump(),
            )
        )
        return state

    def renew(self, *, ttl_seconds: float = 60.0) -> LeaseState:
        """续租：语义上等价于 acquire（追加新事实），但要求当前确实持有且未过期。"""
        self.require()
        return self.acquire(ttl_seconds=ttl_seconds)


def guarded_write(
    lease: SingleWriterLease,
    *,
    write: Any,
    now: float | None = None,
) -> Any:
    """先证权、后写的薄封装（示例与测试共用；调用方也可以用 ``require`` 自己串）。

    时序（写在语义文档里）：

    ```
    进程持有（事件日志折叠）                  checkpoint 事务
    ─────────────────────────────────────────────────────────
    lease.require()  ← 只读日志，不占事务
        │ ok
        └──────────────────────────────────▶ BEGIN IMMEDIATE … COMMIT
    ```

    校验**不放进事务**：租约是"入场条件"，不是事务的一部分；把两者缠在一起会让
    "租约事件本身也要写日志"变成自指（写租约要持租约？），那是死循环而不是严谨。
    """
    lease.require(now=now)
    return write()
