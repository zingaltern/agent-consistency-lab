"""ID 生成：可用时用 uuid7（时间有序），否则回退 uuid4。

时序只是**排查便利**，不承担任何排序职责——事件顺序由 ``(branch_id, seq)`` 保证，
checkpoint 顺序由 ``created_at + rowid`` 保证。因此 uuid7 不可用（Python < 3.14）
时回退到 uuid4 是安全的，不需要改语义。
"""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    generator = getattr(uuid, "uuid7", None)
    value = generator() if generator is not None else uuid.uuid4()
    return f"{prefix}_{value.hex}"
