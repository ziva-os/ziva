from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ziva.adapters.openai.provider import ModelAdapter, OpenAIChatAdapter

logger = logging.getLogger(__name__)
from ziva.capabilities.events import EventBus
from ziva.capabilities.registries import CapabilityRegistry
from ziva.config.instructions import load_layered_instructions
from ziva.config.loader import load_effective_config, validate_config
from ziva.permissions import (
    DeniedError,
    PermissionManager,
    from_config,
    get_permission_manager,
    RejectedError,
)
from ziva.plugins.loader import load_plugins
from ziva.session.compaction import (
    compact_messages,
    _llm_context,
    compose_post_compact_on_disk,
    estimate_tokens,
    find_last_summary_idx,
    find_cutoff_in_llm_visible,
)
from ziva.shared_types import ApprovalRequest, ApprovalPolicy, CancellationToken, ChatMessage, ChatResult, MCPConnectStatus, RuntimeContext, SessionState, ToolCall, ToolCallItem, ToolResult
from ziva.storage.file_storage import FileStorage, _project_hash


_ADAPTER_REGISTRY: dict[tuple, "ModelAdapter"] = {}


def _find_provider_for_model(config: dict) -> dict | None:
    """Return the provider entry that owns ``config["model"]["name"]``.

    Lookup is **case-insensitive**: session metadata persists the model
    name as it was selected in the UI at the time the turn was created,
    and historical sessions (or older tests) can have casing that
    differs from the canonical case declared in ``~/.ziva/config.yaml``
    (e.g. ``"Kimi-K2.6"`` vs. ``"kimi-k2.6"``). A case-sensitive
    comparison would then refuse the turn with "Model … is not listed
    in any provider's models" even though the model is in fact
    configured. The provider config is returned unchanged so the
    caller can read ``api_key`` / ``base_url`` / ``options`` with the
    original keys intact.
    """
    model_name = (config.get("model", {}).get("name") or "").lower()
    if not model_name:
        return None
    provider_name = (config.get("model", {}).get("provider_name") or "").lower()
    providers = config.get("providers", []) or []
    if provider_name:
        # Same model name under multiple providers — match the chosen one exactly.
        for p in providers:
            if (p.get("name") or "").lower() == provider_name:
                for m in p.get("models", []) or []:
                    if (m.get("name") or "").lower() == model_name:
                        return p
                return None
        return None
    for p in providers:
        for m in p.get("models", []) or []:
            if (m.get("name") or "").lower() == model_name:
                return p
    return None


def _resolve_capabilities(provider_cfg: dict, model_name: str) -> dict:
    """Merge provider-level and model-level capabilities. Model overrides provider."""
    merged: dict = {}
    for key, val in (provider_cfg.get("capabilities") or {}).items():
        merged[key] = val
    for m in provider_cfg.get("models", []) or []:
        if m.get("name") == model_name:
            for key, val in (m.get("capabilities") or {}).items():
                merged[key] = val
            break
    return merged


def _build_adapter(provider_cfg: dict, *, model_name: str, max_tokens: int) -> "ModelAdapter":
    """Construct a fresh adapter from a provider config dict (no caching)."""
    from ziva.adapters.openai.provider import OpenAIChatAdapter

    capabilities = _resolve_capabilities(provider_cfg, model_name)
    api_type = provider_cfg.get("api_type", "openai_compatible")
    if api_type == "anthropic":
        from ziva.adapters.anthropic.provider import AnthropicChatAdapter
        return AnthropicChatAdapter(
            api_key=provider_cfg.get("api_key") or None,
            base_url=provider_cfg.get("base_url") or None,
            default_max_tokens=max_tokens,
            capabilities=capabilities,
        )
    return OpenAIChatAdapter(
        base_url=provider_cfg.get("base_url") or None,
        api_key=provider_cfg.get("api_key") or None,
        capabilities=capabilities,
        options=provider_cfg.get("options") or {},
        default_max_tokens=max_tokens,
    )


def _create_adapter(config: dict) -> "ModelAdapter":
    """Return a cached adapter for the provider owning config's model.

    Cached by (api_type, base_url, api_key) so HTTP connection pools are reused
    across turns and across sessions. Raises ValueError if the configured model
    is not declared in any provider's models list — no silent fallback.
    """
    provider_cfg = _find_provider_for_model(config)
    if provider_cfg is None:
        model_name = config.get("model", {}).get("name", "")
        available = [p.get("name") for p in (config.get("providers") or [])]
        raise ValueError(
            f"Model '{model_name}' is not listed in any provider's models. "
            f"Available providers: {available}. Add the model to a provider entry "
            f"in .ziva/config.yaml, or change model.name to match a declared model."
        )

    key = (
        provider_cfg.get("api_type", "openai_compatible"),
        provider_cfg.get("base_url") or "",
        provider_cfg.get("api_key") or "",
    )
    cached = _ADAPTER_REGISTRY.get(key)
    if cached is not None:
        return cached

    model_name = config.get("model", {}).get("name", "")
    max_tokens = int(config.get("model", {}).get("max_tokens", 16384))
    adapter = _build_adapter(provider_cfg, model_name=model_name, max_tokens=max_tokens)
    _ADAPTER_REGISTRY[key] = adapter
    return adapter


def _reset_adapter_registry() -> None:
    """Test helper — clears the adapter cache to avoid cross-test pollution."""
    _ADAPTER_REGISTRY.clear()


def _detect_timezone() -> str:
    tz = os.environ.get("TZ")
    if tz:
        return tz.lstrip(":")

    try:
        resolved = Path("/etc/localtime").resolve()
        parts = resolved.parts
        if "zoneinfo" in parts:
            idx = parts.index("zoneinfo")
            zone = "/".join(parts[idx + 1:])
            if zone:
                return zone
    except OSError:
        pass

    local_now = datetime.now().astimezone()
    return local_now.tzname() or "local"


def _current_date_for_timezone(timezone: str) -> str:
    try:
        return datetime.now(ZoneInfo(timezone)).date().isoformat()
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().date().isoformat()


# Skill categorization — used by the sidebar Skills page to group
# skills in the browsing UI. The mapping is purely keyword-based and
# intentionally coarse: a few top-level buckets are easier to scan
# than a long flat list, and the search box handles fine-grained
# filtering.
#
# Matching is **score-based**: every keyword hit contributes its length
# as a score, and the rule with the longest single keyword hit wins.
# This avoids the "first short generic substring wins" trap that the
# previous first-match-wins design had — e.g. `stitch-loop` losing to
# `stitch` because the 6-char `stitch` was listed before the 10-char
# `stitch-loop`. Within a rule, keywords are ordered most-specific-first
# so ties resolve in a sensible order.
#
# "其他" is the fallback when no keyword hits at all.
_SKILL_CATEGORY_RULES: List[tuple] = [
    # 文档/Office — multi-word phrases first so longer hits win
    ("文档/Office", ("internal comm", "spreadsheet file", "pdf files", "docx files", "xlsx files",
                     "pptx files", "spreadsheet", "presentation", "communication", "documents in",
                     "office", "docx", "pptx", "xlsx", "pdf")),
    # MCP/集成 — whole-name keywords before generic "mcp"
    ("MCP/集成", ("model context protocol", "clawdhub", "tool integration",
                  "agent skills", "install skill", "mcp server", "mcp", "plugin")),
    # 开发/工程 — `stitch-loop` whole-name before generic `build`/`code`
    ("开发/工程", ("stitch-loop", "frontend", "backend", "scaffold", "website",
                   "engineer", "debug", "test", "coding", "build", "code")),
    # 视频/动画 — multi-word phrases first
    ("视频/动画", ("manga-drama", "manga-style-video", "video-wrapper", "seedance",
                   "video", "manga", "drama", "stitch", "动画", "短剧", "漫画", "视频", "即梦")),
    # 浏览器/网页
    ("浏览器/网页", ("browser automation", "web page", "devtools", "navigate",
                     "snapshot", "browser", "fill form", "click", "dom")),
    # 规划/工作流
    ("规划/工作流", ("task_plan", "session-catchup", "findings", "progress",
                     "brainstorm", "workflow", "manus", "会话", "plan")),
    # 数据/搜索
    ("数据/搜索", ("search the web", "data source", "datasource",
                   "网络搜索", "search", "数据")),
    # GIF/动图
    ("GIF/动图", ("animated", "slack", "gif")),
    # 金融/投资
    ("金融/投资", ("portfolio", "crypto", "financial", "finance", "trading", "yahoo", "stock")),
    # 设计/UI
    ("设计/UI", ("interface", "styling", "visual", "theme", "design", "ux", "ui")),
]


def _categorize_skill(name: str, description: str) -> str:
    """Best-effort categorization for a skill based on its name + description.

    The runtime scans SKILL.md frontmatter to build a compact index, and
    callers (the desktop UI's Skills page) need a single `category`
    string per entry so they can group skills into collapsible sections
    and offer category filters.

    Match is score-based (longest keyword hit wins); see
    ``_SKILL_CATEGORY_RULES`` for the rationale. The cost of a wrong
    bucket (a skill landing in the wrong section) is much lower than
    the cost of an uncategorized skill that the user has to hunt for,
    so the rules are intentionally lenient.
    """
    haystack = f"{name} {description}".lower()
    best_category = "其他"
    best_score = 0
    for category, keywords in _SKILL_CATEGORY_RULES:
        for kw in keywords:
            if kw in haystack and len(kw) > best_score:
                best_category = category
                best_score = len(kw)
    return best_category


def _resolve_image_paths(
    messages: List[ChatMessage],
    *,
    base_dir: Path | None = None,
    model_supports_image: bool = True,
) -> List[ChatMessage]:
    """Expand local-path `image_url` blocks based on model capability.

    The desktop UI drops user-pasted images and drag-and-dropped files
    onto disk under ``~/.ziva/sessions/<pid>/attachments/<sid>/`` and
    sends the absolute path as the ``url`` of an ``image_url`` content
    block. How we expand that path depends on what the *current* model
    can do:

    * **Vision-capable model** (``model_supports_image=True``) — read
      the file and convert to a base64 data URL. The provider sees an
      image the user actually attached and can reason about it. Any
      unresolvable path (missing file, unknown extension, relative
      path with no anchor) is rewritten to a text reference so the
      provider never sees a raw path-shaped URL.

    * **Non-vision model** (``model_supports_image=False``) — there is
      no point burning tokens on a base64 blob the model cannot read.
      Convert every path-shaped ``image_url`` block to a text reference
      naming the file. The model can still call ``read_file`` on the
      path if it has tools that help (OCR, image analysis, etc.) or
      it can tell the user "I see an attachment at X but I can't view
      images — could you describe it?".

    http(s) and data: URLs are passed through untouched regardless of
    the model flag — those are provider-native and the user wrote them
    deliberately.

    The original message list is never mutated; we deep-copy any
    message that needs editing so the persisted history keeps the
    path form (cheap to reload, no multi-MB blobs in the JSONL).
    """
    import base64
    import copy
    from ziva.shared_types import ChatMessage as _CM  # local alias for type hints

    resolved: List[_CM] = []
    for msg in messages:
        content = msg.content
        if not isinstance(content, list):
            resolved.append(msg)
            continue
        changed = False
        new_blocks: list = []
        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue
            if block.get("type") != "image_url":
                new_blocks.append(block)
                continue
            url_field = block.get("image_url")
            if isinstance(url_field, dict):
                url = url_field.get("url", "")
            elif isinstance(url_field, str):
                url = url_field
            else:
                url = ""
            if not isinstance(url, str) or not url:
                new_blocks.append({
                    "type": "text",
                    "text": "[attachment: empty url]",
                })
                changed = True
                continue
            stripped = url.strip()
            # data: and http(s): are provider-native — pass through
            # regardless of the model flag. The user wrote these
            # deliberately and the provider knows what to do with
            # them. A data: blob is what a vision model needs; an
            # http(s) URL is what any model can be told to fetch
            # (though non-vision models will just see it as a string).
            if stripped.startswith(("data:", "http://", "https://")):
                new_blocks.append(block)
                continue
            # file:// is a path with a scheme prefix — strip and treat
            # as a path.
            if stripped.startswith("file://"):
                file_path_str = stripped[len("file://"):]
            else:
                file_path_str = stripped

            # -------------------------------------------------------------
            # Branch 1: current model is *not* vision-capable.
            #
            # Don't even try to read the file. Burning tokens on a
            # base64 blob that the model cannot interpret is pure
            # waste — and the provider will likely error or silently
            # drop it anyway. Surface the attachment as a named text
            # reference instead. The model can use read_file (if the
            # file happens to be parseable as text) or specialized
            # image tools to act on it, or just tell the user it
            # can't view images.
            # -------------------------------------------------------------
            if not model_supports_image:
                new_blocks.append({
                    "type": "text",
                    "text": f"[attachment: {file_path_str}]",
                })
                changed = True
                continue

            # -------------------------------------------------------------
            # Branch 2: current model *is* vision-capable.
            #
            # Read the file, base64-encode, embed as a data URL so
            # the provider sees the image directly. Any failure mode
            # (file missing, unknown extension, read error) falls
            # back to a text reference — never a leaked path, never
            # a raw image_url with a non-data: URL.
            # -------------------------------------------------------------
            try:
                path = Path(file_path_str)
            except (OSError, ValueError):
                new_blocks.append({
                    "type": "text",
                    "text": f"[attachment: invalid path `{file_path_str}`]",
                })
                changed = True
                continue
            if not path.is_absolute():
                if base_dir is not None:
                    path = base_dir / path
                else:
                    new_blocks.append({
                        "type": "text",
                        "text": f"[attachment: relative path `{file_path_str}` (no base directory)]",
                    })
                    changed = True
                    continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                new_blocks.append({
                    "type": "text",
                    "text": f"[attachment: {file_path_str} — {exc.__class__.__name__}]",
                })
                changed = True
                continue
            ext = path.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "webp": "image/webp",
            }.get(ext)
            if mime is None:
                # File exists but isn't a recognized image extension.
                # Don't leak a path-shaped URL to the provider; rewrite
                # as a text block with the file metadata. The model
                # can call read_file on the path if it actually wants
                # the contents.
                new_blocks.append({
                    "type": "text",
                    "text": f"[file: {file_path_str} ({len(data)} bytes, .{ext or '?'})]",
                })
                changed = True
                continue
            b64 = base64.b64encode(data).decode("ascii")
            new_blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
            changed = True
        if changed:
            resolved.append(copy.copy(msg))
            resolved[-1].content = new_blocks
        else:
            resolved.append(msg)
    return resolved


