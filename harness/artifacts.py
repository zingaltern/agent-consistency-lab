"""内容寻址的 artifact 存储：大结果卸载与按需读回。

三条设计约束：

1. **内容寻址**：``digest = sha256(content)``。同一个结果无论被卸载多少次都落在
   同一个文件上，因此视图渲染是**确定性**的——这一点直接影响缓存前缀能否稳定命中。
2. **先写后引用**：artifact 必须在引用它的上下文/事件之前落盘。崩溃只会留下
   无人引用的孤儿文件（可 GC），不会留下"引用了一个不存在的 artifact"的坏状态。
3. **按需读回**：模型通过 ``read_artifact`` 工具取切片（progressive disclosure），
   而不是把全文塞回上下文——读回同样计入预算与缓存。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .tokens import estimate_tokens
from .tools import Effect, Tool


class ArtifactRef(BaseModel):
    digest: str
    bytes: int
    tokens: int
    preview: str


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put(self, content: str) -> ArtifactRef:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = self.path_for(digest)
        if not path.exists():  # 内容寻址 ⇒ 幂等，重复写不做任何事
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(path)  # 原子替换：读者永远看不到半个文件
        return ArtifactRef(
            digest=digest,
            bytes=len(content.encode("utf-8")),
            tokens=estimate_tokens(content),
            preview=content[:160],
        )

    def put_json(self, payload: Any) -> ArtifactRef:
        return self.put(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))

    def get(self, digest: str) -> str:
        path = self.path_for(digest)
        if not path.exists():
            raise FileNotFoundError(f"artifact {digest} not found")
        return path.read_text(encoding="utf-8")

    def read_slice(self, digest: str, *, offset: int = 0, limit: int = 4000) -> dict[str, Any]:
        content = self.get(digest)
        chunk = content[offset : offset + limit]
        return {
            "digest": digest,
            "offset": offset,
            "returned_chars": len(chunk),
            "total_chars": len(content),
            "truncated": offset + limit < len(content),
            "content": chunk,
        }

    def orphan_count(self, referenced: set[str]) -> int:
        return sum(
            1 for path in self.root.rglob("*") if path.is_file() and path.name not in referenced
        )


def make_read_artifact_tool(store: ArtifactStore, *, max_limit: int = 8000) -> Tool:
    """把 artifact 读回注册成工具：模型按需取切片，而不是让全文留在上下文里。"""

    def read_artifact(args: dict[str, Any], _key: str) -> dict[str, Any]:
        digest = str(args.get("digest", ""))
        offset = int(args.get("offset", 0))
        limit = min(int(args.get("limit", 4000)), max_limit)
        return store.read_slice(digest, offset=offset, limit=limit)

    return Tool(
        name="read_artifact",
        effect=Effect.READ,
        fn=read_artifact,
        description="按 digest 读取被卸载的大结果切片（offset/limit）",
        tags=("readonly", "artifact"),
    )
