"""Kafka bus. The scale-out path; not used by the MVP.

Kept deliberately thin and API-identical to RedisBus so the swap at scale is
a config change, not a refactor. Partition by key (camera_id) so one
camera's events stay ordered and consumers can shard by geography.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .base import Bus, BusMessage, _json_default


class KafkaBus(Bus):
    def __init__(self, bootstrap: str):
        self.bootstrap = bootstrap
        self._producer = None
        self._consumers: dict[str, Any] = {}

    async def connect(self) -> None:
        from aiokafka import AIOKafkaProducer
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap,
            value_serializer=lambda v: json.dumps(v, default=_json_default).encode(),
            key_serializer=lambda k: k.encode() if k else None,
            linger_ms=20,               # small batching window; latency budget allows it
            compression_type="lz4",
            acks="all",
        )
        await self._producer.start()

    async def close(self) -> None:
        if self._producer:
            await self._producer.stop()
            self._producer = None
        for c in self._consumers.values():
            await c.stop()
        self._consumers.clear()

    async def publish(self, topic: str, payload: dict[str, Any],
                      key: str | None = None) -> str:
        assert self._producer is not None, "call connect() first"
        md = await self._producer.send_and_wait(topic, payload, key=key)
        return f"{md.partition}-{md.offset}"

    async def subscribe(self, topics: list[str], group: str,
                        consumer: str) -> AsyncIterator[BusMessage]:
        from aiokafka import AIOKafkaConsumer
        c = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self.bootstrap,
            group_id=group,
            client_id=consumer,
            value_deserializer=lambda v: json.loads(v.decode()),
            enable_auto_commit=False,        # explicit ack, same as Redis path
            auto_offset_reset="earliest",
        )
        await c.start()
        self._consumers[group] = c
        try:
            async for rec in c:
                yield BusMessage(topic=rec.topic, payload=rec.value,
                                 key=rec.key.decode() if rec.key else None,
                                 message_id=f"{rec.partition}-{rec.offset}")
        finally:
            await c.stop()
            self._consumers.pop(group, None)

    async def ack(self, message: BusMessage, group: str) -> None:
        c = self._consumers.get(group)
        if c is not None:
            await c.commit()
