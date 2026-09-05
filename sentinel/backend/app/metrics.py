"""Prometheus metrics.

Deliberately small. A metric nobody looks at is a metric that misleads
during an incident, so this covers only what an operator or an SRE would
actually page on: request rate and latency, error rate, live camera and
alert counts, and the pipeline's own throughput.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest,
)

REQUESTS = Counter(
    "sentinel_http_requests_total", "HTTP requests",
    ["method", "path", "status"])

LATENCY = Histogram(
    "sentinel_http_request_seconds", "HTTP request latency",
    ["method", "path"],
    # Buckets chosen around what matters here: a command-centre tile should
    # render in well under 250 ms, and anything past 2 s reads as broken.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0))

WS_CLIENTS = Gauge("sentinel_websocket_clients", "Connected WebSocket clients")
WS_DROPPED = Counter("sentinel_websocket_dropped_total",
                     "Messages dropped for slow clients")

CAMERAS_ONLINE = Gauge("sentinel_cameras_online", "Cameras reporting healthy")
CAMERAS_OFFLINE = Gauge("sentinel_cameras_offline", "Cameras not reporting")
ALERTS_OPEN = Gauge("sentinel_alerts_open", "Unacknowledged alerts")
VEHICLES_TRACKED = Gauge("sentinel_vehicles_tracked_1h",
                         "Distinct vehicles seen in the last hour")

AUTH_FAILURES = Counter("sentinel_auth_failures_total", "Failed logins", ["reason"])
RATE_LIMITED = Counter("sentinel_rate_limited_total", "Rate-limited requests")


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
