from __future__ import annotations

from typing import Any, Dict, Protocol

from ziva_runtime.shared_types import RuntimeContext


class PromptProvider(Protocol):
    def render(self, template: str, variables: Dict[str, Any], ctx: RuntimeContext) -> str: ...


class Tool(Protocol):
    def spec(self) -> Dict[str, Any]: ...
    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]: ...


class Skill(Protocol):
    def match(self, input_text: str, ctx: RuntimeContext) -> bool: ...
    async def execute(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]: ...


class Hook(Protocol):
    event_name: str
    async def handle(self, event: Dict[str, Any], ctx: RuntimeContext) -> None: ...


class MemoryStore(Protocol):
    async def put(self, key: str, value: Dict[str, Any], ctx: RuntimeContext) -> None: ...
    async def search(self, query: str, limit: int, ctx: RuntimeContext) -> list[Dict[str, Any]]: ...
    async def summarize(self, ctx: RuntimeContext) -> Dict[str, Any]: ...
