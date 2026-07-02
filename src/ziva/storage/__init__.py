"""Storage module for the ziva runtime.

``Storage`` is the contract the runtime talks to; ``FileStorage`` is the
filesystem-backed implementation used by the CLI/desktop app; ``InMemoryStorage``
is an in-process implementation for the SDK / tests. ``set_base_dir`` redirects
``FileStorage`` away from ``~/.ziva``.
"""

from ziva.storage.base import Storage
from ziva.storage.file_storage import FileStorage, get_storage, set_base_dir, get_base_dir, _project_hash
from ziva.storage.memory import InMemoryStorage

__all__ = [
    "Storage",
    "FileStorage",
    "InMemoryStorage",
    "get_storage",
    "set_base_dir",
    "get_base_dir",
    "_project_hash",
]
