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
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from sentinel_core.bus import Bus, Topics
from sentinel_core.log import get_logger

from . import db

log = get_logger("sentinel.api.ws")

# Per-client buffer. Ten messages is roughly two seconds of a busy estate;
# a client that cannot keep up with that is not going to recover.
CLIENT_QUEUE_SIZE = 10


# eq=False keeps identity-based __hash__. A plain @dataclass generates
# __eq__, which sets __hash__ to None and makes the class unhashable -- so
# `self.clients.add(client)` raises TypeError and every WebSocket connection
# is closed the instant it opens. Two clients are never "equal" anyway; they
# are distinct sockets.
@dataclass(eq=False)
class Client:
    websocket: WebSocket
    username: str
    #: Department code this socket may receive events for. None with
    #: sees_all False means the socket receives nothing -- the same
    #: fail-closed rule the REST layer applies to an unassigned user.
    department: str | None = None
    #: True only for the state admin, who spans every department.
    sees_all: bool = False
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
    #: Seconds to wait before a cache miss may refresh the directory again.
    REFRESH_COOLDOWN = 30.0

    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._camera_dept: dict[str, str] = {}
        self._dept_refreshed_at = 0.0
        self._task: asyncio.Task | None = None
        self._bus: Bus | None = None
        self.messages_sent = 0
        self.messages_dropped = 0

    # ── client lifecycle ─────────────────────────────────────────────
    async def connect(self, websocket: WebSocket, username: str,
                      department: str | None = None,
                      sees_all: bool = False) -> Client:
        await websocket.accept()
        client = Client(websocket=websocket, username=username,
                        department=department, sees_all=sees_all)
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
    async def broadcast(self, channel: str, payload: dict[str, Any],
                        department: str | None = None) -> None:
        """Fan one event out to the sockets entitled to it.

        `department` is the department that owns the camera that produced
        the event. An event that cannot be attributed to one reaches only
        the state admin: the live channel must not become the hole that the
        REST layer's scoping closed.
        """
        message = {"type": "event", "channel": channel, "data": payload}
        stale: list[Client] = []
        for client in list(self.clients):
            if not client.wants(channel):
                continue
            if not client.sees_all and (
                    department is None or client.department != department):
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

    async def _camera_department(self, camera_ref: str | None) -> str | None:
        """Which department owns this camera, or None if unattributable.

        Cached, because this runs on every broadcast. A miss refreshes the
        directory at most once every REFRESH_COOLDOWN seconds so a newly
        onboarded camera becomes visible quickly without letting an unknown
        reference hammer the database.
        """
        if not camera_ref:
            return None
        if camera_ref in self._camera_dept:
            return self._camera_dept[camera_ref]
        now = time.monotonic()
        if now - self._dept_refreshed_at < self.REFRESH_COOLDOWN:
            return None
        self._dept_refreshed_at = now
        try:
            rows = await db.fetch_all(
                "SELECT c.camera_id, d.code FROM camera c "
                "JOIN department d ON d.id = c.department_id")
            self._camera_dept = {r["camera_id"]: r["code"] for r in rows}
        except Exception as e:                            # pragma: no cover
            log.error("camera directory refresh failed", extra={"error": str(e)})
            return None
        return self._camera_dept.get(camera_ref)

    async def _route(self, topic: str, payload: dict) -> None:
        dept = await self._camera_department(
            payload.get("camera_ref") or payload.get("camera_id"))
        if topic == Topics.ALERTS:
            await self.broadcast("alerts", payload, dept)
            if vid := payload.get("vehicle_track_id"):
                await self.broadcast(f"vehicle:{vid}", payload, dept)
        elif topic == Topics.SIGHTINGS:
            await self.broadcast("sightings", payload, dept)
            if cam := payload.get("camera_id"):
                await self.broadcast(f"camera:{cam}", payload, dept)
        elif topic == Topics.CAMERA_HEALTH:
            await self.broadcast("camera_health", payload, dept)

    def stats(self) -> dict:
        return {
            "clients": len(self.clients),
            "messages_sent": self.messages_sent,
            "messages_dropped": self.messages_dropped,
            "subscriptions": sorted({c for cl in self.clients for c in cl.channels}),
        }


manager = ConnectionManager()
