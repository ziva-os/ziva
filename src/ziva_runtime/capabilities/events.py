from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, DefaultDict, Deque, Dict, List


@dataclass
class EventBus:
    _queues: DefaultDict[str, List[asyncio.Queue]]
    _history: DefaultDict[str, Deque[Dict[str, Any]]]
    _history_limit: int

    def __init__(self, history_limit: int = 500) -> None:
        self._queues = defaultdict(list)
        self._history = defaultdict(lambda: deque(maxlen=history_limit))
        self._history_limit = history_limit

    def subscribe(self, session_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[session_id].append(q)
        return q

    async def publish(self, session_id: str, event: Dict[str, Any]) -> None:
        self._history[session_id].append(event)
        for q in list(self._queues.get(session_id, [])):
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
