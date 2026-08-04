from __future__ import annotations

from typing import Any, Dict, Protocol

from ziva.shared_types import RuntimeContext, ToolResult


class PromptProvider(Protocol):
    def render(self, template: str, variables: Dict[str, Any], ctx: RuntimeContext) -> str: ...


class Tool(Protocol):
    def spec(self) -> Dict[str, Any]: ...
    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult: ...


class Skill(Protocol):
    def match(self, input_text: str, ctx: RuntimeContext) -> bool: ...
    async def execute(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]: ...


class BaseHook:
    """所有 hook 的基类。Python hook 和 shell hook 共享这套字段。

    字段由 loader 从 manifest.yaml 统一赋值（manifest 是 source of truth）。
    子类只需实现 ``handle``。
    """
    event_name: str = "after_tool"
    matcher: str | None = None
    block: bool = False
    timeout: int = 10
    async_run: bool = False

    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        raise NotImplementedError


# 向后兼容别名
Hook = BaseHook


class MemoryStore(Protocol):
    async def put(self, key: str, value: Dict[str, Any], ctx: RuntimeContext) -> None: ...
    async def search(self, query: str, limit: int, ctx: RuntimeContext) -> list[Dict[str, Any]]: ...
    async def summarize(self, ctx: RuntimeContext) -> Dict[str, Any]: ...
