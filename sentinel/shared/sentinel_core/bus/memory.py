"""In-process bus for unit tests. No external service, deterministic ordering."""

from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict
from typing import Any, AsyncIterator

from .base import Bus, BusMessage


class MemoryBus(Bus):
    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], asyncio.Queue[BusMessage]] = {}
        self._groups: dict[str, set[str]] = defaultdict(set)
        self._counter = itertools.count(1)
        self._connected = False
        self.published: list[BusMessage] = []      # inspectable by tests

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def publish(self, topic: str, payload: dict[str, Any],
                      key: str | None = None) -> str:
        mid = f"mem-{next(self._counter)}"
        msg = BusMessage(topic=topic, payload=payload, key=key, message_id=mid)
        self.published.append(msg)
        # Fan out one copy per consumer group, mirroring real broker semantics.
        for group in self._groups[topic]:
            q = self._queues.setdefault((topic, group), asyncio.Queue())
            await q.put(msg)
        return mid

    async def subscribe(self, topics: list[str], group: str,
                        consumer: str) -> AsyncIterator[BusMessage]:
        for t in topics:
            self._groups[t].add(group)
            self._queues.setdefault((t, group), asyncio.Queue())
        queues = [self._queues[(t, group)] for t in topics]
        while True:
            getters = [asyncio.create_task(q.get()) for q in queues]
            done, pending = await asyncio.wait(getters, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            for d in done:
                yield d.result()

    async def ack(self, message: BusMessage, group: str) -> None:
        return None
