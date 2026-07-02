from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Dict

from ziva.runtime import Runtime
from ziva.shared_types import ChatMessage


@dataclass
class ACPServer:
    runtime: Runtime
    _streams: Dict[str, Dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self._streams is None:
            self._streams = {}

    async def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "initialize":
            return self._ok(
                request_id,
                {
                    "name": "ziva-acp",
                    "version": "0.2.0",
                    "capabilities": {"chat": True, "tools": True, "stream": True},
                },
            )
        if method == "ping":
            return self._ok(request_id, {"pong": True})
        if method == "tools/list":
            return self._ok(request_id, {"tools": self.runtime.list_tools()})
        if method == "chat":
            return await self._chat(request_id, params)
        if method == "chat_stream":
            return await self._chat_stream(request_id, params)
        if method == "chat_stream_chunks":
            return await self._chat_stream_chunks(request_id, params)
        if method == "chat_stream_open":
            return await self._chat_stream_open(request_id, params)
        if method == "chat_stream_next":
            return self._chat_stream_next(request_id, params)
        return self._err(request_id, -32601, f"Method not found: {method}", classification="method_not_found")

    async def _chat(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        messages = self._parse_messages(params)
        if not messages:
            return self._err(request_id, -32602, "params.messages must be a non-empty array", classification="invalid_params")

        result = await self.runtime.chat(messages, session_id=params.get("session_id"))
        return self._ok(
            request_id,
            {
                "message": {"role": result.role, "content": result.content},
                "model": result.model,
                "usage": result.usage or {},
                "finish_reason": result.finish_reason,
            },
        )

    async def _chat_stream(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        messages = self._parse_messages(params)
        if not messages:
            return self._err(request_id, -32602, "params.messages must be a non-empty array", classification="invalid_params")

        session_id, result, events = await self.runtime.chat_with_events(messages, session_id=params.get("session_id"))
        return self._ok(
            request_id,
            {
                "session_id": session_id,
                "events": events,
                "final": {
                    "role": result.role,
                    "content": result.content,
                    "model": result.model,
                    "usage": result.usage or {},
                    "finish_reason": result.finish_reason,
                },
            },
        )

    async def _chat_stream_chunks(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        messages = self._parse_messages(params)
        if not messages:
            return self._err(request_id, -32602, "params.messages must be a non-empty array", classification="invalid_params")

        session_id, result, events = await self.runtime.chat_with_events(messages, session_id=params.get("session_id"))
        chunks = self._build_chunks(events, result, params)
        return self._ok(request_id, {"session_id": session_id, "chunks": chunks})

    async def _chat_stream_open(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        messages = self._parse_messages(params)
        if not messages:
            return self._err(request_id, -32602, "params.messages must be a non-empty array", classification="invalid_params")
        session_id, result, events = await self.runtime.chat_with_events(messages, session_id=params.get("session_id"))
        chunks = self._build_chunks(events, result, params)
        stream_id = str(uuid.uuid4())
        self._streams[stream_id] = {"chunks": chunks, "index": 0, "session_id": session_id}
        return self._ok(request_id, {"stream_id": stream_id, "session_id": session_id, "size": len(chunks)})

    def _chat_stream_next(self, request_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        stream_id = params.get("stream_id")
        if not isinstance(stream_id, str) or not stream_id:
            return self._err(request_id, -32602, "params.stream_id is required", classification="invalid_params")
        state = self._streams.get(stream_id)
        if not state:
            return self._err(request_id, -32602, "stream not found", classification="invalid_stream")

        idx = state["index"]
        chunks = state["chunks"]
        if idx >= len(chunks):
            del self._streams[stream_id]
            return self._ok(request_id, {"done": True, "chunk": None})

        chunk = chunks[idx]
        state["index"] = idx + 1
        done = state["index"] >= len(chunks)
        if done:
            del self._streams[stream_id]
        return self._ok(request_id, {"done": done, "chunk": chunk})

    def _parse_messages(self, params: Dict[str, Any]) -> list[ChatMessage]:
        raw = params.get("messages")
        if not isinstance(raw, list) or not raw:
            return []
        return [ChatMessage(role=str(it.get("role", "user")), content=str(it.get("content", ""))) for it in raw if isinstance(it, dict)]

    def _build_chunks(self, events: list[Dict[str, Any]], result: Any, params: Dict[str, Any]) -> list[Dict[str, Any]]:
        granularity = str(params.get("token_granularity", "word")).lower()
        chunks: list[Dict[str, Any]] = []
        for ev in events:
            et = ev.get("type")
            if et == "model_response":
                content = ev.get("content", "")
                for piece in self._split_content(content, granularity):
                    chunks.append(
                        {
                            "type": "delta",
                            "content": piece,
                            "seq": ev.get("seq"),
                            "round": ev.get("round"),
                            "ts": ev.get("ts"),
                        }
                    )
            elif et in {"tool_start", "tool_end"}:
                chunks.append(
                    {
                        "type": et,
                        "payload": ev,
                        "seq": ev.get("seq"),
                        "round": ev.get("round"),
                        "ts": ev.get("ts"),
                    }
                )
        chunks.append(
            {
                "type": "final",
                "ts": events[-1].get("ts") if events else None,
                "payload": {
                    "role": result.role,
                    "content": result.content,
                    "model": result.model,
                    "usage": result.usage or {},
                    "finish_reason": result.finish_reason,
                },
            }
        )
        return chunks

    def _split_content(self, content: str, granularity: str) -> list[str]:
        if not content:
            return [""]
        if granularity == "char":
            return list(content)
        # word-ish segmentation while preserving whitespace chunks
        pieces = [p for p in re.split(r"(\\s+)", content) if p != ""]
        return pieces if pieces else [content]

    @staticmethod
    def _ok(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _err(request_id: Any, code: int, message: str, classification: str) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": code,
                "message": message,
                "data": {
                    "classification": classification,
                },
            },
        }


async def serve_stdio(server: ACPServer) -> int:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            break
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error", "data": {"classification": "parse_error"}},
            }
        else:
            response = await server.handle(request)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0
