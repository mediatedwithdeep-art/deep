from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator


class Topics:
    """Topic names. Versioned, because a schema change to any of these is a
    breaking change for at least two services."""
    DETECTIONS = "sentinel.detections.v1"
    SIGHTINGS = "sentinel.sightings.v1"
    ALERTS = "sentinel.alerts.v1"
    CAMERA_HEALTH = "sentinel.camera.health.v1"
    TRACK_LINKS = "sentinel.track.links.v1"
    COMMANDS = "sentinel.commands.v1"

    ALL = [DETECTIONS, SIGHTINGS, ALERTS, CAMERA_HEALTH, TRACK_LINKS, COMMANDS]


@dataclass
class BusMessage:
    topic: str
    payload: dict[str, Any]
    key: str | None = None
    message_id: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def to_wire(self) -> bytes:
        return json.dumps(self.payload, default=_json_default).encode()


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump()
    return str(o)


class Bus(abc.ABC):
    """Minimal publish/subscribe contract.

    Deliberately small. Anything richer (transactions, exactly-once) is not
    portable across the three backends, and pretending otherwise produces
    code that works on one and silently misbehaves on another.
    """

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def publish(self, topic: str, payload: dict[str, Any],
                      key: str | None = None) -> str: ...

    @abc.abstractmethod
    def subscribe(self, topics: list[str], group: str,
                  consumer: str) -> AsyncIterator[BusMessage]: ...

    @abc.abstractmethod
    async def ack(self, message: BusMessage, group: str) -> None: ...

    async def publish_many(self, topic: str, payloads: list[dict[str, Any]],
                           key_field: str | None = None) -> int:
        n = 0
        for p in payloads:
            await self.publish(topic, p, key=p.get(key_field) if key_field else None)
            n += 1
        return n

    async def health(self) -> dict[str, Any]:
        return {"backend": type(self).__name__, "healthy": True}
