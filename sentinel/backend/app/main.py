"""Sentinel API.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Serves the command centre: camera registry, vehicle search and tracking,
alerts, analytics, and a WebSocket fan-out for live events.

The API never carries video. It returns a WHEP URL the browser negotiates
directly with the media server, so video bytes never traverse this process
and one slow viewer cannot affect anyone else.
"""

from __future__ import annotations

import contextlib
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sentinel_core.bus import create_bus
from sentinel_core.config import get_settings
from sentinel_core.log import configure_logging, get_logger, set_trace_id

from . import db, metrics, ws
from .deps import rate_limit
from .routers import alerts, auth, cameras, intelligence, system, vehicles
from .security import Role, decode_token

log = get_logger("sentinel.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging("api", settings.log_level, settings.log_format)
    log.info("starting", extra={"environment": settings.environment})

    await db.init_pool(settings.database_url,
                       settings.postgres_pool_min, settings.postgres_pool_max)

    # Partitions must exist before the first row of the day lands, or the
    # insert fails outright. Doing it here means a fresh deployment works
    # without a separate scheduler being configured first.
    with contextlib.suppress(Exception):
        await db.execute("SELECT count(*) FROM ensure_partitions()")

    bus = create_bus(settings.bus_backend, redis_url=settings.redis_url,
                     kafka_bootstrap=settings.kafka_bootstrap)
    await bus.connect()
    await ws.manager.start(bus)
    app.state.bus = bus

    log.info("ready")
    yield

    await ws.manager.stop()
    await bus.close()
    await db.close_pool()
    log.info("stopped")


app = FastAPI(
    title="Sentinel VMS API",
    description=(
        "Unified Video Management System and AI analytics for the Gujarat "
        "Police Sentinel Challenge 2026.\n\n"
        "**Nothing this API returns about vehicle identity is certain.** "
        "Cross-camera associations carry a decision band (AUTO / PROBABLE) "
        "and a full score breakdown; plate searches are fuzzy by design. "
        "Endpoints exposing an identifiable person's movements require an "
        "`X-Reason` header stating the purpose, which is written to the "
        "audit log (DPDP Act 2023)."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Reason"],
)


@app.middleware("http")
async def observability(request: Request, call_next):
    """Trace id, timing, metrics and security headers on every response."""
    trace_id = set_trace_id(request.headers.get("x-trace-id"))
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        metrics.REQUESTS.labels(request.method, request.url.path, "500").inc()
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            # Never leak an internal error to the client. The trace id is
            # how an operator connects what they saw to what the logs hold.
            content={"detail": "internal error", "trace_id": trace_id})

    elapsed = time.perf_counter() - start
    # Label by ROUTE TEMPLATE, not the raw path. Using the raw path would
    # create a new metric series per camera id and blow up Prometheus
    # cardinality within a day.
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    metrics.REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    metrics.LATENCY.labels(request.method, path).observe(elapsed)

    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Response-Time-ms"] = f"{elapsed * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if remaining := getattr(request.state, "rate_remaining", None):
        response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response


API = "/api/v1"
for router in (auth.router, cameras.router, vehicles.router, intelligence.router,
               alerts.router, system.router):
    app.include_router(router, prefix=API, dependencies=[Depends(rate_limit)])


# ── health and metrics: unauthenticated by design ────────────────────
# A liveness probe that needs a token cannot run before the database is up,
# which is exactly when it matters most.

@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "service": "sentinel-api", "version": "1.0.0"}


@app.get("/health/ready", tags=["health"])
async def readiness():
    database = await db.health()
    ready = database.get("healthy", False)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"ready": ready, "database": database,
                 "websocket_clients": len(ws.manager.clients)})


@app.get("/metrics", tags=["health"])
async def prometheus_metrics():
    # Refresh the gauges from the database on scrape rather than
    # maintaining them on every write. A scrape is cheap and infrequent;
    # keeping counters live in application state across replicas is not.
    with contextlib.suppress(Exception):
        row = await db.fetch_one("SELECT * FROM v_dashboard_stats")
        if row:
            metrics.CAMERAS_ONLINE.set(row["cameras_online"] or 0)
            metrics.CAMERAS_OFFLINE.set(row["cameras_offline"] or 0)
            metrics.ALERTS_OPEN.set(row["active_alerts"] or 0)
            metrics.VEHICLES_TRACKED.set(row["vehicles_tracked_1h"] or 0)
    metrics.WS_CLIENTS.set(len(ws.manager.clients))
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


# ── WebSocket ────────────────────────────────────────────────────────

@app.websocket("/api/v1/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None):
    """Live alerts, sightings and camera health.

    The token arrives as a query parameter because browsers cannot set an
    Authorization header on a WebSocket handshake. The token is short-lived
    and this is the standard workaround, but it does mean the token can
    appear in proxy access logs -- so in production the WebSocket endpoint
    should sit behind a reverse proxy configured not to log query strings.
    """
    import asyncio
    import json

    payload = decode_token(token or "", get_settings().secret_key)
    if not payload or payload.get("type") != "access":
        await websocket.close(code=4401, reason="invalid or expired token")
        return

    client = await ws.manager.connect(websocket, payload.get("username", "?"))
    pump = asyncio.create_task(ws.manager.pump(client))
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "invalid JSON"})
                continue

            action = message.get("action")
            if action == "subscribe":
                client.channels |= set(message.get("channels", []))
            elif action == "unsubscribe":
                client.channels -= set(message.get("channels", []))
            elif action == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            else:
                await websocket.send_json(
                    {"type": "error", "detail": f"unknown action '{action}'"})
                continue
            await websocket.send_json({"type": "subscribed",
                                       "channels": sorted(client.channels)})
    except WebSocketDisconnect:
        pass
    except Exception as e:                                # pragma: no cover
        log.warning("websocket error", extra={"error": str(e)})
    finally:
        pump.cancel()
        ws.manager.disconnect(client)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Sentinel VMS",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "websocket": "/api/v1/ws?token=<access_token>",
    }
