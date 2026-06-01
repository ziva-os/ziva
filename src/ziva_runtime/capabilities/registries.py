from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class CapabilityRecord:
    id: str
    kind: str
    instance: Any
    manifest: Dict[str, Any]


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: Dict[str, CapabilityRecord] = {}

    def register(self, capability_id: str, kind: str, instance: Any, manifest: Dict[str, Any]) -> None:
        self._items[capability_id] = CapabilityRecord(
            id=capability_id,
            kind=kind,
            instance=instance,
            manifest=manifest,
        )

    def get(self, capability_id: str) -> CapabilityRecord:
        return self._items[capability_id]

    def list_kind(self, kind: str) -> list[CapabilityRecord]:
        return [item for item in self._items.values() if item.kind == kind]

    def all(self) -> list[CapabilityRecord]:
        return list(self._items.values())
