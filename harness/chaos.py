"""崩溃注入：命名窗口 + 确定性 SIGKILL。

设计要点（评审要求"参数化的确定性注入点"，不是随机 kill）：

* 注入点是**代码中命名的窗口**，由环境变量或构造参数指定，命中即
  ``SIGKILL`` 自身进程——没有 cleanup、没有 flush 兜底，等价于 kill -9。
* 每个窗口可指定**第几次命中**才触发（``window:2``），用于"恢复后再次崩溃"
  这类需要跨进程计数的场景；计数在进程内，跨进程语义由 runner 控制。
* 崩溃前先落一个 ``crash_marker.json``（fsync），让 runner 能验证"确实死在
  这个窗口"，而不是"没跑到"。

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
        self, windows: Mapping[str, int] | None = None, marker_path: Path | None = None
    ) -> None:
        self._windows = dict(windows or {})
        self._marker_path = marker_path
        self._counts: dict[str, int] = {}

    @classmethod
    def disabled(cls) -> Chaos:
        return cls({}, None)

    @classmethod
    def from_env(cls, marker_path: Path | None = None) -> Chaos:
        return cls(parse_spec(os.environ.get("CHAOS_WINDOWS", "")), marker_path)

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

    def _write_marker(self, window: str) -> None:
        if self._marker_path is None:
            return
        self._marker_path.write_text(
            json.dumps(
                {
                    "window": window,
                    "occurrence": self._counts.get(window, 0),
                    "counts": self._counts,
                    "pid": os.getpid(),
                    "at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
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
