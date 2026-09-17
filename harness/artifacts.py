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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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
        """历史接口（W4 起用于暴露孤儿数）。

        生产路径已由 :func:`sweep` 取代（它同时给出来源分类与删除清单）；
        本方法保留是因为 `tests/test_w4_components.py` 仍在用它做轻量断言——
        保留原因写在这里，免得后来者以为它是"没人用的死代码"（评审 P2-22）。
        """
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


# ------------------------------------------------------------------ 孤儿回收（R-B5）


class ArtifactSweepError(RuntimeError):
    """回收的前置条件不成立（例如：共享 artifacts root 却没给全引用来源）。"""


class SweepReport(BaseModel):
    dry_run: bool
    artifacts_root: str
    files_before: int
    orphans_found: int
    deleted: list[str] = Field(default_factory=list)
    kept_referenced: int = 0
    never_deleted: list[str] = Field(default_factory=list)
    scanned_sources: list[str] = Field(default_factory=list)
    scanned_run_dirs: list[str] = Field(default_factory=list)
    # 解析失败的 payload 数（评审 P2-13/P1-2）：枚举"哪些对象不能删"时吞掉解析错误，
    # 等于把损坏 payload 里的引用当作"没有引用"——这个计数让那件事至少可见。
    unreadable_payloads: int = 0
    # 共享 artifacts root（不在任何一个被扫描的 run 目录下）：此时**无法**保证
    # 其它 run 的引用被枚举到，因此 `--apply` 必须显式 --force。
    shared_root: bool = False


