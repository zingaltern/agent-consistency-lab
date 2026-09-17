"""外围集成（可选装、可选跑）：MCP 工具服务 + 可观测落地。

**边界**：这个包**只**改变"谁能调用这些工具"和"别人用什么工具看这些数据"，
不产生任何结论，也不改变 `docs/semantics.md` 里任何一条承诺的适用范围。
依赖方向是单向的：`integrations/ → harness/`；内核目录**不得** import 本包
（有结构用例守着）。内核依赖仍然只有 `pydantic`——MCP SDK 只进 `[mcp]` extra。

安装与用法见 `docs/integrations.md`。
"""

__all__: list[str] = []
