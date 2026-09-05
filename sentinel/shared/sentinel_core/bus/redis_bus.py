"""Redis Streams bus. The MVP default.

Consumer groups (XREADGROUP) give at-least-once delivery with explicit acks
and a pending-entries list for crash recovery -- the same guarantees the
pipeline needs from Kafka, without the operational weight.

Streams are capped with MAXLEN ~ so a stalled consumer cannot exhaust
memory. That is a real risk here: if the event processor dies while 50
cameras keep publishing, an uncapped stream fills RAM and takes Redis down,
which takes the whole system down. Bounded loss beats total failure.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from .base import Bus, BusMessage, _json_default

# Per-topic caps, roughly "a few minutes of headroom at MVP rates".
DEFAULT_MAXLEN = {
    "sentinel.detections.v1": 200_000,
    "sentinel.sightings.v1": 100_000,
    "sentinel.alerts.v1": 50_000,
    "sentinel.camera.health.v1": 50_000,
    "sentinel.track.links.v1": 50_000,
    "sentinel.commands.v1": 10_000,
}


class RedisBus(Bus):
    def __init__(self, url: str, maxlen: dict[str, int] | None = None,
                 block_ms: int = 2000, batch: int = 50):
        self.url = url
        self.maxlen = {**DEFAULT_MAXLEN, **(maxlen or {})}
        self.block_ms = block_ms
        self.batch = batch
        self._redis = None
        self._groups_ready: set[tuple[str, str]] = set()

    async def connect(self) -> None:
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(self.url, decode_responses=True)
        await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def publish(self, topic: str, payload: dict[str, Any],
                      key: str | None = None) -> str:
        assert self._redis is not None, "call connect() first"
        fields = {"data": json.dumps(payload, default=_json_default)}
        if key:
            fields["key"] = key
        return await self._redis.xadd(
            topic, fields, maxlen=self.maxlen.get(topic, 100_000), approximate=True)

    async def _ensure_group(self, topic: str, group: str) -> None:
        if (topic, group) in self._groups_ready:
            return
        try:
            # id="0" so a newly created group replays whatever is still in the
            # stream. mkstream lets a consumer start before any producer.
            await self._redis.xgroup_create(topic, group, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
        self._groups_ready.add((topic, group))

    async def subscribe(self, topics: list[str], group: str,
                        consumer: str) -> AsyncIterator[BusMessage]:
        assert self._redis is not None, "call connect() first"
        for t in topics:
            await self._ensure_group(t, group)

        # First drain this consumer's own pending entries (messages delivered
        # but never acked before a crash), then switch to new messages.
        streams = {t: "0" for t in topics}
        replaying = True

        while True:
            try:
                resp = await self._redis.xreadgroup(
                    group, consumer, streams,
                    count=self.batch, block=None if replaying else self.block_ms)
            except Exception as e:
                if "NOGROUP" in str(e):
                    self._groups_ready.clear()
                    for t in topics:
                        await self._ensure_group(t, group)
                    continue
                raise

            if not resp:
                if replaying:
                    replaying = False
                    streams = {t: ">" for t in topics}
                    continue
                await asyncio.sleep(0)
                continue

            empty = True
            for stream_name, entries in resp:
                if entries:
                    empty = False
                for msg_id, fields in entries:
                    raw = fields.get("data")
                    if raw is None:
                        await self._redis.xack(stream_name, group, msg_id)
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        # Poison message: ack it so it cannot block the group
                        # forever, but do not pretend it was processed.
                        await self._redis.xack(stream_name, group, msg_id)
                        continue
                    yield BusMessage(topic=stream_name, payload=payload,
                                     key=fields.get("key"), message_id=msg_id)
            if replaying and empty:
                replaying = False
                streams = {t: ">" for t in topics}

    async def ack(self, message: BusMessage, group: str) -> None:
        if self._redis is not None and message.message_id:
            await self._redis.xack(message.topic, group, message.message_id)

    async def health(self) -> dict[str, Any]:
        if self._redis is None:
            return {"backend": "redis", "healthy": False, "error": "not connected"}
        try:
            await self._redis.ping()
            info = await self._redis.info("memory")
            depths = {}
            for t in DEFAULT_MAXLEN:
                try:
                    depths[t] = await self._redis.xlen(t)
                except Exception:
                    depths[t] = 0
            return {"backend": "redis", "healthy": True,
                    "used_memory_mb": round(int(info.get("used_memory", 0)) / 1e6, 1),
                    "queue_depth": depths}
        except Exception as e:
            return {"backend": "redis", "healthy": False, "error": str(e)}
