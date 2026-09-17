"""SQLite 快照读取：**必须**连 ``-wal``/``-shm`` 一起拷（纪律的唯一实现）。

为什么单独一个模块：这条纪律原先有三份实现（`harness/audit_chain.py`、
`opsenv/oracle.py`、`scripts/probe_tornwrite.py`）。只要其中一份漂移（例如忘了 `-wal`），
"效果发生了几次"的判定就会**整个反转**——进程被 SIGKILL 后，最后一次已提交的效果
可能只存在于 WAL 里，只拷主库会把"已发生"读成"没发生"（外部审计实测踩过这个坑）。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

SUFFIXES: tuple[str, ...] = ("", "-wal", "-shm")


def snapshot_db(path: str | Path) -> Path:
    """把 ``path``（连同 ``-wal``/``-shm``）拷进临时目录，返回快照里的主库路径。"""
    source = Path(path)
    target = Path(tempfile.mkdtemp(prefix="db-snapshot-"))
    for suffix in SUFFIXES:
        candidate = Path(str(source) + suffix)
        if candidate.exists():
            shutil.copy2(candidate, target / (source.name + suffix))
    return target / source.name
