from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, DefaultDict, Deque, Dict, List


@dataclass
class EventBus:
    _queues: DefaultDict[str, List[asyncio.Queue]]
    _history: DefaultDict[str, Deque[Dict[str, Any]]]
    _global_queues: List[asyncio.Queue]
    _history_limit: int

    def __init__(self, history_limit: int = 500) -> None:
        self._queues = defaultdict(list)
        self._history = defaultdict(lambda: deque(maxlen=history_limit))
        self._global_queues = []
        self._history_limit = history_limit

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[session_id].append(q)
        return q

    def subscribe_global(self) -> asyncio.Queue:
        """Subscribe to a single broadcast queue that fans out every
        session's events. The frontend uses this so one SSE connection
        can deliver events for N sessions; per-session routing happens
        client-side from the `session_id` field on each event."""
        q: asyncio.Queue = asyncio.Queue()
        self._global_queues.append(q)
        return q

    def unsubscribe_global(self, queue: asyncio.Queue) -> None:
        if queue in self._global_queues:
            self._global_queues.remove(queue)

    async def publish(self, session_id: str, event: Dict[str, Any]) -> None:
        self._history[session_id].append(event)
        for q in list(self._queues.get(session_id, [])):
            await q.put(event)
        for q in list(self._global_queues):
            await q.put(event)

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        if session_id in self._queues and queue in self._queues[session_id]:
            self._queues[session_id].remove(queue)
            if not self._queues[session_id]:
                del self._queues[session_id]

    def history(self, session_id: str) -> List[Dict[str, Any]]:
        return list(self._history.get(session_id, []))

    def clear_history(self, session_id: str) -> None:
        if session_id in self._history:
            del self._history[session_id]

    def unsubscribe_all(self, session_id: str) -> None:
        """Remove all subscriber queues for a session (called on session delete)."""
        self._queues.pop(session_id, None)
