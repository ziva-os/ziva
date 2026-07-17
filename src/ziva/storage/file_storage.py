"""Local file-based storage for ziva runtime sessions."""

from __future__ import annotations

import json
import fcntl
import uuid
from pathlib import Path
from typing import Any, Optional, List, Generator
from contextlib import contextmanager
import hashlib

_BASE_DIR = Path.home() / ".ziva"


def set_base_dir(path) -> None:
    """Override the storage root (default ``~/.ziva``).

    Used by the SDK / tests to point FileStorage at a temp or project-local
    directory instead of the user's home. Forces re-initialization so the
    directory layout is (re)created at the new location on next access.
    """
    global _BASE_DIR
    _BASE_DIR = Path(path).expanduser()
    FileStorage._initialized = False


def get_base_dir() -> Path:
    """Return the current storage root (see :func:`set_base_dir`)."""
    return _BASE_DIR



def _project_hash(workspace_root: Path) -> str:
    """Generate a stable, human-readable project ID from workspace path.

    Format: ``<basename>-<short_hash>`` (e.g. ``ziva-e4d21244``). The
    basename makes the ``sessions/<id>/`` directory readable at a glance;
    the 8-char hash disambiguates same-named workspaces in different
    locations. Non-alphanumeric chars in the basename become ``-``.
    """
    base = workspace_root.name or "root"
    safe = "".join(c if c.isalnum() else "-" for c in base.lower()).strip("-") or "ws"
    short = hashlib.sha256(str(workspace_root).encode()).hexdigest()[:8]
    return f"{safe}-{short}"


