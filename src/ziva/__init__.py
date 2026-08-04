"""ziva — a customizable LLM agent runtime and SDK.

Public surface
--------------
* :class:`Agent`        — high-level one-liner agent (most users start here)
* :class:`Runtime`      — the full engine, for advanced/injected use
* Storage: :class:`FileStorage`, :class:`InMemoryStorage`, :class:`Storage`,
  :func:`set_base_dir`
* :class:`PermissionManager`, :class:`CapabilityRegistry`
* Data types: :class:`ChatMessage`, :class:`ChatResult`, :class:`ToolResult`,
  :class:`RuntimeContext`, :class:`StreamDelta`
* Extension protocols: :class:`Tool`, :class:`Skill`, :class:`Hook`,
  :class:`MemoryStore`, :class:`PromptProvider`

Importing this package does NOT require aiohttp — the HTTP/SSE/CLI transports
live under ``ziva.transports`` / ``ziva.app`` / ``ziva.protocols`` and are only
loaded when you use them (or install the ``[desktop]`` / ``[cli]`` extras).
"""

from ziva.agent import Agent
from ziva.capabilities.registries import CapabilityRegistry
from ziva.permissions import PermissionManager
from ziva.runtime import Runtime
from ziva.shared_types import (
    ChatMessage,
    ChatResult,
    RuntimeContext,
    StreamDelta,
    ToolResult,
)
from ziva.storage import FileStorage, InMemoryStorage, Storage, set_base_dir

# Extension protocols — imported lazily-free (no heavy deps).
from ziva.capabilities.interfaces import Hook, MemoryStore, PromptProvider, Skill, Tool

__version__ = "1.1.5"

__all__ = [
    "__version__",
    # high-level
    "Agent",
    "Runtime",
    # storage
    "FileStorage",
    "InMemoryStorage",
    "Storage",
    "set_base_dir",
    # services
    "PermissionManager",
    "CapabilityRegistry",
    # types
    "ChatMessage",
    "ChatResult",
    "ToolResult",
    "RuntimeContext",
    "StreamDelta",
    # protocols
    "Tool",
    "Skill",
    "Hook",
    "MemoryStore",
    "PromptProvider",
]