# Auto-compact hook configuration. These are the only knobs the start-of-round
# hook has; they apply to both the in-turn rounds (triggered by API prompt_tokens)
# and the implicit "next turn" case (a stale 100% session, see below).
AUTO_COMPACT_THRESHOLD = 0.9
# Keep the last K model-call cycles verbatim. A "cycle" = one assistant
# message (possibly with tool_calls) + the tool_results that follow. We
# count by asst turns (not user messages) because one user message can
# produce many model calls when the agent loops with tools — K=5 asst
# turns gives the model a decent recent context (typically 10–15
# messages including tool_results) without bloating on tool-heavy
# turns.
AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS = 3


@dataclass
class Runtime:
    config: Dict[str, Any]
    registry: CapabilityRegistry
    event_bus: EventBus
    workspace_root: Path
    _sessions: Dict[str, SessionState] = field(default_factory=dict)
    _background_agents: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _project_id: str | None = None
    _ask_user_callbacks: list = field(default_factory=list)
    _send_file_callbacks: list = field(default_factory=list)
    _agent_concurrency: Optional[asyncio.Semaphore] = field(default=None)
    _agent_max_history: int = 50
    # Injectable storage (SDK): defaults to the filesystem-backed FileStorage
    # for CLI/desktop back-compat; library users can pass InMemoryStorage (or
    # any Storage) to run with no ~/.ziva on disk. Stored as the class/instance
    # whose methods perform the actual session/message/project/automation I/O.
    storage: Any = field(default=None)
    # Injectable permission manager (SDK): defaults to the process-wide
    # singleton (get_permission_manager) so CLI/desktop behaviour is unchanged;
    # library users can pass a fresh PermissionManager to avoid global state.
    permission_manager: Optional[PermissionManager] = field(default=None)
    # Signature of the last skill index we pushed to the frontend. Compared
    # on each ``build_skill_index()`` call so we only emit a
    # ``skill_index_changed`` SSE event when the on-disk SKILL.md tree
    # actually changed — the frontend caches the index in module-level
    # memory otherwise (see web/src/modals/skills.ts) and would never see
    # new skills until a hard reload.
    _last_skill_index_sig: str | None = field(default=None)
    # 缓存 build_skill_index 结果的 dict 视图（name → entry），
    # 供 read_skill 等"按 name 单次查找"的调用方 O(1) 命中。
    # 每次 build_skill_index 返回新 list 时，这里也会刷新，
    # 与 sig 缓存同步：sig 不变只会跳过 SSE 但仍然走 build → 重建 list。
    _skill_by_name: dict[str, dict] | None = field(default=None)

    def __post_init__(self) -> None:
        if self._agent_concurrency is None:
            max_conc = int(self.config.get("spawn", {}).get("max_concurrency", 20))
            self._agent_concurrency = asyncio.Semaphore(max_conc)
        self._agent_max_history = int(self.config.get("spawn", {}).get("max_history", 50))
        # Apply injection defaults lazily so both Runtime.create() (app path,
        # no storage kwarg) and Runtime.from_config() (SDK path, explicit
        # storage=InMemoryStorage()) land on the right instance.
        if self.storage is None:
            self.storage = FileStorage
        if self.permission_manager is None:
            self.permission_manager = get_permission_manager()

    def _prune_background_agents(self) -> None:
        """Keep only the most recent _agent_max_history finished agents."""
        finished = [
            (aid, a) for aid, a in self._background_agents.items()
            if a.get("status") in ("completed", "failed", "cancelled")
        ]
        finished.sort(key=lambda x: x[1].get("finished_at", 0))
        excess = len(finished) - self._agent_max_history
        for aid, _ in finished[:max(0, excess)]:
            self._background_agents.pop(aid, None)

    @property
    def project_id(self) -> str:
        if self._project_id is None:
            self._project_id = _project_hash(self.workspace_root)
            # Initialize project metadata
            self.storage.save_project(self._project_id, {
                "id": self._project_id,
                "path": str(self.workspace_root),
                "time": {
                    "created": int(time.time() * 1000),
                    "updated": int(time.time() * 1000),
                },
            })
        return self._project_id

    def _get_session(self, session_id: str) -> SessionState:
        if session_id not in self._sessions:
            sess = SessionState(project_id=self._project_id)
            # Pull the session's saved model_name from disk so the first
            # turn after app restart — or the first turn after the user
            # picks a model in the dropdown before sending the first
            # message — uses the right adapter. The frontend writes
            # `model_name` via PATCH /sessions/{sid}; the in-memory state
            # here mirrors that on first access.
            try:
                # The session may live in a different workspace than the
                # currently-focused one (e.g. an automation's backing
                # session created in workspace A, running while the user has
                # switched to B). Search known workspaces for its REAL
                # project so we read the right file and so
                # _resolve_project_id returns the correct pid for all
                # subsequent disk ops — otherwise chat()'s "create if
                # missing" branch would write a NEW session file in the
                # wrong project, leaking the backing session into the sidebar.
                pid = self._find_session_project(session_id)
                meta = self.storage.get_session(pid, session_id) or {}
                saved = meta.get("model_name")
                if isinstance(saved, str) and saved:
                    # Heal "provider|model" values written by an older client
                    # that persisted the composite dropdown value as model_name.
                    if "|" in saved:
                        pn, _, mn = saved.partition("|")
                        sess.model_name = mn or saved
                        if pn and not sess.provider_name:
                            sess.provider_name = pn
                    else:
                        sess.model_name = saved
                saved_provider = meta.get("provider_name")
                if isinstance(saved_provider, str) and saved_provider:
                    sess.provider_name = saved_provider
                saved_effort = meta.get("thinking_mode")
                if isinstance(saved_effort, str) and saved_effort:
                    sess.thinking_mode = saved_effort
                saved_ws = meta.get("workspace_root")
                if isinstance(saved_ws, str) and saved_ws:
                    sess.workspace_root = saved_ws
                    sess.project_id = _project_hash(Path(saved_ws))
                elif pid != self._project_id:
                    sess.project_id = pid
                # Restore the is_automation marker so backing sessions
                # created before this runtime started still get their
                # chat events suppressed on subsequent runs. Without
                # this, restarting the server would re-leak automation
                # streaming into the user's active session.
                if meta.get("is_automation") is True:
                    sess.is_automation = True
            except Exception:
                # Disk read is best-effort; fall through with model_name=None
                # so chat() falls back to the runtime config.
                pass
            self._sessions[session_id] = sess
        return self._sessions[session_id]

    def _find_session_project(self, session_id: str) -> str:
        """Find the project_id that actually owns this session by searching
        known workspace directories on disk. Falls back to the runtime's
        current project_id. Needed because a session may belong to a
        different workspace than the currently-focused one."""
        from pathlib import Path
        import json as _json
        workspaces = [str(self.workspace_root)]
        try:
            rp = Path.home() / ".ziva" / "recent_workspaces.json"
            if rp.exists():
                data = _json.loads(rp.read_text())
                if isinstance(data, list):
                    workspaces += [str(p) for p in data if p]
        except Exception:
            pass
        seen: set = set()
        for ws in workspaces:
            if not ws or ws in seen:
                continue
            seen.add(ws)
            try:
                pid = _project_hash(Path(ws))
                if self.storage.get_session(pid, session_id):
                    return pid
            except Exception:
                continue
        return self.project_id

    def _resolve_project_id(self, session_id: str) -> str:
        """Resolve project_id from session context, falling back to global."""
        session = self._sessions.get(session_id)
        if session and session.project_id:
            return session.project_id
        return self.project_id

    def build_skill_index(self) -> list[dict]:
        """Scan skill directories on demand and return a compact index.

        Called by the /skills API endpoint. Re-scans every request so newly
        installed skills appear without a restart, and machines without these
        particular skills just see an empty list — no stale baked-in index
        persisted to the config file.

        Walks ``extra_paths`` with ``followlinks=True`` so symlinked skill
        directories (e.g. ``~/.ziva/skills/<name>`` → ``~/.claude/skills/<name>``)
        are still picked up; ``Path.rglob`` does not follow symlinks by
        default, which silently skipped those entries.
        """
        import yaml  # local import: this method is on a slow path

        def _inside(child: str, root: str) -> bool:
            try:
                Path(child).relative_to(root)
                return True
            except ValueError:
                return False

        # Resolved roots that opt a directory in as a skill source.
        # A symlinked skill directory (e.g. ``~/.ziva/skills/<x>`` →
        # ``~/.claude/skills/<x>``) is allowed when its real target
        # lives anywhere under the user's ``$HOME`` — the common
        # pattern is symlinking Claude's official skill tree into a
        # Ziva-readable location to avoid duplicating megabytes of
        # templates. The filter only blocks symlinks that escape
        # HOME entirely (e.g. ``/etc/passwd``) which would otherwise
        # be silently adopted as a skill.
        home = str(Path.home().resolve())
        allowed_real_roots: list[str] = [
            str(Path(sp).expanduser().resolve())
            for sp in self.config.get("skill", {}).get("extra_paths", [])
            if Path(sp).expanduser().resolve().exists()
        ]

        def _is_safe_symlink_target(real: str) -> bool:
            # Accept when the symlink target is itself under one of
            # the configured extra_paths, OR when it lives anywhere
            # under the user's HOME (the common Claude-shared-tree
            # pattern). Reject targets that escape HOME — those
            # would let an attacker turn arbitrary directories into
            # Ziva-readable skills.
            if any(_inside(real, r) for r in allowed_real_roots):
                return True
            return _inside(real, home)

        index: list[dict] = []
        seen_dirs: set[str] = set()
        for sp in self.config.get("skill", {}).get("extra_paths", []):
            p = Path(sp).expanduser().resolve()
            if not p.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(p, followlinks=True):
                # Bound depth at 2 (skill_dir / SKILL.md). Going deeper than
                # that just scans references/, templates/, etc. which are not
                # top-level skills.
                rel = os.path.relpath(dirpath, str(p))
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth > 1:
                    dirnames[:] = []
                    continue
                # Avoid double-counting when two extra_paths symlink to the
                # same physical directory.
                real = os.path.realpath(dirpath)
                if real in seen_dirs:
                    dirnames[:] = []
                    continue
                # Drop symlinked skill dirs whose target escapes
                # HOME. See ``_is_safe_symlink_target`` for the
                # rationale: a symlink to a sibling tree under HOME
                # (e.g. ``~/.claude/skills``) is the common
                # Claude-shared-tree pattern and stays allowed; a
                # symlink to ``/etc/...`` or anywhere outside HOME
                # would otherwise be silently adopted.
                if os.path.realpath(dirpath) != dirpath and not _is_safe_symlink_target(real):
                    dirnames[:] = []
                    continue
                seen_dirs.add(real)
                for fn in filenames:
                    if fn != "SKILL.md":
                        continue
                    skill_file = Path(dirpath) / fn
                    try:
                        raw = skill_file.read_text(encoding="utf-8").strip()
                    except (OSError, UnicodeDecodeError):
                        continue
                    if not raw:
                        continue
                    name, desc = skill_file.parent.name, ""
                    meta: dict = {}
                    fm_category: str | None = None
                    if raw.startswith("---"):
                        end = raw.find("\n---", 3)
                        if end > 0:
                            try:
                                fm = yaml.safe_load(raw[3:end]) or {}
                            except yaml.YAMLError:
                                fm = {}
                            if isinstance(fm, dict):
                                if isinstance(fm.get("name"), str):
                                    name = fm["name"].strip()
                                d = fm.get("description")
                                if isinstance(d, str):
                                    desc = d.strip()
                                elif isinstance(d, (int, float, bool)):
                                    desc = str(d)
                                # Explicit category wins over the
                                # keyword-based _categorize_skill heuristic
                                # below — if the author wrote one in the
                                # frontmatter, respect it.
                                if isinstance(fm.get("category"), str):
                                    fm_category = fm["category"].strip()
                                # Pass the raw frontmatter through
                                # verbatim so the viewer can surface
                                # every field (name, description,
                                # category, version, tags, hooks, …)
                                # above the markdown body — mirroring
                                # the on-disk frontmatter layout. The
                                # card preview still shows name +
                                # description via the top-level keys,
                                # so duplicating them in ``meta`` is
                                # the point: the meta block is the
                                # place to see the full description
                                # without the 3-line clamp the card
                                # applies for scanability.
                                meta = dict(fm)
                    index.append({
                        "name": name,
                        # No truncation here — the card preview
                        # clamps to 3 lines via CSS
                        # (``-webkit-line-clamp: 3`` on
                        # ``.skill-card-desc``) and the viewer
                        # renders the full description in the meta
                        # block above the body. Truncating server-
                        # side would force a re-fetch to see the
                        # rest of a long description.
                        "description": desc,
                        "path": str(skill_file),
                        "category": fm_category or _categorize_skill(name, desc),
                        "meta": meta,
                    })
        # Detect changes vs. the last emission and notify the frontend via
        # SSE so the Skills modal can invalidate its module-level cache
        # (see web/src/modals/skills.ts). The signature is intentionally
        # coarse — name + description + path — so trivial whitespace tweaks
        # don't churn the UI, but adding/removing/editing a SKILL.md
        # does. Meta fields are included via ``json.dumps`` (sorted
        # keys) so changing just ``version:`` still fires the event —
        # otherwise the sidebar would keep showing stale frontmatter
        # until a hard reload.
        sig = "|".join(
            f"{s['name']}\0{s.get('description', '')}\0{s['path']}\0"
            f"{json.dumps(s.get('meta') or {}, sort_keys=True, ensure_ascii=False)}"
            for s in index
        )
        if sig != self._last_skill_index_sig:
            self._last_skill_index_sig = sig
            # Fire-and-forget; both call sites (turn prompt and /skills
            # HTTP handler) are async, but guard against sync callers
            # (tests, CLI startup) where get_running_loop() raises.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(self._publish_skill_index_changed(len(index)))
        # 刷新 dict 视图（同名 skill 取第一个出现，保持 first-match 语义）
        self._skill_by_name = {s["name"]: s for s in index}
        return index

    def get_skill(self, name: str) -> dict | None:
        """O(1) skill lookup by name.

        首次调用会主动触发一次 build_skill_index() 填充 _skill_by_name 缓存。
        之后每次 build_skill_index() 调用（turn 提示词构建 / /skills API）
        都会在末尾无条件刷新 _skill_by_name —— sig 只控制 SSE 通知，
        不跳过 list 重建和 dict 刷新，所以缓存永远不会过期。
        实际开销：dict 索引一次构建 O(n)，单次 read_skill 查询 O(1)。
        """
        if self._skill_by_name is None:
            self.build_skill_index()
        return (self._skill_by_name or {}).get(name)

    async def _publish_skill_index_changed(self, count: int) -> None:
        """Push a ``skill_index_changed`` SSE event so the UI can drop its
        cached skill list. Goes through the same EventBus as per-session
        events; the ``/events`` SSE endpoint forwards everything to a
        single global queue, so the frontend sees it via its existing
        SSEPool.

        The sentinel ``session_id="_global"`` keeps this event out of any
        per-session history bucket — it should not appear in a session's
        history replay. The frontend recognises the type field on the
        payload and routes it before any session-id-based dispatch.
        """
        await self.event_bus.publish("_global", {
            "type": "skill_index_changed",
            "count": count,
            "ts": int(time.time() * 1000),
        })

    def _read_last_usage(self, session_id: str) -> Dict[str, int] | None:
        """Read the most recent API-reported prompt_tokens from session metadata on disk.

        This is the authoritative overflow signal — it reflects what the model
        provider actually billed for the previous round, not a local heuristic
        estimate. Returned by `update_session_usage` after every `round_complete`.
        """
        try:
            meta = self.storage.get_session(self._resolve_project_id(session_id), session_id) or {}
            return meta.get("last_usage")
        except Exception:
            return None

    def _chatmessage_to_record(self, m: ChatMessage) -> Dict[str, Any]:
        """Serialize a ChatMessage to the dict format FileStorage uses."""
        record: Dict[str, Any] = {
            "role": m.role,
            "content": m.content,
            "tool_call_id": m.tool_call_id,
            "name": m.name,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in m.tool_calls
            ],
        }
        # Preserve thinking + reasoning so reloads can show the same thinking
        # card the user saw during streaming. Without these, auto-compact would
        # silently strip reasoning from the post-compact tail and the UI's
        # `renderMessages` would never get the data to populate the thinking
        # cards. Matches the manual `/compact` path in `_apply_post_compact`.
        if getattr(m, "reasoning_content", None):
            record["reasoning_content"] = m.reasoning_content
        if getattr(m, "reasoning_signature", None):
            record["reasoning_signature"] = m.reasoning_signature
        if getattr(m, "_compaction_summary", False):
            record["_compaction_summary"] = True
        if getattr(m, "_hidden", False):
            record["_hidden"] = True
        return record

    def _apply_compact_to_disk(
        self,
        session_id: str,
        working_before: List[ChatMessage],
        new_working: List[ChatMessage],
        keep_last_assistant_turns: int,
    ) -> None:
        """Persist a compact result to disk and refresh in-memory runtime state.

        On-disk layout after this call:  [preserved_old, new_summary, ...to_keep]
        where preserved_old is the portion of the current on-disk that came
        before the new to_keep. The LLM-visible view (used by SessionState.history
        for the next turn) is just `new_working`.

        This is the runtime-side equivalent of the server's `_apply_post_compact`
        used by the manual `/compact` endpoint — same on-disk shape, same
        last_usage refresh, just no server-side session-store update.
        """
        # Read current on-disk as dicts (FileStorage format) and serialize the
        # new tail to dicts so the result is a uniform list of records.
        current_on_disk = list(self.storage.get_messages(self._resolve_project_id(session_id), session_id) or [])
        new_working_dicts = [self._chatmessage_to_record(m) for m in new_working]

        last_summary_idx = find_last_summary_idx(current_on_disk)
        cutoff = find_cutoff_in_llm_visible(working_before, keep_last_assistant_turns)
        new_on_disk = compose_post_compact_on_disk(
            current_on_disk, last_summary_idx, cutoff, new_working_dicts
        )
        self.storage.replace_messages(self._resolve_project_id(session_id), session_id, new_on_disk)

        # Update SessionState.history to the LLM-visible view (= new_working).
        # The next turn loads from this when SessionState.history is empty, so
        # we need it to reflect the compact result, not the bloated pre-compact
        # state.
        session = self._get_session(session_id)
        session.history = list(new_working)

        # Refresh last_usage from the post-compact LLM-visible view. This
        # resets the overflow threshold so the next round's start-of-round
        # hook doesn't immediately re-fire.
        new_prompt_tokens = estimate_tokens(new_working)
        new_usage = {
            "prompt_tokens": new_prompt_tokens,
            "completion_tokens": 0,
            "total_tokens": new_prompt_tokens,
        }
        self.update_session_usage(session_id, new_usage)

    # -- graceful restart ------------------------------------------------------

    def _running_turn_count(self) -> int:
        """Count how many sessions currently have an in-flight turn task.

        Used by IM ``/restart`` to decide whether the request can be honored
        immediately or must wait for the running turn to drain. The check
        mirrors the canonical ``session.turn_task is not None and not
        session.turn_task.done()`` pattern used in ``desktop_api.server`` and
        ``im_bridge.bridge``.

        Returns 0 when no sessions are loaded (e.g. CLI idle) or all turns
        have finished.
        """
        n = 0
        for sid, session in list(self._sessions.items()):
            task = getattr(session, "turn_task", None)
            if task is not None and not task.done():
                n += 1
        return n

    async def _graceful_execvp(
        self,
        *,
        reason: str = "manual",
        wait_timeout: float = 10.0,
        pending_payload: list[dict] | None = None,
        pending_payload_path: Path | None = None,
    ) -> None:
        """Replace the current process with a fresh ziva instance.

        Used by IM ``/restart`` (and other remote-triggered restarts) so that
        the new process picks up config / skill / code changes without losing
        the PID, IM connections in flight, or the operator's confidence in
        what the daemon is doing.

        The function does not return under normal flow — ``os.execvp``
        replaces the current process image. On the rare path where it does
        return (write failure, signal interrupt), the caller can decide
        whether to raise or log.

        Args:
            reason: short label for logs/audit (``"manual"`` for IM ``/restart``,
                ``"config_change"`` for future config-watcher, etc.).
            wait_timeout: seconds to wait for in-flight turns to finish
                before giving up. ``/restart`` waits this long then proceeds
                anyway — aborting is worse than losing ~1 turn.
            pending_payload: optional list of dicts to persist as JSON so the
                new process can pick up where we left off (e.g. IM restart
                notifications — see ``docs/im-restart.md`` §10).
            pending_payload_path: where to write ``pending_payload``. Defaults
                to ``~/.ziva/.restart_pending.json``.
        """
        # 1. Wait briefly for in-flight turns so the user doesn't lose work.
        #    Skip if no event loop is running (e.g. from a sync shutdown).
        deadline = time.monotonic() + max(0.0, wait_timeout)
        try:
            while time.monotonic() < deadline:
                if self._running_turn_count() == 0:
                    break
                await asyncio.sleep(0.1)
        except RuntimeError:
            # No running loop (CLI shutdown path) — proceed without waiting.
            pass

        # 2. Persist any pending payload the new process should know about.
        if pending_payload is not None:
            path = pending_payload_path or (Path.home() / ".ziva" / ".restart_pending.json")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(pending_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                logger.exception("runtime: failed to write restart pending payload to %s", path)

        # 3. Replace current process. argv[0] is preserved; argv[1:] is the
        #    same as what launched us. This keeps the supervisor's PID table
        #    stable so the parent doesn't think we crashed.
        logger.warning(
            "runtime: graceful execvp (reason=%s, in_flight_turns=%d)",
            reason,
            self._running_turn_count(),
        )
        try:
            os.execvp(sys.executable, [sys.executable, *sys.argv])
        except Exception:
            logger.exception("runtime: execvp failed; process will exit instead")
            # Surface the failure to the caller — without execvp happening,
            # we cannot pretend the restart succeeded.
            raise

    @classmethod
    def create(
        cls,
        *,
        workspace_root: Path,
        global_config_path: Path | None = None,
        session_override: Dict[str, Any] | None = None,
    ) -> "Runtime":
        config = load_effective_config(global_config_path, session_override)
        registry = CapabilityRegistry()

        # Workspace plugins — expand ~ before joining so that absolute
        # user paths (e.g. ``~/.ziva/plugins``) are not treated as literal
        # subdirectories of the workspace.
        plugin_paths: list[Path] = []
        for p in config.get("plugin", {}).get("paths", ["./plugins"]):
            pp = Path(p).expanduser()
            plugin_paths.append(pp if pp.is_absolute() else workspace_root / pp)
        # Packaged app (PyInstaller): the workspace won't have a plugins/
        # dir, so also load the plugins bundled into the app bundle (shipped
        # via PyInstaller datas to _MEIPASS/plugins).
        import sys as _sys
        if getattr(_sys, "frozen", False):
            _bundled = Path(getattr(_sys, "_MEIPASS", Path(__file__).resolve().parent)) / "plugins"
            if _bundled.is_dir() and _bundled not in plugin_paths:
                plugin_paths.append(_bundled)
        load_plugins(plugin_paths, registry, config)

        # Skills live under their own `skill:` block. Scan
        # `skill.extra_paths` (which defaults to the well-known global
        # directories `~/.ziva/skills` and `~/.agents/skills` so
        # canonical skills like agent-browser work out of the box).
        # Users can add or remove roots via `~/.ziva/config.yaml`.
        extra_skill_paths = config.get("skill", {}).get("extra_paths", [])
        for sp in extra_skill_paths:
            p = Path(sp).expanduser().resolve()
            if p.exists():
                load_plugins([p], registry, config)

        runtime = cls(
            config=config,
            registry=registry,
            event_bus=EventBus(),
            workspace_root=workspace_root,
        )

        # Seed the runtime's own permission manager (defaults to the
        # process-wide singleton via __post_init__) with config-approved rules.
        perm_config = config.get("permissions", {})
        if perm_config:
            runtime.permission_manager.set_approved_rules(from_config(perm_config))

        return runtime

    @classmethod
    def from_config(
        cls,
        config: Dict[str, Any],
        workspace_root: Path | str | None = None,
        *,
        storage: Any = None,
        registry: Optional[CapabilityRegistry] = None,
        permission_manager: Optional["PermissionManager"] = None,
        load_default_plugins: bool = True,
        extra_tools: Optional[list] = None,
        extra_skill_paths: Optional[List[str]] = None,
    ) -> "Runtime":
        """Library-friendly constructor (the path ``ziva.Agent`` takes).

        Unlike :meth:`create`, this accepts an already-built config dict (no
        ``~/.ziva/config.yaml`` required), an optional injectable storage
        (pass ``InMemoryStorage()`` to run with no filesystem), and an
        optional injectable ``PermissionManager``. Filesystem plugin discovery
        only runs when ``load_default_plugins`` is True *and* the configured
        plugin directories actually exist on disk — so a pure in-process
        ``Agent`` doesn't depend on a checkout layout. ``extra_tools`` are
        registered after plugin load so they can override or supplement the
        defaults.
        """
        validate_config(config)

        ws = Path(workspace_root).expanduser() if workspace_root is not None else Path.cwd()
        reg = registry if registry is not None else CapabilityRegistry()

        if load_default_plugins:
            plugin_paths: List[Path] = []
            for p in config.get("plugin", {}).get("paths", ["./plugins"]):
                pp = Path(p).expanduser()
                candidate = pp if pp.is_absolute() else ws / pp
                if candidate.exists():
                    plugin_paths.append(candidate)
            if plugin_paths:
                load_plugins(plugin_paths, reg, config)
            skill_paths = list(extra_skill_paths or []) + list(config.get("skill", {}).get("extra_paths", []))
            for sp in skill_paths:
                p = Path(sp).expanduser().resolve()
                if p.exists():
                    load_plugins([p], reg, config)

        runtime = cls(
            config=config,
            registry=reg,
            event_bus=EventBus(),
            workspace_root=ws,
            storage=storage,                        # None → FileStorage via __post_init__
            permission_manager=permission_manager,  # None → singleton via __post_init__
        )

        perm_config = config.get("permissions", {})
        if perm_config:
            runtime.permission_manager.set_approved_rules(from_config(perm_config))

        for tool in extra_tools or []:
            runtime.register_tool(tool)

        return runtime

    def register_tool(self, tool: Any, tool_id: str | None = None) -> None:
        """Register a Tool-like object (``spec()`` + ``async run(args, ctx)``).

        SDK entry point: lets library users add tools without a manifest or a
        plugins/ directory. Derives the capability id from the spec name and
        records an empty permissions manifest so the approval gate treats it
        like any other tool.
        """
        spec = tool.spec() if hasattr(tool, "spec") else tool["spec"]
        name = tool_id or spec.get("name") or spec.get("id") or f"tool_{id(tool)}"
        cap_id = name if "." in name else f"tool.{name}"
        manifest = {
            "id": cap_id,
            "type": "tool",
            "permissions": dict(spec.get("permissions") or {}),
        }
        self.registry.register(cap_id, "tool", tool, manifest)

    async def chat(self, messages: Iterable[ChatMessage], session_id: str | None = None) -> ChatResult:
        sid = session_id or str(uuid.uuid4())
        new_messages = list(messages)
        ctx = RuntimeContext(session_id=sid, config=self.config)
        ctx.metadata["_runtime"] = self
        session = self._get_session(sid)
        # Resolve paths against the session's OWN workspace (the one it was
        # created in), not runtime.workspace_root — the latter tracks the
        # currently-focused workspace and would be wrong if the user switches
        # workspaces and then runs a session created elsewhere. Falls back to
        # the runtime workspace for sessions created before this field existed.
        ctx.metadata["_workspace_root"] = session.workspace_root or str(self.workspace_root)

        # Load session history from disk if not already loaded.
        # Serialize the check+load+extend under a per-session lock so that
        # two concurrent chat() calls for the same session cannot both see
        # an empty history and duplicate the loaded messages.
        async with session.load_lock:
            if not session.history:
                loaded = self._load_session_from_disk(sid)
                if loaded:
                    session.history.extend(loaded)
                else:
                    # Create the session on disk only if it doesn't already
                    # exist. The session may have been created earlier via
                    # POST /sessions and then updated via PATCH /sessions
                    # (e.g. to set model_name); we must not overwrite that
                    # metadata just because no messages have been written
                    # yet. storage.create_session does a full-file
                    # write_json, so calling it on an existing session
                    # would clobber model_name / name / etc.
                    if self.storage.get_session(self._resolve_project_id(sid), sid) is None:
                        self.storage.create_session(self._resolve_project_id(sid), {
                            "id": sid,
                            "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
                        })

        # Append new user messages to session history
        session = self._get_session(sid)
        # Safety net: ensure any orphaned assistant+tool_calls from a previous
        # cancelled or crashed turn are covered by synthetic tool_result messages
        # before we add the new user messages. This keeps the in-memory history
        # and the JSONL on disk in sync and avoids the provider 400 error about
        # unmatched tool_call_ids.
        sanitized = self._sanitize_orphaned_tool_calls(sid, list(session.history))
        session.history[:] = sanitized
        session.history.extend(new_messages)
        for msg in new_messages:
            self._persist_message(sid, msg)

        await self._run_hooks("before_turn", {"messages": [m.__dict__ for m in new_messages]}, ctx)
        await self._emit(sid, {"type": "turn_start"})

        rendered_messages = self._apply_prompt(list(session.history), ctx)

        # Build the per-turn model config first, so the image-resolution
        # branch below sees the SAME model that will actually be used to
        # call the API. session.model_name (if set) overrides the runtime
        # config's model.name; the rest of the runtime config (max_tokens,
        # thinking_mode, providers) is unchanged.
        #
        # The merge is shallow on the `model:` block — max_tokens /
        # thinking_mode stay runtime-level. Building this here (rather
        # than after _resolve_image_paths) is what lets a per-session
        # model switch from a vision-capable model to a non-vision one
        # correctly turn historical images into text references, and
        # vice versa.
        model_cfg = dict(self.config.get("model", {}))
        if session.model_name:
            model_cfg["name"] = session.model_name
        if session.provider_name:
            # Pin the provider so _find_provider_for_model resolves the right
            # one when the same model name is listed under multiple providers.
            model_cfg["provider_name"] = session.provider_name
        if session.thinking_mode:
            # Per-session effort override wins over the global thinking_mode.
            model_cfg["thinking_mode"] = session.thinking_mode
        turn_config = dict(self.config)
        turn_config["model"] = model_cfg
        turn_adapter = _create_adapter(turn_config)

        # Resolve any `image_url` blocks whose url is a local file path
        # (e.g. a user-attached screenshot dropped to ~/.ziva/.../clip-123.png).
        # Vision-capable models get a base64 data URL; non-vision models
        # get a plain text reference to the path (so we don't burn tokens
        # on a blob the model can't interpret). The original
        # `rendered_messages` history keeps the path form either way so
        # reloads stay cheap; only the per-turn copy sent to the
        # provider is rewritten. The capability lookup is parameterized
        # on the per-turn model name, NOT self._current_model_supports_image,
        # so it follows session.model_name.
        rendered_messages = _resolve_image_paths(
            rendered_messages,
            model_supports_image=self._model_supports_image(model_cfg["name"]),
        )

        # Run unified streaming loop; events are emitted to event bus automatically
        final_content = ""
        final_reasoning_content = ""
        final_reasoning_signature = None
        final_usage = None
        final_finish_reason = "stop"
        cancelled = False
        # Pass the session's cancel token so the polling checkpoints inside
        # _run_model_tool_loop (between streamed deltas / at each round) can
        # react to "stop" immediately. desktop_api's create_turn stashes the
        # CancellationToken on session.cancel_token; without forwarding it
        # here, those checkpoints never fire and cancellation only happens
        # via task.cancel()'s CancelledError at an await boundary — which is
        # why stop felt slow (the current tool had to finish first).
        async for event in self._run_model_tool_loop(rendered_messages, sid, ctx, cancellation_token=session.cancel_token, model_cfg=model_cfg, model_adapter=turn_adapter):
            if event.get("type") == "model_response":
                final_content = event.get("content", "")
                final_reasoning_content = event.get("reasoning_content") or ""
                final_reasoning_signature = event.get("reasoning_signature") or None
                final_usage = event.get("usage")
                final_finish_reason = event.get("finish_reason", "stop")
            if event.get("type") == "cancelled":
                cancelled = True
                final_finish_reason = "cancelled"
                break

        if cancelled:
            # Cancel may have fired between the assistant tool_calls
            # being persisted (line 848) and the tool_result messages
            # being written (lines 909/920). Without this fix the
            # JSONL on disk ends with an orphan `assistant` + tool_calls
            # and the *next* turn replays it to Anthropic, which rejects
            # with 400 "tool call result does not follow tool call
            # (2013)". Append synthetic tool_result messages for any
            # tool_use without a matching result so the next turn can
            # replay history cleanly, and mirror them back into the
            # in-memory session history so the current process doesn't
            # keep sending stale history to the provider.
            session = self._get_session(sid)
            sanitized = self._sanitize_orphaned_tool_calls(sid, list(session.history))
            session.history[:] = sanitized

            result = ChatResult(
                role="assistant",
                content=final_content or "Turn cancelled by user.",
                model=self.config["model"]["name"],
                usage=final_usage,
                finish_reason="cancelled",
                reasoning_content=final_reasoning_content or None,
                reasoning_signature=final_reasoning_signature,
            )
            await self._emit(sid, {"type": "turn_cancelled"})
            return result

        # The loop already persisted the final assistant message; just construct ChatResult
        result = ChatResult(
            role="assistant",
            content=final_content,
            model=self.config["model"]["name"],
            usage=final_usage,
            finish_reason=final_finish_reason,
            reasoning_content=final_reasoning_content or None,
            reasoning_signature=final_reasoning_signature,
        )
        await self._store_memory(list(session.history), result, ctx)
        await self._run_hooks("after_turn", {"result": result.__dict__}, ctx)
        await self._emit(sid, {"type": "turn_end", "session_id": sid, "result": result.__dict__})
        return result

    async def chat_with_events(self, messages: Iterable[ChatMessage], session_id: str | None = None) -> tuple[str, ChatResult, List[Dict[str, Any]]]:
        sid = session_id or str(uuid.uuid4())
        self.event_bus.clear_history(sid)
        result = await self.chat(messages, session_id=sid)
        return sid, result, self.event_bus.history(sid)

    async def chat_streaming(
        self,
        messages: Iterable[ChatMessage],
        session_id: str | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yields events in real-time as the model streams tokens and tools execute."""

        sid = session_id or str(uuid.uuid4())
        new_messages = list(messages)
        ctx = RuntimeContext(session_id=sid, config=self.config)
        ctx.metadata["_runtime"] = self
        session = self._get_session(sid)
        # Mirror of chat(): use the session's own workspace (creation-time),
        # not runtime.workspace_root.
        ctx.metadata["_workspace_root"] = session.workspace_root or str(self.workspace_root)

        async with session.load_lock:
            if not session.history:
                loaded = self._load_session_from_disk(sid)
                if loaded:
                    session.history.extend(loaded)
                else:
                    # Don't clobber an existing session file with an empty
                    # record — see the matching comment in chat().
                    if self.storage.get_session(self._resolve_project_id(sid), sid) is None:
                        self.storage.create_session(self._resolve_project_id(sid), {
                            "id": sid,
                            "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
                        })
            # Safety net for sessions whose JSONL ends with an orphaned
            # assistant+tool_calls message. Covers:
            #  - Cancels that fired before _sanitize_orphaned_tool_calls
            #    ran (e.g. session was force-killed mid-cancel).
            #  - Sessions from older ziva versions that predate the fix.
            #  - Any other crash path that left history inconsistent.
            # The function appends synthetic tool_result messages to
            # both the in-memory history and the JSONL; replaying to the
            # next turn is then wire-format-safe. We mirror its returned
            # list back into session.history so the in-memory copy and
            # the JSONL stay in sync.
            sanitized = self._sanitize_orphaned_tool_calls(sid, list(session.history))
            session.history[:] = sanitized

        session.history.extend(new_messages)
        for msg in new_messages:
            self._persist_message(sid, msg)

        await self._run_hooks("before_turn", {"messages": [m.__dict__ for m in new_messages]}, ctx)
        yield {"type": "turn_start", "session_id": sid}

        rendered_messages = self._apply_prompt(list(session.history), ctx)

        last_exc: Exception | None = None
        # Snapshot model config and adapter at turn start so a mid-turn
        # model change (global or per-session) doesn't invalidate this
        # turn. Same merge as in chat(): session.model_name, if set,
        # overrides the runtime config's model.name.
        model_cfg = dict(self.config.get("model", {}))
        if session.model_name:
            model_cfg["name"] = session.model_name
        if session.provider_name:
            # Pin the provider so _find_provider_for_model resolves the right
            # one when the same model name is listed under multiple providers.
            model_cfg["provider_name"] = session.provider_name
        if session.thinking_mode:
            # Per-session effort override wins over the global thinking_mode.
            model_cfg["thinking_mode"] = session.thinking_mode
        turn_config = dict(self.config)
        turn_config["model"] = model_cfg
        turn_adapter = _create_adapter(turn_config)
        try:
            for attempt in (1, 2):
                try:
                    async for event in self._run_model_tool_loop(rendered_messages, sid, ctx, cancellation_token, model_cfg=model_cfg, model_adapter=turn_adapter):
                        yield event
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt == 1 and self._is_retryable_provider_error(exc):
                        # Stream control event — NOT a toast. Tells the
                        # client "the partial text on screen from the
                        # previous attempt is invalidated; drop it." The
                        # client must remove its current streaming
                        # assistant block (and any in-flight tool
                        # cards) so the new attempt's deltas land in a
                        # fresh block.
                        #
                        # Note: disk / history are NOT touched. The
                        # failure happened during streaming, *before*
                        # model_response was yielded, so
                        # `_run_model_tool_loop` never reached the
                        # history.append / _persist_message calls —
                        # there's nothing to roll back on the server.
                        yield {
                            "type": "stream_reset",
                            "attempt": 2,
                            "reason": str(exc)[:200],
                            "class": exc.__class__.__name__,
                        }
                        continue
                    yield {"type": "turn_error", "session_id": sid, "error": str(exc), "class": exc.__class__.__name__}
                    break
        finally:
            yield {"type": "turn_end", "session_id": sid}

    async def _run_model_tool_loop(
        self,
        messages: List[ChatMessage],
        session_id: str,
        ctx: RuntimeContext,
        cancellation_token: CancellationToken | None = None,
        *,
        model_cfg: Dict[str, Any],
        model_adapter: "ModelAdapter",
    ) -> AsyncIterator[Dict[str, Any]]:

        await self._connect_mcp_if_needed(session_id)
        is_sub = ctx.metadata.get("_subagent", False) if ctx else False
        sub_call_id = ctx.metadata.get("_subagent_call_id") if ctx else None
        # Both model_cfg and model_adapter MUST be passed in. Callers that
        # used to omit them were silently using the runtime's global
        # `config["model"]` — which ignored any per-session `model_name`
        # pinned via PATCH /sessions. Today the only known callers are
        # `chat()` / `chat_streaming()` (turn entry points) and
        # `spawn_agent` (sub-agent entry point); both now build a
        # session-aware turn_config and pass it explicitly.
        raw_max = self.config.get("tool", {}).get("max_rounds", 10)
        max_rounds = None if raw_max in (0, None, "0") else int(raw_max or 10)
        context_window = int(self.config.get("memory", {}).get("context_window_tokens", 200000) or 200000)
        working = list(messages)
        api_tools = self._build_tools_param(ctx)

        def _flag(payload: dict) -> dict:
            if is_sub:
                payload["_subagent"] = True
            return payload

        # Handle slash commands (e.g., /compact)
        _last_user_text = working[-1].content if working and working[-1].role == "user" else ""
        if isinstance(_last_user_text, list):
            _last_user_text = " ".join(p.get("text", "") for p in _last_user_text if isinstance(p, dict) and p.get("type") == "text")
        if _last_user_text.strip() == "/compact":
            summary_list = await compact_messages(
                working, context_window, model_cfg["name"], model_adapter
            )
            working = summary_list
            had_summary = any(m._compaction_summary for m in working)
            event = {"type": "context_compacted", "round": 0, "note": "Manual compact triggered by /compact"}
            yield _flag(event)
            await self._emit(session_id, event)
            # If compact_messages had nothing to compress (e.g. only one user
            # message, or the model returned empty), surface a clear noop
            # message instead of the misleading "Context has been compacted."
            if had_summary:
                content = "Context has been compacted."
            else:
                content = "Nothing to compact — context is already minimal."
            event = {"type": "model_response", "round": 0, "content": content, "usage": None, "finish_reason": "stop"}
            yield _flag(event)
            await self._emit(session_id, event)
            self._get_session(session_id).history.append(ChatMessage(role="assistant", content=content))
            self._persist_message(session_id, ChatMessage(role="assistant", content=content), is_subagent=is_sub, sub_call_id=sub_call_id)
            return

        round_idx = 0
        while max_rounds is None or round_idx < max_rounds:
            round_idx += 1
            if cancellation_token and cancellation_token.is_cancelled:
                event = {"type": "cancelled", "round": round_idx}
                yield _flag(event)
                await self._emit(session_id, event)
                return

            # ---- Start-of-round auto-compact hook ----
            # Trigger on the API's real prompt_tokens (read from disk after every
            # previous round_complete), not on the local estimate_tokens heuristic.
            # The 0.9 threshold matches the legacy 200k-20k OVERFLOW_BUFFER.
            last_usage = self._read_last_usage(session_id)
            if last_usage and last_usage.get("prompt_tokens", 0) / context_window >= AUTO_COMPACT_THRESHOLD:
                # Decide whether compact is even *possible* before emitting any
                # events. compact_messages needs ≥ keep_last_assistant_turns
                # asst messages in `working` to do a meaningful split; with
                # fewer, the whole conversation is one "tail" and there's
                # nothing to summarize. Skip silently — wait for more rounds
                # to accumulate. No prune fallback either: prune lives on a
                # separate code path (manual `/prune`), keeping this hook's
                # behavior fully predictable.
                asst_indices = [i for i, m in enumerate(working) if m.role == "assistant"]
                if len(asst_indices) >= AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS:
                    yield _flag({"type": "status", "content": "compact", "round": round_idx})
                    await self._emit(session_id, {"type": "status", "content": "compact", "round": round_idx})

                    working_before = working   # captured for on-disk composition
                    summary_list = await compact_messages(
                        working, context_window, model_cfg["name"], model_adapter,
                        keep_last_assistant_turns=AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS,
                    )

                    if summary_list and summary_list is not working:
                        working = summary_list
                        self._apply_compact_to_disk(
                            session_id, working_before, working,
                            keep_last_assistant_turns=AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS,
                        )
                        # Only emit context_compacted when we actually
                        # compacted — otherwise the UI would show a
                        # misleading "Context compacted" toast.
                        event = {"type": "context_compacted", "round": round_idx}
                        yield _flag(event)
                        await self._emit(session_id, event)

            round_start = time.perf_counter()
            base_prompt = self.config.get("prompt", {}).get("system_prompt") or ""
            # Use the session's OWN workspace (creation-time) for the system
            # prompt's `cwd:` line and layered AGENTS.md instructions — not
            # runtime.workspace_root, which tracks the currently-focused
            # workspace and would tell the model the wrong directory when the
            # user switches workspaces and then runs a session created elsewhere.
            session_ws = ctx.metadata.get("_workspace_root") or str(self.workspace_root)
            instructions = load_layered_instructions(Path(str(session_ws)))
            env_context = self._build_environment_context(session_ws, model_cfg)
            parts = [p for p in [base_prompt, instructions] if p]
            parts.append(env_context)
            # Skill 列表注入 — 两个条件都满足才展示:
            #   1. read_skill 工具未被禁（子 agent 的 _allowed_tools 不含
            #      read_skill 时跳过整段，避免"告诉你有这些 skill 但不给你
            #      工具读详情"的半残提示）
            #   2. _allowed_skills 为 None（继承全部）时照常列出；
            #      非 None 时只列白名单内的 skill
            _allowed_tools = ctx.metadata.get("_allowed_tools") if ctx else None
            _allowed_skills = ctx.metadata.get("_allowed_skills") if ctx else None
            if _allowed_tools is None or "read_skill" in _allowed_tools:
                skill_index = self.build_skill_index()
                if _allowed_skills is not None:
                    skill_index = [s for s in skill_index if s["name"] in _allowed_skills]
                if skill_index:
                    skill_lines = ["# Available Skills (use `read_skill` tool to load full details)", ""]
                    for s in skill_index:
                        if s["description"]:
                            skill_lines.append(f"- **{s['name']}**: {s['description']}")
                        else:
                            skill_lines.append(f"- **{s['name']}**")
                    parts.append("\n".join(skill_lines))
            effective_prompt = "\n\n".join(parts)
            # IM channel context: when a turn is driven from the IM bridge,
            # tell the model the user is reaching it REMOTELY through a
            # messenger — the channel is just the transport, the agent is
            # still Ziva — so it doesn't mistake the channel for its identity.
            _im_session = self._get_session(session_id)
            _im_channel = getattr(_im_session, "im_channel", None)
            if _im_channel:
                effective_prompt += (
                    "\n\n## Remote IM session\n"
                    f"The user is reaching you remotely through their **{_im_channel}** "
                    f"messenger. {_im_channel} is only the transport channel, not your "
                    "identity — you are still Ziva running on the user's machine. Your text "
                    "reply and any `send_file` deliveries are sent back through this "
                    f"{_im_channel} chat. Keep replies concise. To send the user any file, "
                    "call `send_file` (delivered as an attachment here); do NOT embed files "
                    "as inline image/file markdown."
                )

            # When a plan exists, remind the model to keep it in sync.
            _plan_session = self._get_session(session_id)
            if _plan_session.plan:
                effective_prompt += (
                    "\n\n## Task Plan\n"
                    "You have an active task plan. After completing each step — or whenever "
                    "any step's status changes — call the `update_plan` tool immediately to "
                    "sync it. Do not save up all updates for the end; keep the plan current "
                    "as you work so progress stays visible."
                )

            thinking_config = None
            # Capability lookup is parameterized on this turn's model
            # name (from the snapshotted model_cfg), NOT the runtime
            # config's model — a session pinned to a non-thinking model
            # via updateSession must not get a thinking block even if
            # the runtime default has thinking_mode: high.
            #
            # Thinking decision matrix (previously the rule was "missing
            # caps → no thinking", which silently disabled thinking for
            # any model entry that didn't explicitly set
            # `capabilities.thinking: true` — kimi-k2.6 was the visible
            # regression):
            #
            #   caps.thinking | thinking_mode    | thinking enabled?
            #   --------------+------------------+------------------
            #   False         | any              | NO   (cap is ceiling)
            #   True/missing  | unset/disabled   | NO   (user opt-out)
            #   True/missing  | low/medium/high  | YES  (user opt-in)
            #
            # `capabilities.thinking: false` is the only cap that can
            # override user intent — it lets a provider/model declare
            # "I do not support thinking", and that ceiling must be
            # honored regardless of what the user toggled. Anything else
            # (caps=True or caps missing) defers to the user: "I support
            # thinking" is permissive, not coercive, and "no opinion"
            # means "let the user decide". This matches the mental model
            # users have when they flip the global thinking switch — the
            # only way for a model to ignore that switch is to declare
            # itself incapable.
            caps = self._capabilities_for_model_name(model_cfg.get("name", ""))
            caps_thinking = caps.get("thinking")
            thinking_mode = model_cfg.get("thinking_mode")
            user_wants_thinking = bool(thinking_mode) and thinking_mode != "disabled"
            if caps_thinking is False:
                thinking_capable = False
            else:
                # caps_thinking is True or missing — defer to the user.
                thinking_capable = user_wants_thinking
            if thinking_capable:
                thinking_config = {
                    "type": "enabled",
                    # When the user opted in via thinking_mode, use that
                    # value as the mode. When the capability authorized
                    # thinking but the user disabled it (`disabled`),
                    # fall back to "medium" so the model still gets a
                    # reasonable default budget hint instead of "disabled".
                    "mode": thinking_mode if user_wants_thinking else "medium",
                    "max_tokens": int(model_cfg.get("max_tokens", 16384)),
                }

            full_content = ""
            full_reasoning_content = ""
            final_tool_calls: List[ToolCallItem] = []
            final_usage: Dict[str, int] | None = None
            final_finish_reason: str | None = None
            final_reasoning_signature: str | None = None

            # Stream from model
            stream = model_adapter.chat_stream(
                working,
                model=model_cfg["name"],
                system_prompt=effective_prompt,
                tools=api_tools if api_tools else None,
                thinking_config=thinking_config,
            )
            async for delta in stream:
                if cancellation_token and cancellation_token.is_cancelled:
                    event = {"type": "cancelled", "round": round_idx}
                    yield _flag(event)
                    await self._emit(session_id, event)
                    return
                if delta.reasoning_content:
                    full_reasoning_content += delta.reasoning_content
                    event = {
                        "type": "reasoning_delta",
                        "content": delta.reasoning_content,
                        "round": round_idx,
                    }
                    yield _flag(event)
                    await self._emit(session_id, event)
                if delta.content:
                    full_content += delta.content
                    event = {"type": "delta", "content": delta.content, "round": round_idx}
                    yield _flag(event)
                    await self._emit(session_id, event)
                if delta.tool_calls:
                    final_tool_calls = delta.tool_calls
                if delta.usage:
                    if final_usage is None:
                        final_usage = dict(delta.usage)
                    else:
                        for k, v in delta.usage.items():
                            if v:
                                if k in final_usage and isinstance(v, (int, float)) and isinstance(final_usage[k], (int, float)):
                                    final_usage[k] = max(final_usage[k], v)
                                else:
                                    final_usage[k] = v
                    event = {"type": "usage_update", "usage": final_usage}
                    yield _flag(event)
                    await self._emit(session_id, event)
                if delta.finish_reason:
                    final_finish_reason = delta.finish_reason
                if delta.reasoning_signature:
                    final_reasoning_signature = delta.reasoning_signature

            event = {
                "type": "model_response",
                "round": round_idx,
                "content": full_content,
                "reasoning_content": full_reasoning_content or None,
                "reasoning_signature": final_reasoning_signature or None,
                "usage": final_usage,
                "finish_reason": final_finish_reason,
            }
            yield _flag(event)
            await self._emit(session_id, event)

            if not final_tool_calls:
                latency_ms = int((time.perf_counter() - round_start) * 1000)
                event = {"type": "round_complete", "round": round_idx, "latency_ms": latency_ms, "usage": final_usage}
                yield _flag(event)
                await self._emit(session_id, event)
                self.update_session_usage(session_id, final_usage)
                # Persist final assistant message
                assistant_msg = ChatMessage(role="assistant", content=full_content)
                if full_reasoning_content:
                    assistant_msg.reasoning_content = full_reasoning_content
                if final_reasoning_signature:
                    assistant_msg.reasoning_signature = final_reasoning_signature
                self._get_session(session_id).history.append(assistant_msg)
                self._persist_message(session_id, assistant_msg, is_subagent=is_sub, sub_call_id=sub_call_id)
                return

            assistant_msg = ChatMessage(role="assistant", content=full_content, tool_calls=final_tool_calls)
            if full_reasoning_content:
                assistant_msg.reasoning_content = full_reasoning_content
            if final_reasoning_signature:
                assistant_msg.reasoning_signature = final_reasoning_signature
            working.append(assistant_msg)
            self._get_session(session_id).history.append(assistant_msg)
            self._persist_message(session_id, assistant_msg, is_subagent=is_sub, sub_call_id=sub_call_id)

            # Parallel tool execution with ordered event emission
            for tc in final_tool_calls:
                event = {"type": "tool_start", "round": round_idx, "tool": tc.name, "arguments": tc.arguments, "call_id": tc.id}
                yield _flag(event)
                await self._emit(session_id, event)

            # Step 2: execute all tools in parallel
            async def _run_tool(tc: ToolCallItem) -> tuple[ToolResult, ToolCallItem]:
                call_ctx = RuntimeContext(
                    session_id=ctx.session_id,
                    config=ctx.config,
                    metadata={**ctx.metadata, "_tool_call_id": tc.id},
                )
                tool_output = await self._execute_tool(ToolCall(name=tc.name, arguments=tc.arguments), call_ctx)
                return tool_output, tc

            try:
                tool_results = await asyncio.gather(*[_run_tool(tc) for tc in final_tool_calls])
            except asyncio.CancelledError:
                # Tool execution was interrupted (user cancelled or task cancelled).
                # Append synthetic tool_result messages for every tool_call so the
                # history stays valid (assistant tool_calls must be followed by
                # tool results). Without this, the next turn would send an
                # unmatched tool_call to the API, triggering a 400 error.
                for tc in final_tool_calls:
                    tool_msg = ChatMessage(role="tool", content="[cancelled]", tool_call_id=tc.id, name=tc.name)
                    working.append(tool_msg)
                    session = self._get_session(session_id)
                    session.history.append(tool_msg)
                    self._persist_message(session_id, tool_msg, is_subagent=is_sub, sub_call_id=sub_call_id)
                raise

            # Step 3: build tool/image messages, persist, then emit tool_end events.
            # We emit tool_end *after* persisting so the frontend can always resolve
            # image messages by their tool_call_id, and parallel tool calls cannot
            # swap image positions. Images are still grouped after tool messages
            # in working history to stay compatible with OpenAI-style APIs.
            deferred_images: list[ChatMessage] = []
            tool_end_events: list[dict] = []
            for tool_output, tc in tool_results:
                is_not_found = tool_output.error and "tool_not_found" in tool_output.text

                # Build SSE output — metadata carries original structured data for frontend
                sse_output = tool_output.metadata.copy()
                sse_output["_text"] = tool_output.text
                sse_output["_error"] = tool_output.error
                if tool_output.images:
                    sse_output = {
                        "type": "image",
                        "metadata": tool_output.metadata,
                        "image_url": tool_output.images[0],
                        "_text": tool_output.text,
                    }

                event = {
                    "type": "tool_end",
                    "round": round_idx,
                    "tool": tc.name,
                    "arguments": tc.arguments,
                    "output": sse_output,
                    "error_class": "tool_not_found" if is_not_found else None,
                    "call_id": tc.id,
                }

                if is_not_found:
                    # Not-found is a terminal error for this round; emit immediately
                    # and do not persist any further tool state.
                    yield _flag(event)
                    await self._emit(session_id, event)
                    event = {"type": "round_complete", "round": round_idx}
                    yield _flag(event)
                    await self._emit(session_id, event)
                    content = f"Tool '{tc.name}' is not available. Please check your configuration."
                    event = {"type": "model_response", "round": round_idx, "content": content, "usage": None, "finish_reason": "tool_not_found"}
                    yield _flag(event)
                    await self._emit(session_id, event)
                    self._get_session(session_id).history.append(ChatMessage(role="assistant", content=content))
                    self._persist_message(session_id, ChatMessage(role="assistant", content=content), is_subagent=is_sub, sub_call_id=sub_call_id)
                    return

                # Image handling
                if tool_output.images:
                    # Put the tool's text output in the tool message itself so the
                    # model sees it as the actual tool result. The image is attached
                    # in a separate hidden user message keyed by tool_call_id so the
                    # frontend can always map it to the right card, even with
                    # parallel tool calls.
                    tool_text = tool_output.text or f"[Image file read: {tool_output.metadata.get('path', 'unknown')}]"
                    tool_msg = ChatMessage(role="tool", content=tool_text, tool_call_id=tc.id, name=tc.name)
                    working.append(tool_msg)
                    self._get_session(session_id).history.append(tool_msg)
                    self._persist_message(session_id, tool_msg, is_subagent=is_sub, sub_call_id=sub_call_id)
                    image_parts: list = [
                        {"type": "text", "text": f"[Image from {tool_output.metadata.get('path', 'file')} | call_id={tc.id}]"},
                        {"type": "image_url", "image_url": {"url": tool_output.images[0]}},
                    ]
                    img_msg = ChatMessage(role="user", content=image_parts, _hidden=True, tool_call_id=tc.id)
                    deferred_images.append(img_msg)
                else:
                    tool_msg = ChatMessage(role="tool", content=tool_output.text, tool_call_id=tc.id, name=tc.name)
                    working.append(tool_msg)
                    self._get_session(session_id).history.append(tool_msg)
                    self._persist_message(session_id, tool_msg, is_subagent=is_sub, sub_call_id=sub_call_id)

                tool_end_events.append(event)

            # Persist all image messages after tool messages so history order is stable.
            for img_msg in deferred_images:
                working.append(img_msg)
                self._get_session(session_id).history.append(img_msg)
                self._persist_message(session_id, img_msg, is_subagent=is_sub, sub_call_id=sub_call_id)

            # Now that every message is on disk, emit tool_end events in order.
            for event in tool_end_events:
                yield _flag(event)
                await self._emit(session_id, event)

            latency_ms = int((time.perf_counter() - round_start) * 1000)
            event = {"type": "round_complete", "round": round_idx, "latency_ms": latency_ms, "usage": final_usage}
            yield _flag(event)
            await self._emit(session_id, event)
            self.update_session_usage(session_id, final_usage)

        # max_rounds reached
        content = "Tool execution reached max_rounds without final answer."
        event = {"type": "model_response", "round": round_idx, "content": content, "usage": None, "finish_reason": "max_rounds"}
        yield _flag(event)
        await self._emit(session_id, event)
        event = {"type": "round_complete", "round": round_idx}
        yield _flag(event)
        await self._emit(session_id, event)
        self._get_session(session_id).history.append(ChatMessage(role="assistant", content=content))
        self._persist_message(session_id, ChatMessage(role="assistant", content=content), is_subagent=is_sub, sub_call_id=sub_call_id)

    async def _connect_mcp_if_needed(self, session_id: str) -> None:
        session = self._get_session(session_id)
        # CONNECTED is terminal — skip.
        # FAILED is intentionally NOT skipped so the next turn retries.
        # NO_CONFIG is also retried: the user may have switched to a workspace
        # that now has MCP configured, or added MCP servers after the session
        # was created. In that case the tools are visible globally but the
        # session would otherwise permanently reject the call.
        if session.mcp_status == MCPConnectStatus.CONNECTED:
            return
        if session.mcp_status == MCPConnectStatus.CONNECTING:
            await session.mcp_connected_event.wait()
            return

        session.mcp_status = MCPConnectStatus.CONNECTING
        session.mcp_connected_event.clear()
        try:
            from ziva.adapters.mcp.client import MCPClient, parse_mcp_config

            mcp_configs = parse_mcp_config(self.config)
            if not mcp_configs:
                session.mcp_status = MCPConnectStatus.NO_CONFIG
                return

            try:
                client = MCPClient(mcp_configs)
                tools = await client.connect_all()
                # Register MCP tools in the registry (only once, route-through)
                for tool in tools:
                    cap_id = f"mcp.{tool._name}"
                    try:
                        self.registry.get(cap_id)
                    except KeyError:
                        self.registry.register(
                            capability_id=cap_id,
                            kind="tool",
                            instance=tool,
                            manifest={
                                "version": "0.0.1",
                                "permissions": {"tool": [tool._name]},
                                "enabled_by_default": True,
                                "path": "mcp",
                            },
                        )
                session.mcp_client = client
                session.mcp_status = MCPConnectStatus.CONNECTED
            except Exception as e:
                # connect_all normally swallows per-server errors, so this
                # branch only fires for catastrophic failures (e.g. the
                # agents.mcp import blowing up). Mark FAILED, not CONNECTED,
                # so the next turn retries instead of permanently skipping.
                logger.error("MCP initialization failed: %s", e)
                session.mcp_status = MCPConnectStatus.FAILED
        finally:
            if session.mcp_status == MCPConnectStatus.CONNECTING:
                # We never got past the try block — treat as FAILED so the
                # next turn retries. Avoids the "stuck in CONNECTING" case
                # if e.g. parse_mcp_config itself raised.
                session.mcp_status = MCPConnectStatus.FAILED
            session.mcp_connected_event.set()

    def _build_tools_param(self, ctx: RuntimeContext | None = None) -> list[dict]:
        """Build OpenAI-format tools list from registered tools, filtered by context."""
        is_subagent = ctx and ctx.metadata.get("_subagent")
        allowed_tools = ctx.metadata.get("_allowed_tools") if ctx else None
        # Transport of the current session. Tools can declare `config.transports`
        # in their manifest (e.g. [im]) to restrict exposure — send_file is
        # IM-only, so desktop sessions won't see it and the model can't mis-call it.
        current_transport: str | None = None
        if ctx:
            _sess = self._get_session(ctx.session_id)
            current_transport = "im" if getattr(_sess, "im_channel", None) else "desktop"
        tools = []
        for tool_rec in self.registry.list_kind("tool"):
            spec = tool_rec.instance.spec()
            name = spec["name"]
            # Sub-agents cannot see parent-only tools in their tool list
            _parent_only = {"spawn_agent", "get_agent_result", "cancel_agent"}
            if is_subagent and name in _parent_only:
                continue
            # Transport-restricted tools (manifest config.transports). Unrestricted
            # tools (no `transports`) are always shown.
            transports = (tool_rec.manifest.get("config") or {}).get("transports")
            if transports and current_transport and current_transport not in transports:
                continue
            # If whitelist specified, only include those tools
            if allowed_tools is not None and name not in allowed_tools:
                continue
            # Dynamically enrich spawn_agent for parent agents: list all
            # configured agent names as enum + descriptions
            if name == "spawn_agent" and not is_subagent:
                agents_cfg = self.config.get("agents", {})
                agent_names = list(agents_cfg.keys())
                if agent_names:
                    spec = dict(spec)
                    spec["input_schema"] = dict(spec.get("input_schema") or {})
                    spec["input_schema"]["properties"] = dict(spec["input_schema"].get("properties") or {})
                    agent_prop = dict(spec["input_schema"]["properties"].get("agent") or {})
                    agent_prop["enum"] = agent_names
                    spec["input_schema"]["properties"]["agent"] = agent_prop
                    lines = []
                    for an, ad in agents_cfg.items():
                        desc = ad.get("description") or (ad.get("instructions", "") or "")[:120]
                        lines.append(f"  - {an}: {desc}")
                    base_desc = spec.get("description", "").split("\n\nAvailable agents:")[0]
                    spec["description"] = base_desc + "\n\nAvailable agents:\n" + "\n".join(lines)
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return tools

    async def _emit(self, session_id: str, event: Dict[str, Any]) -> None:
        session = self._get_session(session_id)
        # Automation backing sessions run in the background and must not
        # leak streaming into another session's chat UI. We suppress every
        # intermediate chat event (delta / tool_start / tool_end /
        # model_response / reasoning_delta / ask_user_question /
        # permission_request / turn_start / turn_end / turn_error /
        # stream_reset / round_complete / context_compacted / ...) and
        # let only the dedicated `automation_run` summary through to
        # subscribers — that's what the Automations panel listens for.
        # The chat history itself is still persisted on disk so the run
        # is fully reproducible; only the live broadcast is suppressed.
        if session.is_automation and event.get("type") != "automation_run":
            session.event_seq += 1
            return
        session.event_seq += 1
        seq = session.event_seq
        payload = {"session_id": session_id, "seq": seq, "ts": int(time.time() * 1000)}
        payload.update(event)
        await self.event_bus.publish(session_id, payload)

    def on_ask_user(self, callback) -> None:
        """Register a callback for ask_user_question events.

        The callback receives (session_id, question, options, call_id)
        and should call set_user_answer() to resolve the question.
        Used by the REPL to handle ask_user inline.
        """
        self._ask_user_callbacks.append(callback)

    def on_send_file(self, callback) -> None:
        """Register a callback for ``send_file`` tool deliveries.

        The callback receives ``(session_id, path, media_type, caption)``
        and should return truthy if it actually delivered the file (so the
        tool can report "sent" vs "no IM channel"). ``media_type`` is the
        model's hint (``image``/``video``/``file``) or ``None`` (infer from
        the extension). Used by the IM bridge to push generated files to the
        user's chat instead of relying on the model embedding resolvable
        references in its reply text.
        """
        self._send_file_callbacks.append(callback)

    async def await_user_answer(self, session_id: str, call_id: str = "") -> Dict[str, Any]:
        """Block the calling tool until the user replies via the UI.

        Used by the `ask_user` tool: instead of returning a fake
        "waiting" tool_result, the tool awaits this future so the model
        round stays open until the user actually answers. The future
        is set by `set_user_answer` (called from the server's
        `/sessions/{sid}/questions/reply` handler). If the turn is
        cancelled while waiting, returns a cancelled envelope so the
        LLM can react gracefully instead of hanging.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        session = self._get_session(session_id)
        session.pending_questions[call_id] = fut
        try:
            return await fut
        except asyncio.CancelledError:
            return {
                "status": "cancelled",
                "message": "User cancelled the turn before answering.",
            }
        finally:
            if session.pending_questions.get(call_id) is fut:
                session.pending_questions.pop(call_id, None)

    def set_user_answer(self, session_id: str, answer: str | None, call_id: str = "") -> bool:
        session = self._get_session(session_id)
        pending = session.pending_questions
        fut = pending.get(call_id)
        if fut is None or fut.done():
            return False
        if answer is None:
            fut.cancel()
        else:
            fut.set_result({"status": "answered", "answer": answer})
        return True

    def cancel_all_questions(self, session_id: str) -> None:
        session = self._get_session(session_id)
        for fut in list(session.pending_questions.values()):
            if not fut.done():
                fut.cancel()
        session.pending_questions.clear()

    async def _execute_tool(self, call: ToolCall, ctx: RuntimeContext) -> ToolResult:
        # Check deny list (legacy, for backward compatibility)
        deny_list = self.config.get("tool", {}).get("deny", [])
        if call.name in deny_list:
            return ToolResult(text=f"Error: permission_denied\nTool '{call.name}' is in the deny list", error=True)

        # Check sub-agent tool restrictions
        is_subagent = ctx.metadata.get("_subagent")
        if is_subagent:
            _parent_only = {"spawn_agent", "get_agent_result", "cancel_agent"}
            if call.name in _parent_only:
                return ToolResult(text=f"Error: tool_blocked\nSub-agents cannot use '{call.name}'", error=True)
            allowed_tools = ctx.metadata.get("_allowed_tools")
            if allowed_tools is not None and call.name not in allowed_tools:
                return ToolResult(text=f"Error: tool_blocked\nTool '{call.name}' is not available in this sub-agent", error=True)

        # Check approval policy
        approval_policy: ApprovalPolicy = self.config.get("approval", {}).get("policy", "suggest")
        request_id = str(uuid.uuid4())

        # Get tool permissions for permission checking
        tool_perms = []
        tool_rec = None
        for rec in self.registry.list_kind("tool"):
            if rec.instance.spec().get("name") == call.name:
                tool_rec = rec
                tool_perms = rec.manifest.get("permissions", {}).keys()
                break

        if approval_policy == "full-auto":
            pass
        elif approval_policy == "auto-edit":
            if "shell" in tool_perms:
                return ToolResult(text=f"Error: permission_denied\nTool '{call.name}' requires shell access which is denied in auto-edit mode", error=True)
        elif approval_policy == "suggest":
            perm_manager = self.permission_manager

            patterns = []
            permissions = []
            metadata = {"tool": call.name, "arguments": call.arguments}

            if "fs:read" in tool_perms or "fs:write" in tool_perms:
                if "file_path" in call.arguments:
                    patterns.append(call.arguments["file_path"])
                if "path" in call.arguments:
                    patterns.append(call.arguments["path"])

            for perm in tool_perms:
                if perm == "fs:read":
                    permissions.append("fs:read")
                elif perm == "fs:write":
                    permissions.append("fs:write")
                elif perm == "shell:execute":
                    permissions.append("shell:execute")
                elif perm == "tool":
                    permissions.append(call.name)

            if not patterns:
                patterns = ["*"]

            perm_config = self.config.get("permissions", {})
            ruleset = from_config(perm_config) if perm_config else []

            try:
                async def emit_permission_event(req_info: Dict[str, Any]) -> None:
                    await self._emit(
                        ctx.session_id,
                        {
                            "type": "permission_request",
                            "request": req_info,
                        },
                    )

                for perm in permissions:
                    await perm_manager.ask(
                        sessionID=ctx.session_id,
                        permission=perm,
                        patterns=patterns,
                        ruleset=ruleset,
                        metadata=metadata,
                        requestID=request_id,
                        tool={"name": call.name, "arguments": call.arguments},
                        event_callback=emit_permission_event,
                    )
            except RejectedError:
                return ToolResult(text=f"Error: permission_rejected\nTool '{call.name}' was rejected by user", error=True)
            except DeniedError as e:
                return ToolResult(text=f"Error: permission_denied\n{e}", error=True)
            except Exception as e:
                return ToolResult(text=f"Error: permission_error\n{e}", error=True)

        # Execute the tool
        tool_timeouts = self.config.get("tool", {}).get("timeouts", {})
        default_timeout = tool_timeouts.get("default", 120)
        for tool_rec in self.registry.list_kind("tool"):
            tool = tool_rec.instance
            spec = tool.spec()
            if spec.get("name") == call.name:
                hook_result = await self._run_hooks("before_tool", {"tool": call.name, "arguments": call.arguments}, ctx)
                if hook_result.get("_block"):
                    return ToolResult(
                        text=f"Blocked by hook: {hook_result.get('_block_reason', '')}",
                        error=True,
                    )
                if call.name in ("spawn_agent", "ask_user", "get_agent_result"):
                    # These three block on a per-session future that the
                    # *caller* (UI for ask_user, parent turn for
                    # spawn_agent, sibling turn for get_agent_result) is
                    # responsible for resolving or cancelling. A 120s
                    # default executor timeout would race that and
                    # surface a synthetic "Error: timeout" tool_result,
                    # which the model would happily treat as a real
                    # answer and write a new reply on top of. The tool
                    # itself enforces its own per-call bound (e.g. the
                    # `timeout` arg on get_agent_result, capped at
                    # 600000ms), so the executor layer is redundant
                    # *and* harmful here. Keep it out of the way.
                    timeout = None
                else:
                    # Single source of truth for timeout: if the tool declares
                    # a `timeout` parameter (e.g. shell), use the caller's
                    # value (the tool no longer wraps its own wait_for, so the
                    # executor is the only bound). Otherwise fall back to the
                    # configured executor default (120s).
                    props = ((spec.get("input_schema") or {}).get("properties") or {})
                    if "timeout" in props:
                        arg_to = call.arguments.get("timeout") if isinstance(call.arguments, dict) else None
                        timeout = min(int(arg_to), 600) if isinstance(arg_to, (int, float)) and arg_to > 0 else default_timeout
                    else:
                        timeout = tool_timeouts.get(call.name, default_timeout)
                try:
                    # Apply schema `default` values so the schema is the single
                    # source of truth — tools read args without duplicating
                    # defaults as code fallbacks.
                    _args = dict(hook_result.get("arguments", call.arguments) or {})
                    _props = (spec.get("input_schema") or {}).get("properties") or {}
                    for _pk, _pv in _props.items():
                        if _pk not in _args and isinstance(_pv, dict) and "default" in _pv:
                            _args[_pk] = _pv["default"]
                    out = await asyncio.wait_for(tool.run(_args, ctx), timeout=timeout)
                except asyncio.TimeoutError:
                    return ToolResult(text=f"Error: timeout\nTool '{call.name}' timed out after {timeout}s", error=True)
                if not isinstance(out, ToolResult):
                    out = ToolResult(text=str(out))
                hook_result = await self._run_hooks("after_tool", {"tool": call.name, "output": out, "arguments": call.arguments}, ctx)
                out = hook_result.get("output", out)
                return out
        return ToolResult(text=f"Error: tool_not_found\nTool '{call.name}' is not available", error=True)

    def _apply_prompt(self, messages: List[ChatMessage], ctx: RuntimeContext) -> List[ChatMessage]:
        prompts = self.registry.list_kind("prompt")
        if not prompts:
            return messages
        provider = prompts[0].instance
        variables = self.config.get("prompt", {}).get("variables", {})
        rendered = provider.render(messages[-1].content, variables, ctx)
        out = list(messages)
        out[-1] = ChatMessage(role=out[-1].role, content=rendered)
        return out

    def _capabilities_for_model_name(self, model_name: str) -> dict:
        """Return merged provider+model capabilities for `model_name`.

        Looks `model_name` up in ``self.config["providers"][*].models[*]`` and
        merges the model entry's capabilities on top of the provider's.
        Returns ``{}`` if the model isn't found anywhere — callers must
        handle the unknown case (e.g. defaulting thinking to False,
        vision to True).

        Used by per-turn code paths that need the capabilities of the
        model that will *actually* be used for this turn, which may
        differ from the runtime config's model when a session has
        pinned its own via updateSession.
        """
        for p in self.config.get("providers") or []:
            for m in p.get("models") or []:
                if m.get("name") == model_name:
                    caps = dict(p.get("capabilities") or {})
                    caps.update(m.get("capabilities") or {})
                    return caps
        return {}

    def _effort_levels_for_model(self, model_name: str) -> list[str]:
        """Supported thinking_mode levels for `model_name` (excl. disabled).

        From ``capabilities.effort_levels`` if declared; otherwise default to
        ``["low","medium","high"]`` when the model supports thinking, or ``[]``
        when ``capabilities.thinking`` is explicitly false (the UI then hides
        the effort dropdown entirely).
        """
        caps = self._capabilities_for_model_name(model_name)
        if caps.get("thinking") is False:
            return []
        levels = caps.get("effort_levels")
        if isinstance(levels, list) and levels:
            return [str(x) for x in levels]
        return ["low", "medium", "high", "xhigh", "max"]

    def _model_supports_image(self, model_name: str) -> bool:
        """True if `model_name` can consume `image_url` blocks.

        Reads ``capabilities.vision`` (model-level wins; provider-level
        is the fallback). Defaults to True when the model isn't found
        anywhere — the conservative choice for unknown models, so we
        don't silently drop image attachments.

        This drives the image-URL → data-URL vs → text-reference
        decision in ``_resolve_image_paths``. Vision models get the
        base64 data URL so the API can decode raw pixels; non-vision
        models get a path string the model can read with the
        filesystem tool. Both branches preserve the original
        ``rendered_messages`` so re-renders stay cheap.
        """
        for p in self.config.get("providers") or []:
            for m in p.get("models") or []:
                if m.get("name") == model_name:
                    m_caps = m.get("capabilities") or {}
                    if "vision" in m_caps:
                        return bool(m_caps["vision"])
                    prov_vision = bool((p.get("capabilities") or {}).get("vision", True))
                    return bool(m_caps.get("vision", prov_vision))
        return True

    # Backward-compat aliases — read the runtime config's model, NOT the
    # per-session one. Kept for environment_info() in the /status surface
    # and any external callers that want the global "what's the default
    # model" answer rather than the per-turn one. Per-turn code paths
    # (chat, chat_streaming, _run_model_tool_loop) must use the new
    # parameterized helpers above.
    def _current_model_capabilities(self) -> dict:
        return self._capabilities_for_model_name(
            self.config.get("model", {}).get("name", "")
        )

    def _current_model_supports_image(self) -> bool:
        return self._model_supports_image(
            self.config.get("model", {}).get("name", "")
        )

    def _is_retryable_provider_error(self, exc: Exception) -> bool:
        """True if a provider error is worth a same-input retry.

        Delegates to adapters.retry._is_retryable so the turn-level
        stream_reset loop and the connection-level retry wrapper use
        the same criteria. Kept as a method for backwards-compat with
        the chat_streaming caller.
        """
        from ziva.adapters.retry import _is_retryable
        return _is_retryable(exc)

    def _build_environment_context(self, workspace_root: str | None = None, model_cfg: dict | None = None) -> str:
        timezone = _detect_timezone()
        shell = os.environ.get("SHELL", "")
        # Use the per-turn model config (which includes session.model_name
        # overrides) instead of the runtime's global config["model"] so the
        # system prompt reflects the model that will actually handle this turn.
        effective_model_cfg = model_cfg or self.config.get("model", {})
        model_name = effective_model_cfg.get("name", "unknown")
        # Prefer the caller-supplied workspace (the session's own) over the
        # runtime's currently-focused workspace, so the `cwd:` line reflects
        # where this session lives, not where the user last switched to.
        ws = workspace_root or str(self.workspace_root)
        lines = [
            "## Environment",
            # Defensive cleanup of the workspace path before it reaches the
            # system prompt. Without this, sessions started before the
            # `expanduser().resolve()` fix may have a workspace_root like
            # `/Users/<u>/~/code/x` (literal ~ in the middle, because
            # Path('~/x').resolve() doesn't expand ~). expanduser alone
            # won't help because ~ isn't at the start, so we string-replace
            # the broken `/~/` segment with the user's home dir. For the
            # common case (already a clean absolute path) this is a no-op.
            f"cwd: {_clean_workspace_path_for_display(ws)}",
            f"shell: {Path(shell).name if shell else ''}",
            f"current_date: {_current_date_for_timezone(timezone)}",
            f"timezone: {timezone}",
            f"model: {model_name}",
            f"supports_image: {str(self._model_supports_image(model_name)).lower()}",
        ]
        return "\n".join(lines)

    async def _store_memory(self, messages: List[ChatMessage], result: ChatResult, ctx: RuntimeContext) -> None:
        mems = self.registry.list_kind("memory")
        if not mems:
            return
        store = mems[0].instance
        await store.put("last_turn", {"messages": [m.__dict__ for m in messages], "result": result.__dict__}, ctx)

    async def _run_hooks(self, lifecycle: str, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        from fnmatch import fnmatch
        # 全局禁用列表：从 config.hooks.disabled 读，所有子 agent / 父 agent 都受影响。
        # 与 ``_allowed_hooks`` / ``_denied_hooks`` 的"子 agent 个体限制"是正交维度——
        # 一个 hook 既要被全局允许、又要被该子 agent 个体允许才会跑。
        globally_disabled = set(self.config.get("hooks", {}).get("disabled", []) or [])
        # 子 agent hooks 过滤：
        # - ``_allowed_hooks`` 为 None 且 ``_denied_hooks`` 为 None → 继承全部
        # - ``_allowed_hooks`` 非 None → 只跑 id 在白名单内的 hook
        # - ``_denied_hooks`` 非 None → 跳过 id 在黑名单内的 hook
        # 两者并存时黑名单优先（deny 胜于 allow）。匹配按注册的 hook id，
        # 老的"event_name 写法"作为兼容仍然有效。
        allowed_hooks = ctx.metadata.get("_allowed_hooks") if ctx else None
        denied_hooks = ctx.metadata.get("_denied_hooks") if ctx else None
        for hook_rec in self.registry.list_kind("hook"):
            hook = hook_rec.instance
            if getattr(hook, "event_name", "") != lifecycle:
                continue
            hid = getattr(hook_rec, "id", "")
            # 全局禁用胜于所有其他设置——开关关闭后 hook 直接不跑。
            if hid in globally_disabled:
                continue
            if denied_hooks is not None and (hid in denied_hooks or lifecycle in denied_hooks):
                continue
            if allowed_hooks is not None:
                if hid not in allowed_hooks and lifecycle not in allowed_hooks:
                    continue
            matcher = getattr(hook, "matcher", None)
            if matcher and "tool" in payload:
                if not fnmatch(payload["tool"], matcher):
                    continue
            payload = await hook.handle(payload, ctx)
            if payload.get("_block"):          # 阻断信号，停止后续 hook
                break
        return payload

    def list_tools(self) -> List[Dict[str, Any]]:
        specs = []
        for tool_rec in self.registry.list_kind("tool"):
            tool = tool_rec.instance
            specs.append(tool.spec())
        return specs

    # ============= Storage Methods =============

    def _sanitize_orphaned_tool_calls(
        self,
        session_id: str,
        history: List["ChatMessage"],
        cancel_message: str = "Tool execution cancelled by user.",
    ) -> List["ChatMessage"]:
        """Append dummy tool_result messages for any assistant tool_calls
        that have no matching tool result.

        Anthropic (and every strict tool-use API) requires every
        ``tool_use`` block to be followed by a corresponding
        ``tool_result`` block. If a turn is cancelled *between* the
        assistant message being persisted and the tool results being
        written — or if the runtime dies between rounds — the JSONL on
        disk can end up with an orphaned ``assistant`` + ``tool_calls``
        message whose tool_calls have no follow-up. Replaying that
        history to the next turn produces a 400
        ``tool call result does not follow tool call (2013)``.

        We don't try to surgically edit the JSONL in place (line offsets
        would shift and break anything tailing the file). Instead we
        *append* synthetic ``role="tool"`` messages for the missing
        call IDs. That's append-only, monotonic, and downstream code
        (compaction, anthropic adapter, UI) treats them like normal
        tool results. The synthetic message carries
        ``is_error=True`` semantics via the text content so the model
        knows the tool didn't actually run, but the wire-format
        constraint is satisfied either way.
        """
        # Collect every tool_call_id that *does* have a matching
        # role="tool" result somewhere in history. We can't just look at
        # the immediate next message — earlier rounds' results are
        # interleaved with later rounds' assistant messages.
        answered: set[str] = set()
        for m in history:
            if m.role == "tool" and m.tool_call_id:
                answered.add(m.tool_call_id)

        sanitized: List["ChatMessage"] = []
        appended_any = False
        for m in history:
            sanitized.append(m)
            if m.role != "assistant" or not m.tool_calls:
                continue
            for tc in m.tool_calls:
                if tc.id and tc.id not in answered:
                    synthetic = ChatMessage(
                        role="tool",
                        content=f"[cancelled] {cancel_message}",
                        tool_call_id=tc.id,
                        name=tc.name,
                    )
                    sanitized.append(synthetic)
                    self._persist_message(
                        session_id,
                        synthetic,
                        is_subagent=False,
                        sub_call_id=None,
                    )
                    appended_any = True
        return sanitized

    def _load_session_from_disk(self, session_id: str) -> List[ChatMessage]:
        """Load messages from disk for a session.

        Returns the LLM-visible context: if a compaction summary exists,
        return just `[summary]` (no recent tail, no compacted originals).
        The originals remain on disk for the UI's expand affordance but
        should not bloat the next chat() call's prompt.
        """
        messages = []
        for msg_data in self.storage.get_messages(self._resolve_project_id(session_id), session_id):
            messages.append(ChatMessage(
                role=msg_data.get("role", "user"),
                content=msg_data.get("content", ""),
                tool_call_id=msg_data.get("tool_call_id"),
                name=msg_data.get("name"),
                tool_calls=[
                    ToolCallItem(
                        id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        arguments=tc.get("arguments", {}),
                    )
                    for tc in msg_data.get("tool_calls", [])
                ],
                reasoning_content=msg_data.get("reasoning_content"),
                reasoning_signature=msg_data.get("reasoning_signature"),
                _compaction_summary=msg_data.get("_compaction_summary", False),
                _hidden=msg_data.get("_hidden", False),
            ))
        return _llm_context(messages)

    def _persist_message(self, session_id: str, message: ChatMessage, is_subagent: bool = False, sub_call_id: str | None = None) -> None:
        """Persist a single message to disk."""
        record = {
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in message.tool_calls
            ],
        }
        # Tool messages get a stable id (= tool_call_id) so a background
        # sub-agent's completion can update_message this row in the parent
        # session later (rewrite "started" -> final summary).
        if message.role == "tool" and message.tool_call_id:
            record["id"] = message.tool_call_id
        if message.reasoning_content:
            record["reasoning_content"] = message.reasoning_content
        if message.reasoning_signature:
            record["reasoning_signature"] = message.reasoning_signature
        if is_subagent:
            record["_subagent"] = True
        if sub_call_id:
            record["_subagent_call_id"] = sub_call_id
        if message._compaction_summary:
            record["_compaction_summary"] = True
        if message._hidden:
            record["_hidden"] = True
        self.storage.append_message(self._resolve_project_id(session_id), session_id, record)
        self.storage.update_session(self._resolve_project_id(session_id), session_id, {
            "id": session_id,
            "time": {"updated": int(time.time() * 1000)}
        })

    def update_session_usage(self, session_id: str, usage: Dict[str, int] | None) -> None:
        """Persist usage data to session metadata."""
        if usage and usage.get("prompt_tokens"):
            self.storage.update_session(self._resolve_project_id(session_id), session_id, {
                "last_usage": usage,
            })


    def list_sessions(self, project_id: str | None = None) -> List[dict]:
        """List all sessions for this project."""
        return self.storage.list_sessions(project_id or self.project_id)

    def get_session(self, session_id: str) -> dict | None:
        """Get session metadata."""
        return self.storage.get_session(self._resolve_project_id(session_id), session_id)

    def delete_session(self, session_id: str) -> None:
        """Delete a session and its messages."""
        self.storage.delete_session(self._resolve_project_id(session_id), session_id)
        self._sessions.pop(session_id, None)

    async def shutdown(self) -> None:
        """Gracefully shutdown runtime, disconnecting MCP servers."""
        for session in list(self._sessions.values()):
            if session.mcp_client:
                try:
                    await session.mcp_client.cleanup()
                except Exception:
                    pass
            session.mcp_status = MCPConnectStatus.DISCONNECTED


def _clean_workspace_path_for_display(workspace_root) -> str:
    """Return a navigable absolute path string for the system prompt.

    Defends against two pre-fix forms that can land in `workspace_root`:

    1. Leading ``~`` (``~/code/x``): ``Path(s).expanduser()`` handles this.
    2. ``$HOME/~/code/x`` (literal ``~`` injected in the middle by
       ``Path('~/x').resolve()`` before the fix landed): ``expanduser``
       doesn't touch ``~`` in the middle, so we strip the broken
       ``$HOME/~/`` prefix (the ``$HOME`` part was CWD-relative glue
       from the buggy resolve, not part of the user's intent) and return
       ``$HOME/<rest>`` — the path the user originally typed.

    For the common case (a clean absolute path) both branches are
    no-ops and the input is returned unchanged.
    """
    if workspace_root is None:
        return ""
    p = str(workspace_root)
    home = str(Path.home())
    broken_prefix = f"{home}/~/"
    if p.startswith(broken_prefix):
        # Strip the prepended CWD-relative glue; the user's actual intent
        # was the `~/...` portion, which expands to $HOME/...
        p = f"{home}/{p[len(broken_prefix):]}"
    elif p.startswith("~"):
        p = str(Path(p).expanduser())
    return p
