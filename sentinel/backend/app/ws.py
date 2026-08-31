"""WebSocket fan-out for live alerts, sightings and camera health.

One task subscribes to the bus and pushes to every interested client, so
N connected operators cost one consumer rather than N.

Sends are non-blocking and best-effort. A command centre wall that has
frozen or a laptop that slept must never be able to stall the broadcast to
everyone else: a client whose queue is full is disconnected rather than
waited on. For live operations, dropping a stale update is always better
than delaying a current one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from sentinel_core.bus import Bus, Topics
from sentinel_core.log import get_logger

log = get_logger("sentinel.api.ws")

# Per-client buffer. Ten messages is roughly two seconds of a busy estate;
# a client that cannot keep up with that is not going to recover.
CLIENT_QUEUE_SIZE = 10


@dataclass
class Client:
    websocket: WebSocket
    username: str
    channels: set[str] = field(default_factory=lambda: {"alerts"})
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE))
    dropped: int = 0

    def wants(self, channel: str) -> bool:
        if "*" in self.channels or channel in self.channels:
            return True
        # "vehicle:*" style prefix subscriptions, so an operator can follow
        # one vehicle without receiving the whole firehose.
        return any(c.endswith(":*") and channel.startswith(c[:-1])
                   for c in self.channels)


class ConnectionManager:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._task: asyncio.Task | None = None
        self._bus: Bus | None = None
        self.messages_sent = 0
        self.messages_dropped = 0

    # ── client lifecycle ─────────────────────────────────────────────
    async def connect(self, websocket: WebSocket, username: str) -> Client:
        await websocket.accept()
        client = Client(websocket=websocket, username=username)
        self.clients.add(client)
        log.info("websocket connected",
                 extra={"user": username, "clients": len(self.clients)})
        await websocket.send_json({
            "type": "connected",
            "channels": sorted(client.channels),
            "available_channels": ["alerts", "sightings", "camera_health",
                                   "vehicle:<id>", "camera:<id>", "*"],
        })
        return client

    def disconnect(self, client: Client) -> None:
        self.clients.discard(client)
        log.info("websocket disconnected",
                 extra={"user": client.username, "clients": len(self.clients),
                        "dropped": client.dropped})

    # ── broadcast ────────────────────────────────────────────────────
    async def broadcast(self, channel: str, payload: dict[str, Any]) -> None:
        message = {"type": "event", "channel": channel, "data": payload}
        stale: list[Client] = []
        for client in list(self.clients):
            if not client.wants(channel):
                continue
            try:
                client.queue.put_nowait(message)
                self.messages_sent += 1
            except asyncio.QueueFull:
                client.dropped += 1
                self.messages_dropped += 1
                # A client three seconds behind is not going to catch up on
                # a live feed. Disconnecting lets it reconnect and resync
                # rather than accumulating an ever-growing lag.
                if client.dropped > 30:
                    stale.append(client)
        for client in stale:
            log.warning("dropping slow websocket client",
                        extra={"user": client.username, "dropped": client.dropped})
            self.disconnect(client)
            with contextlib.suppress(Exception):
                await client.websocket.close(code=1013, reason="client too slow")

    async def pump(self, client: Client) -> None:
        """Drain one client's queue to its socket."""
        while True:
            message = await client.queue.get()
            await client.websocket.send_text(json.dumps(message, default=str))

    # ── bus consumer ─────────────────────────────────────────────────
    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _consume(self) -> None:
        assert self._bus is not None
        # A unique group per API replica: every replica must see every
        # alert, because each has its own set of connected operators. A
        # shared group would deliver each alert to exactly one replica and
        # the other replicas' operators would silently never see it.
        import socket
        group = f"sentinel-api-ws-{socket.gethostname()}"
        try:
            async for msg in self._bus.subscribe(
                    [Topics.ALERTS, Topics.SIGHTINGS, Topics.CAMERA_HEALTH],
                    group, "ws-fanout"):
                try:
                    await self._route(msg.topic, msg.payload)
                finally:
                    await self._bus.ack(msg, group)
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # pragma: no cover
            log.exception("websocket bus consumer stopped", extra={"error": str(e)})

    async def _route(self, topic: str, payload: dict) -> None:
        if topic == Topics.ALERTS:
            await self.broadcast("alerts", payload)
            if vid := payload.get("vehicle_track_id"):
                await self.broadcast(f"vehicle:{vid}", payload)
        elif topic == Topics.SIGHTINGS:
            await self.broadcast("sightings", payload)
            if cam := payload.get("camera_id"):
                await self.broadcast(f"camera:{cam}", payload)
        elif topic == Topics.CAMERA_HEALTH:
            await self.broadcast("camera_health", payload)

    def stats(self) -> dict:
        return {
            "clients": len(self.clients),
            "messages_sent": self.messages_sent,
            "messages_dropped": self.messages_dropped,
            "subscriptions": sorted({c for cl in self.clients for c in cl.channels}),
        }


manager = ConnectionManager()
