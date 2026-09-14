"""ID 生成：优先 uuid7（时间有序），回退 uuid4。

日志排查与 checkpoint 排序都依赖 ID 的时间局部性，因此优先 uuid7。
"""

from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    generator = getattr(uuid, "uuid7", None)
    value = generator() if generator is not None else uuid.uuid4()
    return f"{prefix}_{value.hex}"
