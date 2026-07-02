"""In-memory storage backend for the ziva runtime.

Drop-in replacement for :class:`FileStorage` that keeps everything in process
memory — no ``~/.ziva`` on disk, no ``fcntl``, no filesystem at all. Intended
for the SDK (``ziva.Agent(storage=InMemoryStorage())``), unit tests, and any
embedded use where persistence isn't wanted.

Implements the same method surface documented by
:class:`ziva.storage.base.Storage`. ``project_dir`` returns a meaningless
placeholder path (the in-memory backend stores no attachment files); callers
that need real attachment paths should use ``FileStorage``.
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional


class InMemoryStorage:
    """Dict-backed implementation of the Storage contract."""

    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, dict]] = {}      # pid -> {sid -> session}
        self._messages: Dict[str, Dict[str, List[dict]]] = {}  # pid -> {sid -> [msg]}
        self._projects: Dict[str, dict] = {}
        self._automations: Dict[str, List[dict]] = {}

    # ---- sessions ----
    def create_session(self, project_id: str, session_data: dict) -> None:
        self._sessions.setdefault(project_id, {})[session_data["id"]] = dict(session_data)

    def update_session(self, project_id: str, session_id: str, session_data: dict) -> None:
        existing = self._sessions.setdefault(project_id, {}).get(session_id, {})
        existing.update(session_data)
        self._sessions[project_id][session_id] = existing

    def get_session(self, project_id: str, session_id: str) -> Optional[dict]:
        return self._sessions.get(project_id, {}).get(session_id)

    def list_sessions(self, project_id: str) -> List[dict]:
        items = list(self._sessions.get(project_id, {}).values())
        # Mirror FileStorage: hide sub-agent sessions, newest-first by time.updated.
        visible = [s for s in items if not s.get("is_subagent")]
        return sorted(visible, key=lambda x: x.get("time", {}).get("updated", 0), reverse=True)

    def delete_session(self, project_id: str, session_id: str) -> None:
        self._sessions.get(project_id, {}).pop(session_id, None)
        self._messages.get(project_id, {}).pop(session_id, None)

    # ---- messages ----
    def append_message(self, project_id: str, session_id: str, message_data: dict) -> None:
        self._messages.setdefault(project_id, {}).setdefault(session_id, []).append(dict(message_data))

    def get_messages(self, project_id: str, session_id: str, locked: bool = False) -> Iterator[dict]:
        # Materialize before yielding, matching FileStorage (lock released promptly).
        for msg in list(self._messages.get(project_id, {}).get(session_id, [])):
            yield msg

    def update_message(self, project_id: str, session_id: str, message_id: str, message_data: dict) -> None:
        msgs = self._messages.get(project_id, {}).get(session_id, [])
        for msg in msgs:
            if msg.get("id") == message_id:
                msg.update(message_data)

    def replace_messages(self, project_id: str, session_id: str, messages: List[dict]) -> None:
        self._messages.setdefault(project_id, {})[session_id] = [dict(m) for m in messages]

    # ---- project ----
    def get_project(self, project_id: str) -> Optional[dict]:
        return self._projects.get(project_id)

    def save_project(self, project_id: str, project_data: dict) -> None:
        self._projects[project_id] = dict(project_data)

    # ---- automations ----
    def list_automations(self, project_id: str) -> List[dict]:
        return [a for a in self._automations.get(project_id, []) if isinstance(a, dict)]

    def replace_automations(self, project_id: str, automations: List[dict]) -> None:
        self._automations[project_id] = [a for a in automations if isinstance(a, dict)]

    def upsert_automation(self, project_id: str, automation: dict) -> None:
        automations = self.list_automations(project_id)
        aid = automation.get("id")
        for idx, item in enumerate(automations):
            if item.get("id") == aid:
                automations[idx] = automation
                self.replace_automations(project_id, automations)
                return
        automations.append(automation)
        self.replace_automations(project_id, automations)

    def delete_automation(self, project_id: str, automation_id: str) -> bool:
        automations = self.list_automations(project_id)
        kept = [a for a in automations if a.get("id") != automation_id]
        if len(kept) == len(automations):
            return False
        self.replace_automations(project_id, kept)
        return True

    # ---- layout ----
    def project_dir(self, project_id: str):
        # No on-disk layout; return a placeholder so callers that only need the
        # abstract handle don't crash. Real attachment storage requires FileStorage.
        import pathlib
        return pathlib.Path(f"<in-memory>/{project_id}")


__all__ = ["InMemoryStorage"]