def referenced_digests(
    run_dirs: Path | Sequence[Path],
) -> tuple[set[str], dict[str, int], int]:
    """收集**引用来源**：``(digests, 每类来源计数, 解析失败的 payload 数)``。

    对抗审查第 21 条的要求是"不许只扫当前 run"——本函数按**调用方给定的 run 目录集合**
    枚举引用，因此 CLI 支持多个 ``--run-dir``。共享 artifacts root + 只给一个 run 目录时，
    其它 run 的引用**枚举不到**：那种情况由 ``sweep`` 标记为 ``shared_root`` 并要求 ``--force``
    （评审 P1-2：docstring 曾经写着"全库扫描"，实现却只打开一个目录）。

    四类来源逐条列出，缺一条就会误删：

    1. ``events.payload_json`` 里的 compaction（``artifact_ref.digest`` 与 ``replaces``）；
    2. 事件里任何形状像 artifact 引用的 ``digest`` 字段（``read_artifact`` 的结果引用等）；
    3. ``checkpoints.state_json``（channel 快照里可能有 artifact 引用）；
    4. ``checkpoint_writes.payload_json``（写入侧的引用）。

    第 3、4 条是**明确要求**：只被 checkpoint 引用的对象**禁止回收**——否则"恢复时读不出
    上下文"这类故障会在回收后很久才暴露。返回 ``(digests, 每类来源的计数)`` 便于报告复算。
    """
    import sqlite3

    if isinstance(run_dirs, Path):
        run_dirs = [run_dirs]
    digests: set[str] = set()
    counts: dict[str, int] = {}
    unreadable = 0

    def _harvest(blob: str, source: str) -> int:
        found = 0
        try:
            payload = json.loads(blob)
        except (TypeError, ValueError):
            # 不静默：解析失败意味着"这份 payload 里的引用看不见"，
            # 它的后果是可能误删，因此计数上报、由 sweep 决定是否放行
            counts[f"{source}:unreadable"] = counts.get(f"{source}:unreadable", 0) + 1
            return 0
        stack = [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key == "digest" and isinstance(value, str) and value:
                        digests.add(value)
                        found += 1
                    else:
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
        counts[source] = counts.get(source, 0) + found
        return found

    for run_dir in run_dirs:
        runtime_db = Path(run_dir) / "runtime.db"
        if not runtime_db.exists():
            continue
        con = sqlite3.connect(runtime_db)
        try:
            for (payload_json,) in con.execute("SELECT payload_json FROM events").fetchall():
                _harvest(payload_json, "events.payload_json")
            for (state_json,) in con.execute("SELECT state_json FROM checkpoints").fetchall():
                _harvest(state_json, "checkpoints.state_json")
            for (payload_json,) in con.execute(
                "SELECT payload_json FROM checkpoint_writes"
            ).fetchall():
                _harvest(payload_json, "checkpoint_writes.payload_json")
        finally:
            con.close()
    unreadable = sum(value for key, value in counts.items() if key.endswith(":unreadable"))
    return digests, counts, unreadable


def sweep(
    store: ArtifactStore,
    *,
    run_dir: Path | Sequence[Path],
    dry_run: bool = True,
    allow_shared_root: bool = False,
) -> SweepReport:
    """回收 ``store.root`` 下**没有任何引用**的 artifact（默认 dry-run）。

    三条边界：

    * **只由显式 CLI 调用**：runtime loop 内绝不自动删（那会引入新的崩溃窗口——
      删除与"读到半个文件"之间没有事务可用）；
    * **引用枚举全库扫描**（见 ``referenced_digests``）：不限于当前 run 目录，
      也不限于事件表——只被 checkpoint 引用的对象同样保留；
    * **分类错误**：被回收后再次 ``get`` 会得到**可读失败**（``FileNotFoundError``
      带 digest），而不是空字符串或旧内容。
    """
    run_dirs = [run_dir] if isinstance(run_dir, Path) else list(run_dir)
    referenced, counts, unreadable = referenced_digests(run_dirs)
    files = [path for path in store.root.rglob("*") if path.is_file()]
    orphans = [path for path in files if path.name not in referenced]
    shared_root = not any(
        store.root.resolve() == (Path(item) / "artifacts").resolve() for item in run_dirs
    )
    if not dry_run and shared_root and not allow_shared_root:
        # 评审 P1-2：共享 artifacts root 时，"别的 run 有没有引用这个对象"我们**看不见**。
        # dry-run 仍然允许（它不删东西），真要删必须显式 --force。
        raise ArtifactSweepError(
            f"artifacts root {store.root} 不在任何被扫描的 run 目录下（共享库）："
            "当前只枚举了 " + "、".join(str(item) for item in run_dirs) + " 的引用，"
            "无法保证其它 run 的引用被看见。要么把那些 run 目录也传给 --run-dir，"
            "要么显式 --force（自担误删风险）。"
        )
    deleted: list[str] = []
    if not dry_run:
        for path in orphans:
            path.unlink()
            deleted.append(path.name)
    return SweepReport(
        dry_run=dry_run,
        artifacts_root=str(store.root),
        files_before=len(files),
        orphans_found=len(orphans),
        deleted=deleted,
        kept_referenced=len(files) - len(orphans),
        scanned_sources=sorted(counts),
        scanned_run_dirs=[str(item) for item in run_dirs],
        unreadable_payloads=unreadable,
        shared_root=shared_root,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI：``python -m harness.artifacts --run-dir X [--apply]``（默认 dry-run）。"""
    import argparse

    parser = argparse.ArgumentParser(prog="harness.artifacts")
    parser.add_argument(
        "--run-dir",
        action="append",
        required=True,
        help="引用来源所在的 run 目录（可重复：共享 artifacts 库时把所有引用方都传进来）",
    )
    parser.add_argument("--artifacts-root", default="", help="缺省 = <run-dir>/artifacts")
    parser.add_argument(
        "--apply", action="store_true", help="真的删除（默认只报告，绝不自动回收）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="共享 artifacts root 时仍然执行删除（自担误删风险；默认拒绝）",
    )
    args = parser.parse_args(argv)

    run_dirs = [Path(item) for item in args.run_dir]
    root = Path(args.artifacts_root) if args.artifacts_root else run_dirs[0] / "artifacts"
    try:
        report = sweep(
            ArtifactStore(root),
            run_dir=run_dirs,
            dry_run=not args.apply,
            allow_shared_root=args.force,
        )
    except ArtifactSweepError as exc:
        print(str(exc))
        return 2
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
