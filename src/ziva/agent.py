"""High-level agent API: ``from ziva import Agent``.

``Agent`` is the convenience surface over :class:`ziva.runtime.Runtime` — one
object you configure with kwargs (model, providers, api_key, storage, tools,
approval policy, …) and then drive with ``await agent.chat(...)`` or
``async for ev in agent.stream(...)``. Everything the full ``Runtime`` exposes
is still reachable via the :attr:`Agent.runtime` escape hatch.

Design notes
------------
* The constructor never touches ``~/.ziva`` unless you ask it to (by passing
  ``config=<path>`` or relying on the default OpenAI provider). Pass
  ``storage=InMemoryStorage()`` to run with no filesystem at all.
* ``api_key``/``base_url`` are auto-attached to whichever provider owns the
  chosen model; if the model isn't declared by any provider, a default
  provider entry is synthesized (api_type inferred from ``base_url``).
* If no key is supplied, ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` are used as
  a last resort so ``Agent(model="gpt-4.1")`` works in a shell that already
  exports a key.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from ziva.config.loader import _deep_merge, load_effective_config, validate_config
from ziva.runtime import Runtime, _find_provider_for_model
from ziva.shared_types import ChatMessage, ChatResult
from ziva.storage import FileStorage, InMemoryStorage, Storage


def _looks_anthropic(base_url: str) -> bool:
    return "anthropic" in (base_url or "").lower()


class Agent:
    """A customizable agent wrapping the ziva :class:`Runtime`.

    Example
    -------
    >>> import asyncio, ziva
    >>> a = ziva.Agent(model="gpt-4.1", api_key="sk-...",
    ...                storage=ziva.InMemoryStorage())
    >>> result = asyncio.run(a.chat("Summarize the plot of Hamlet."))
    >>> print(result.content)
    """

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        providers: Optional[List[dict]] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        workspace: Union[str, Path, None] = None,
        config: Union[dict, str, Path, None] = None,
        storage: Optional[Storage] = None,
        tools: Optional[list] = None,
        skills: Optional[List[str]] = None,
        approval: str = "suggest",
        system_prompt: Optional[str] = None,
        max_rounds: Optional[int] = None,
        permissions: Optional[Any] = None,
        load_default_plugins: bool = True,
    ) -> None:
        self._config = self._build_config(
            base_config=config,
            model=model,
            providers=providers,
            api_key=api_key,
            base_url=base_url,
            approval=approval,
            system_prompt=system_prompt,
            max_rounds=max_rounds,
            skills=skills,
        )
        self._runtime = Runtime.from_config(
            self._config,
            workspace_root=workspace,
            storage=storage,
            permission_manager=permissions,
            load_default_plugins=load_default_plugins,
            extra_tools=tools,
            extra_skill_paths=skills,
        )

    # ---- configuration ----
    @staticmethod
    def _build_config(
        *,
        base_config: Union[dict, str, Path, None],
        model: Optional[str],
        providers: Optional[List[dict]],
        api_key: Optional[str],
        base_url: Optional[str],
        approval: str,
        system_prompt: Optional[str],
        max_rounds: Optional[int],
        skills: Optional[List[str]],
    ) -> Dict[str, Any]:
        # 1. Start from a validated base: explicit dict, a YAML path, or the
        #    built-in DEFAULT_CONFIG (no ~/.ziva read).
        if base_config is None:
            cfg = load_effective_config(None)
        elif isinstance(base_config, dict):
            cfg = _deep_merge(dict(base_config), {})
            validate_config(cfg)
        else:
            cfg = load_effective_config(Path(base_config))

        # 2. Provider list override.
        if providers is not None:
            cfg["providers"] = [dict(p) for p in providers]

        # 3. Model selection.
        if model:
            cfg.setdefault("model", {})["name"] = model

        # 4. api_key / base_url: attach to the model's owning provider, or
        #    synthesize one if the model isn't declared anywhere. Fall back to
        #    the standard env vars so a bare Agent(model=...) works.
        eff_key = api_key
        if eff_key is None:
            eff_key = os.environ.get("ANTHROPIC_API_KEY") if _looks_anthropic(base_url or "") else None
        if eff_key is None:
            eff_key = os.environ.get("OPENAI_API_KEY")

        chosen_model = cfg.get("model", {}).get("name", "")
        if eff_key is not None or base_url is not None:
            prov = _find_provider_for_model(cfg)
            if prov is None:
                # Synthesize a default provider around the chosen model.
                api_type = "anthropic" if _looks_anthropic(base_url or "") else "openai_compatible"
                prov = {
                    "name": (chosen_model or "default").split("-", 1)[0].capitalize() or "Default",
                    "api_type": api_type,
                    "api_key": eff_key or "",
                    "base_url": base_url or "",
                    "models": [{"name": chosen_model}],
                }
                cfg.setdefault("providers", []).append(prov)
            else:
                if eff_key is not None:
                    prov["api_key"] = eff_key
                if base_url is not None:
                    prov["base_url"] = base_url

        # 5. Simple scalar overlays.
        if approval:
            cfg.setdefault("approval", {})["policy"] = approval
        if system_prompt is not None:
            cfg.setdefault("prompt", {})["system_prompt"] = system_prompt
        if max_rounds is not None:
            cfg.setdefault("tool", {})["max_rounds"] = int(max_rounds)
        if skills is not None:
            cfg.setdefault("skill", {})["extra_paths"] = list(skills)

        validate_config(cfg)
        return cfg

    # ---- conversation ----
    @staticmethod
    def _to_messages(message: Any) -> List[ChatMessage]:
        if isinstance(message, str):
            return [ChatMessage(role="user", content=message)]
        out: List[ChatMessage] = []
        for item in message:
            if isinstance(item, ChatMessage):
                out.append(item)
            elif isinstance(item, dict):
                out.append(ChatMessage(role=item["role"], content=item["content"]))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                out.append(ChatMessage(role=item[0], content=item[1]))
            else:
                raise TypeError(f"Unsupported message item: {item!r}")
        return out

    async def chat(self, message: Any, *, session_id: Optional[str] = None) -> ChatResult:
        """Send a message (str or list of messages) and return the final result."""
        return await self._runtime.chat(self._to_messages(message), session_id=session_id)

    async def stream(self, message: Any, *, session_id: Optional[str] = None) -> AsyncIterator[dict]:
        """Yield runtime events as the turn streams (delta / tool_* / turn_end …)."""
        messages = self._to_messages(message)
        async for event in self._runtime.chat_streaming(messages, session_id=session_id):
            yield event

    # ---- session / tools ----
    def new_session(self) -> str:
        """Return a fresh session id (the runtime creates sessions lazily)."""
        return str(uuid.uuid4())

    def register_tool(self, tool: Any, tool_id: Optional[str] = None) -> None:
        """Register a Tool-like object (``spec()`` + ``async run(args, ctx)``)."""
        self._runtime.register_tool(tool, tool_id=tool_id)

    # ---- escape hatches ----
    @property
    def runtime(self) -> Runtime:
        """The underlying :class:`Runtime` for advanced use."""
        return self._runtime

    @property
    def config(self) -> Dict[str, Any]:
        """The effective config dict this agent was built with."""
        return self._config

    async def __aenter__(self) -> "Agent":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._runtime.shutdown()


__all__ = ["Agent"]
