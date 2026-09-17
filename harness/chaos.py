"""崩溃注入：命名窗口 + 确定性 SIGKILL。

设计要点（评审要求"参数化的确定性注入点"，不是随机 kill）：

* 注入点是**代码中命名的窗口**，由环境变量或构造参数指定，命中即
  ``SIGKILL`` 自身进程——没有 cleanup、没有 flush 兜底，等价于 kill -9。
* 每个窗口可指定**第几次命中**才触发（``window:2``），用于"恢复后再次崩溃"
  这类需要跨进程计数的场景；计数在进程内，跨进程语义由 runner 控制。
* 崩溃前先落一个 ``crash_marker.json``（fsync），让 runner 能验证"确实死在
  这个窗口"，而不是"没跑到"。
* **注入族**（R-A2 新增第二族）：命名窗口是"第 N 次命中某代码位置"，与时间无关；
  ``kill_after_ms`` 是**墙钟定时**注入，与代码位置无关。两者语义独立、可同时给出
  （窗口先命中就先死），marker 里用 ``injection_kind`` 区分，便于排查时知道
  "这次死在哪一类注入上"。

窗口目录（W2 实现前三个，W3 补后三个）：

1. ``pre_tool_exec``：模型响应后 / 工具执行前
2. ``post_tool_effect_pre_record``：工具成功后 / 记录前
3. ``post_record_pre_commit``：记录后 / checkpoint 提交前
4. ``post_approval_pre_exec``：（W3）
5. ``during_compaction``：（W4）
6. ``after_resume``：（W3/W2 由 runner 组合实现）
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Mapping
from pathlib import Path

WINDOWS: tuple[str, ...] = (
    "pre_tool_exec",
    "post_tool_effect_pre_record",
    "post_record_pre_commit",
    "post_approval_pre_exec",
    "during_compaction",
    "after_resume",
)


class Chaos:
    def __init__(
        self,
        windows: Mapping[str, int] | None = None,
        marker_path: Path | None = None,
        kill_after_ms: int | None = None,
    ) -> None:
        self._windows = dict(windows or {})
        self._marker_path = marker_path
        self._counts: dict[str, int] = {}
        self._kill_after_ms = kill_after_ms
        self._timer: threading.Timer | None = None
        if kill_after_ms is not None:
            if kill_after_ms <= 0:
                raise ValueError(f"kill_after_ms 必须为正，收到 {kill_after_ms}")
            self._start_timer(kill_after_ms)

    @classmethod
    def disabled(cls) -> Chaos:
        return cls({}, None)

    @classmethod
    def from_env(
        cls, marker_path: Path | None = None, kill_after_ms: int | None = None
    ) -> Chaos:
        return cls(parse_spec(os.environ.get("CHAOS_WINDOWS", "")), marker_path, kill_after_ms)

    def _start_timer(self, kill_after_ms: int) -> None:
        """墙钟定时注入：到点即 SIGKILL 自身进程（真 kill -9，没有 cleanup）。

        用 daemon 线程而不是 signal.alarm：SIGALRM 会打断系统调用且只支持整秒，
        而这里要的是"任意时刻的 kill -9"，与命名窗口的注入强度完全一致。
        """
        self._timer = threading.Timer(kill_after_ms / 1000.0, self._time_hit)
        self._timer.daemon = True
        self._timer.start()

    def _time_hit(self) -> None:
        self._write_marker(
            "time_hit", injection_kind="time_hit", kill_after_ms=self._kill_after_ms
        )
        os.kill(os.getpid(), signal.SIGKILL)

    @property
    def windows(self) -> dict[str, int]:
        return dict(self._windows)

    def armed(self, window: str) -> bool:
        return window in self._windows

    def hit(self, window: str) -> None:
        """命中窗口则杀死当前进程；否则只计数。"""
        self._counts[window] = self._counts.get(window, 0) + 1
        target = self._windows.get(window)
        if target is None or self._counts[window] != target:
            return
        self._write_marker(window)
        os.kill(os.getpid(), signal.SIGKILL)

    def _write_marker(
        self,
        window: str,
        *,
        injection_kind: str = "window",
        kill_after_ms: int | None = None,
    ) -> None:
        if self._marker_path is None:
            return
        payload: dict[str, object] = {
            # injection_kind 与 kill_after_ms 是新增字段；既有字段（window/counts/...）
            # 语义不变，老 runner 读 marker 的方式不受影响。
            "injection_kind": injection_kind,
            "window": window,
            "occurrence": self._counts.get(window, 0),
            "counts": self._counts,
            "pid": os.getpid(),
            "at": time.time(),
        }
        if kill_after_ms is not None:
            payload["kill_after_ms"] = kill_after_ms
        self._marker_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        fd = os.open(self._marker_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def parse_spec(spec: str) -> dict[str, int]:
    """解析 ``"pre_tool_exec,post_record_pre_commit:2"`` 形式的窗口规格。"""
    windows: dict[str, int] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, occurrence = chunk.partition(":")
        if name not in WINDOWS:
            raise ValueError(f"unknown chaos window: {name!r}; known: {list(WINDOWS)}")
        windows[name] = int(occurrence) if occurrence else 1
    return windows
