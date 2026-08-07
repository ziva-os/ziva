"""Permission system for tool usage approval."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, List, Optional

from ziva.permissions.wildcard import match as match_wildcard


Action = str
Reply = str


class PermissionAction:
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionReply:
    ONCE = "once"
    ALWAYS = "always"
    REJECT = "reject"


@dataclass
class Rule:
    permission: str
    pattern: str
    action: Action


Ruleset = List[Rule]


@dataclass
class Request:
    id: str
    sessionID: str
    permission: str
    patterns: List[str]
    metadata: Dict[str, Any]
    always: List[str]
    tool: Optional[Dict[str, str]] = None


class PermissionError(Exception):
    pass


class RejectedError(PermissionError):
    def __init__(self):
        super().__init__("The user rejected permission to use this specific tool call.")


class CorrectedError(PermissionError):
    def __init__(self, feedback: str):
        super().__init__(
            "The user rejected permission to use this specific tool call with the following feedback: "
            + feedback
        )


class DeniedError(PermissionError):
    def __init__(self, ruleset: Ruleset):
        super().__init__(
            "The user has specified a rule which prevents you from using this specific tool call. "
            + f"Here are some of the relevant rules {ruleset}"
        )
        self.ruleset = ruleset


def evaluate(permission: str, pattern: str, *rulesets: Ruleset) -> Rule:
    merged: Ruleset = [rule for ruleset in rulesets for rule in ruleset]
    for rule in reversed(merged):
        if match_wildcard(permission, rule.permission) and match_wildcard(pattern, rule.pattern):
            return rule
    return Rule(permission=permission, pattern="*", action="ask")


def _expand_pattern(pattern: str) -> str:
    if pattern.startswith("~/"):
        return os.path.expanduser("~") + pattern[1:]
    if pattern == "~":
        return os.path.expanduser("~")
    if pattern.startswith("$HOME/"):
        return os.path.expanduser("~") + pattern[5:]
    if pattern.startswith("$HOME"):
        return os.path.expanduser("~") + pattern[5:]
    return pattern


def from_config(permission: Dict[str, Any]) -> Ruleset:
    ruleset: Ruleset = []
    for key, value in permission.items():
        if isinstance(value, str):
            ruleset.append(Rule(permission=key, pattern="*", action=value))
            continue
        for pattern, action in value.items():
            ruleset.append(Rule(permission=key, pattern=_expand_pattern(pattern), action=action))
    return ruleset


class PermissionManager:
    EDIT_TOOLS = ["edit", "write", "patch", "multiedit"]

    def __init__(self):
        self._state = {
            "pending": {},  # request_id -> {"info": Request, "future": asyncio.Future, "event_callback": callable}
            "approved": [],  # Global approved rules
            "session_approved": {},  # session_id -> Ruleset
        }
        self._lock = asyncio.Lock()
        self._on_pending_callbacks: list[Callable[["Request"], Any]] = []

    def _get_state(self) -> Dict[str, Any]:
        return self._state

    def on_pending(self, callback: Callable[["Request"], Any]) -> None:
        """Register a callback for when a permission request needs user input."""
        self._on_pending_callbacks.append(callback)

    def list_pending(self) -> list["Request"]:
        """List all pending permission requests."""
        return [v["info"] for v in self._state["pending"].values()]

    async def ask(
        self,
        sessionID: str,
        permission: str,
        patterns: List[str],
        ruleset: Ruleset,
        metadata: Optional[Dict[str, Any]] = None,
        always: Optional[List[str]] = None,
        requestID: Optional[str] = None,
        tool: Optional[Dict[str, str]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        async with self._lock:
            state = self._get_state()
            pending_needed = False

            session_rules = state["session_approved"].get(sessionID, [])
            for pattern in patterns:
                # Order: ruleset (lowest) -> session_rules -> state["approved"] (Global, highest)
                rule = evaluate(permission, pattern, ruleset, session_rules, state["approved"])
                if rule.action == "deny":
                    relevant = [r for r in ruleset if match_wildcard(permission, r.permission)]
                    raise DeniedError(relevant)
                if rule.action == "allow":
                    continue
                pending_needed = True

            if not pending_needed:
                return

            req_id = requestID or str(asyncio.get_event_loop().time())
            future = asyncio.get_running_loop().create_future()
            info = Request(
                id=req_id,
                sessionID=sessionID,
                permission=permission,
                patterns=patterns,
                metadata=metadata or {},
                always=always or patterns,
                tool=tool,
            )

            state["pending"][req_id] = {
                "info": info,
                "future": future,
                "event_callback": event_callback
            }

            # Emit event via callback if provided
            if event_callback:
                try:
                    await event_callback(asdict(info))
                except Exception:
                    pass  # Don't fail permission check if event emission fails

            # Trigger on_pending callbacks (for CLI terminal approval)
            for cb in self._on_pending_callbacks:
                try:
                    cb(info)
                except Exception:
                    pass

        try:
            await future
        finally:
            async with self._lock:
                state["pending"].pop(req_id, None)

    def reply(self, requestID: str, reply: Reply, message: Optional[str] = None) -> None:
        # This needs to be synchronous for the web handler
        state = self._get_state()
        pending = state["pending"].pop(requestID, None)
        if not pending:
            return

        req_info: Request = pending["info"]
        future: asyncio.Future = pending["future"]

        if reply == "reject":
            error = CorrectedError(message) if message else RejectedError()
            if not future.done():
                future.set_exception(error)

            # Reject all pending requests for this session
            for rid, entry in list(state["pending"].items()):
                if entry["info"].sessionID != req_info.sessionID:
                    continue
                state["pending"].pop(rid, None)
                if not entry["future"].done():
                    entry["future"].set_exception(RejectedError())
            return

        if reply == "once":
            if not future.done():
                future.set_result(None)
            return

        if reply == "always":
            # Add to session-approved rules (scoped to this session)
            if req_info.sessionID not in state["session_approved"]:
                state["session_approved"][req_info.sessionID] = []

            for pattern in req_info.always:
                state["session_approved"][req_info.sessionID].append(
                    Rule(permission=req_info.permission, pattern=pattern, action="allow")
                )

            if not future.done():
                future.set_result(None)

            # Auto-approve other pending requests for this session that now match
            for rid, entry in list(state["pending"].items()):
                if entry["info"].sessionID != req_info.sessionID:
                    continue

                s_rules = state["session_approved"].get(req_info.sessionID, [])
                ok = all(
                    evaluate(entry["info"].permission, pattern, s_rules).action == "allow"
                    for pattern in entry["info"].patterns
                )
                if not ok:
                    continue
                state["pending"].pop(rid, None)
                if not entry["future"].done():
                    entry["future"].set_result(None)

    def list(self) -> List[Request]:
        state = self._get_state()
        return [entry["info"] for entry in state["pending"].values()]

    def disabled_tools(self, tools: List[str], ruleset: Ruleset) -> List[str]:
        disabled = []
        for tool in tools:
            permission = "edit" if tool in self.EDIT_TOOLS else tool
            rule = None
            for entry in reversed(ruleset):
                if match_wildcard(permission, entry.permission):
                    rule = entry
                    break
            if rule and rule.pattern == "*" and rule.action == "deny":
                disabled.append(tool)
        return disabled

    def set_approved_rules(self, rules: Ruleset) -> None:
        """Set the global approved rules from config."""
        self._state["approved"] = list(rules)


_manager = None


def get_permission_manager() -> PermissionManager:
    global _manager
    if _manager is None:
        _manager = PermissionManager()
    return _manager