class FileStorage:
    """File-based storage with JSONL message format."""

    _initialized = False
    _lock_dir: Path

    @classmethod
    def _ensure_initialized(cls) -> None:
        if cls._initialized:
            return
        cls._initialized = True
        cls._ensure_dirs()

    @classmethod
    def _ensure_dirs(cls) -> None:
        """Create all required directories."""
        base = _BASE_DIR
        cls._lock_dir = base / ".locks"
        for d in [base, base / "sessions", base / "automations", cls._lock_dir]:
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _project_dir(cls, project_id: str) -> Path:
        return _BASE_DIR / "sessions" / project_id

    @classmethod
    def project_dir(cls, project_id: str) -> Path:
        """Public alias of :meth:`_project_dir` (the on-disk session dir).

        Part of the Storage contract; the desktop transport uses it to resolve
        attachment paths without reaching into a private method.
        """
        return cls._project_dir(project_id)


    @classmethod
    def _session_file(cls, project_id: str, session_id: str) -> Path:
        return cls._project_dir(project_id) / f"{session_id}.json"

    @classmethod
    def _messages_file(cls, project_id: str, session_id: str) -> Path:
        return cls._project_dir(project_id) / "messages" / f"{session_id}.jsonl"

    @classmethod
    def _automations_file(cls, project_id: str) -> Path:
        return _BASE_DIR / "automations" / f"{project_id}.json"

    @classmethod
    @contextmanager
    def _lock(cls, path: Path, exclusive: bool = True):
        """File-based locking.

        The lock file persists between calls (one stable ``<name>.lock`` per
        resource, reused on every access). We deliberately do NOT unlink it
        here: removing an flock lock file is the classic unsafe pattern — a
        concurrent process can recreate the file under a different inode and
        both then believe they hold the lock, silently breaking mutual
        exclusion. Orphan lock files (left when a session is deleted) are
        cleaned up at deletion time in :meth:`delete_session` instead.
        """
        cls._ensure_dirs()
        lock_file = cls._lock_dir / f"{path.name}.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_file, "w") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    @classmethod
    def read_json(cls, path: Path) -> Any:
        """Read JSON file."""
        with open(path, "r") as f:
            return json.load(f)

    @classmethod
    def write_json(cls, path: Path, data: Any) -> None:
        """Write JSON file atomically with unique temp file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.rename(path)

    # ============= Session Operations =============

    @classmethod
    def create_session(cls, project_id: str, session_data: dict) -> None:
        """Create a new session."""
        cls._ensure_dirs()
        path = cls._session_file(project_id, session_data["id"])
        with cls._lock(path):
            cls.write_json(path, session_data)

    @classmethod
    def update_session(cls, project_id: str, session_id: str, session_data: dict) -> None:
        """Update session data."""
        path = cls._session_file(project_id, session_id)
        with cls._lock(path):
            existing = cls.read_json(path) if path.exists() else {}
            existing.update(session_data)
            cls.write_json(path, existing)

    @classmethod
    def get_session(cls, project_id: str, session_id: str) -> Optional[dict]:
        """Get session by ID."""
        path = cls._session_file(project_id, session_id)
        if not path.exists():
            return None
        with cls._lock(path, exclusive=False):
            return cls.read_json(path)

    @classmethod
    def list_sessions(cls, project_id: str) -> List[dict]:
        """List all sessions for a project."""
        project_dir = cls._project_dir(project_id)
        if not project_dir.exists():
            return []
        sessions = []
        for path in project_dir.glob("*.json"):
            if path.name in ("project.json", "permissions.json"):
                continue
            with cls._lock(path, exclusive=False):
                try:
                    data = cls.read_json(path)
                except Exception:
                    continue
                # Hide sub-agent sessions — they belong to a parent turn and
                # shouldn't surface as top-level conversations in the sidebar.
                if data.get("is_subagent"):
                    continue
                sessions.append(data)
        return sorted(sessions, key=lambda x: x.get("time", {}).get("updated", 0), reverse=True)

    @classmethod
    def delete_session(cls, project_id: str, session_id: str) -> None:
        """Delete a session and its messages."""
        import shutil
        session_path = cls._session_file(project_id, session_id)
        messages_path = cls._messages_file(project_id, session_id)
        attachments_dir = cls._project_dir(project_id) / "attachments" / session_id
        with cls._lock(session_path):
            if session_path.exists():
                session_path.unlink()
            if messages_path.exists():
                messages_path.unlink()
            # Drop any image attachments the user dropped into this
            # session. Pairs with /sessions/{sid}/attachments upload
            # path; without this the disk would grow forever.
            if attachments_dir.exists():
                shutil.rmtree(attachments_dir, ignore_errors=True)
        # The data files are gone, so their lock files are now orphans.
        # Remove them so ~/.ziva/.locks doesn't accumulate one stale .lock
        # pair per deleted session. Done AFTER releasing the lock (we held
        # session_path's) and best-effort: the resource is already deleted,
        # so a concurrent opener only races against a vanishing session.
        for p in (session_path, messages_path):
            try:
                (cls._lock_dir / f"{p.name}.lock").unlink()
            except OSError:
                pass

    # ============= Message Operations (JSONL) =============

    @classmethod
    def append_message(cls, project_id: str, session_id: str, message_data: dict) -> None:
        """Append a message to the JSONL file."""
        cls._ensure_dirs()
        path = cls._messages_file(project_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with cls._lock(path):
            with open(path, "a") as f:
                f.write(json.dumps(message_data, ensure_ascii=False) + "\n")

    @classmethod
    def get_messages(cls, project_id: str, session_id: str, locked: bool = False) -> Generator[dict, None, None]:
        """Read all messages from JSONL file.

        Materializes all messages before yielding so the file lock is
        released promptly and not held across generator suspension points.

        Args:
            project_id: Project ID
            session_id: Session ID
            locked: If True, skip locking (caller already holds lock). Use with caution!
        """
        path = cls._messages_file(project_id, session_id)
        if not path.exists():
            return
        if locked:
            with open(path, "r") as f:
                messages = [json.loads(line) for line in f if line.strip()]
        else:
            with cls._lock(path, exclusive=False):
                with open(path, "r") as f:
                    messages = [json.loads(line) for line in f if line.strip()]
        yield from messages

    @classmethod
    def update_message(cls, project_id: str, session_id: str, message_id: str, message_data: dict) -> None:
        """Update a message in the JSONL file (rewrite entire file)."""
        path = cls._messages_file(project_id, session_id)
        if not path.exists():
            return
        with cls._lock(path):
            messages = []
            for msg in cls.get_messages(project_id, session_id, locked=True):
                if msg.get("id") == message_id:
                    msg.update(message_data)
                messages.append(msg)
            with open(path, "w") as f:
                for msg in messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    @classmethod
    def replace_messages(cls, project_id: str, session_id: str, messages: list[dict]) -> None:
        """Replace all messages in the JSONL file."""
        cls._ensure_dirs()
        path = cls._messages_file(project_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with cls._lock(path):
            with open(path, "w") as f:
                for msg in messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    # ============= Project Operations =============

    @classmethod
    def get_project(cls, project_id: str) -> Optional[dict]:
        """Get project data."""
        path = cls._project_dir(project_id) / "project.json"
        if not path.exists():
            return None
        with cls._lock(path, exclusive=False):
            return cls.read_json(path)

    @classmethod
    def save_project(cls, project_id: str, project_data: dict) -> None:
        """Save project data."""
        cls._ensure_dirs()
        path = cls._project_dir(project_id) / "project.json"
        with cls._lock(path):
            cls.write_json(path, project_data)

    # ============= Automation Operations =============

    @classmethod
    def list_automations(cls, project_id: str) -> List[dict]:
        """List persisted automations for a project."""
        path = cls._automations_file(project_id)
        if not path.exists():
            return []
        with cls._lock(path, exclusive=False):
            data = cls.read_json(path)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("automations", [])
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    @classmethod
    def replace_automations(cls, project_id: str, automations: List[dict]) -> None:
        """Replace the persisted automation list."""
        cls._ensure_dirs()
        path = cls._automations_file(project_id)
        with cls._lock(path):
            cls.write_json(path, {"automations": automations})

    @classmethod
    def upsert_automation(cls, project_id: str, automation: dict) -> None:
        automations = cls.list_automations(project_id)
        aid = automation.get("id")
        replaced = False
        for idx, item in enumerate(automations):
            if item.get("id") == aid:
                automations[idx] = automation
                replaced = True
                break
        if not replaced:
            automations.append(automation)
        cls.replace_automations(project_id, automations)

    @classmethod
    def delete_automation(cls, project_id: str, automation_id: str) -> bool:
        automations = cls.list_automations(project_id)
        kept = [item for item in automations if item.get("id") != automation_id]
        if len(kept) == len(automations):
            return False
        cls.replace_automations(project_id, kept)
        return True


def get_storage() -> type[FileStorage]:
    """Get the file storage class."""
    return FileStorage


__all__ = ["FileStorage", "get_storage", "_project_hash"]
