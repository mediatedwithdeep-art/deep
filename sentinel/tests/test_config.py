"""Configuration tests.

These guard the boundary between docker-compose.yml and the settings model.
A mismatch there does not fail loudly at build time -- it fails at container
start, on demo day, with an error that names neither the variable nor the
cause.
"""
from __future__ import annotations

import pytest

from sentinel_core.config import Settings


def _settings(**env) -> Settings:
    """Build settings from an explicit environment, ignoring any .env file."""
    import os
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.startswith(("POSTGRES_", "REDIS_", "DEMO_", "AI_", "INGEST_",
                             "BUS_", "CORS_", "SECRET_", "ENVIRONMENT",
                             "MATCHER_", "ALERT_", "LOG_")):
                del os.environ[k]
        os.environ.update({k: str(v) for k, v in env.items()})
        return Settings(_env_file=None)
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ── the compose contract ─────────────────────────────────────────────

def test_every_env_var_used_by_compose_maps_to_a_setting():
    """docker-compose.yml sets these by name. If a field is renamed without
    updating compose, the service silently runs on defaults."""
    s = _settings(
        ENVIRONMENT="demo", INGEST_MODE="hybrid", DEMO_VEHICLE_COUNT="900",
        DEMO_TICK_HZ="8", DEMO_TIME_SCALE="2", DEMO_CAMERA_COUNT="40",
        AI_BACKEND="onnx", AI_DEVICE="cpu", AI_TARGET_FPS="10",
        BUS_BACKEND="redis", MATCHER_TICK_SECONDS="2.5",
        MATCHER_TRACK_TTL_SECONDS="600", ALERT_DEDUP_SECONDS="90",
        POSTGRES_HOST="postgres", POSTGRES_PORT="5432", POSTGRES_DB="sentinel",
        POSTGRES_USER="sentinel", POSTGRES_PASSWORD="x",
        REDIS_HOST="redis", REDIS_PORT="6379",
        SECRET_KEY="k" * 40, LOG_LEVEL="DEBUG", LOG_FORMAT="console",
    )
    assert s.ingest_mode == "hybrid"
    assert s.demo_vehicle_count == 900
    assert s.demo_tick_hz == 8.0
    assert s.demo_time_scale == 2.0
    assert s.ai_backend == "onnx"
    assert s.ai_target_fps == 10.0
    assert s.matcher_tick_seconds == 2.5
    assert s.matcher_track_ttl_seconds == 600
    assert s.alert_dedup_seconds == 90
    assert s.postgres_host == "postgres"
    assert s.redis_host == "redis"
    assert s.log_format == "console"


def test_cors_origins_accepts_a_comma_separated_environment_value():
    """pydantic-settings json.loads() list fields from the environment BEFORE
    any validator runs, so an ordinary comma-separated value crashes the
    process at startup unless the field is annotated NoDecode. This is the
    exact shape docker-compose passes."""
    s = _settings(CORS_ORIGINS="http://a:3000,http://b:4173",
                  SECRET_KEY="k" * 40)
    assert s.cors_origins == ["http://a:3000", "http://b:4173"]


def test_default_cors_covers_both_hostname_forms_and_both_vite_ports():
    """Browsers treat localhost and 127.0.0.1 as different origins, and the
    Vite dev server (5173) and preview server (4173) are different ports.
    Missing any of them produces a CORS failure that reads as a broken API."""
    origins = _settings(SECRET_KEY="k" * 40).cors_origins
    for host in ("localhost", "127.0.0.1"):
        for port in (5173, 4173):
            assert f"http://{host}:{port}" in origins


def test_database_url_is_built_from_the_parts():
    s = _settings(POSTGRES_HOST="db", POSTGRES_PORT="6000", POSTGRES_DB="sen",
                  POSTGRES_USER="u", POSTGRES_PASSWORD="p", SECRET_KEY="k" * 40)
    assert s.database_url == "postgresql://u:p@db:6000/sen"
    assert s.async_database_url.startswith("postgresql+asyncpg://")


def test_redis_url_includes_a_password_only_when_set():
    assert _settings(REDIS_HOST="r", SECRET_KEY="k" * 40).redis_url == "redis://r:6379/0"
    assert _settings(REDIS_HOST="r", REDIS_PASSWORD="s",
                     SECRET_KEY="k" * 40).redis_url == "redis://:s@r:6379/0"


# ── production guards ────────────────────────────────────────────────

def test_production_refuses_to_start_without_a_secret_key():
    """Generating a key would invalidate every token on restart and give
    each replica a different one, so sessions would break unpredictably
    rather than fail loudly."""
    with pytest.raises(ValueError, match="SECRET_KEY"):
        _settings(ENVIRONMENT="production", POSTGRES_PASSWORD="a-strong-one")


@pytest.mark.parametrize("weak", ["sentinel", "postgres", "password", "changeme", "admin"])
def test_production_refuses_a_known_weak_database_password(weak):
    with pytest.raises(ValueError, match="weak"):
        _settings(ENVIRONMENT="production", SECRET_KEY="k" * 40,
                  POSTGRES_PASSWORD=weak)


def test_production_starts_with_real_secrets():
    s = _settings(ENVIRONMENT="production", SECRET_KEY="k" * 40,
                  POSTGRES_PASSWORD="a-genuinely-strong-password")
    assert s.environment == "production"


def test_demo_generates_an_ephemeral_key_rather_than_shipping_one():
    """A fixed default secret in the repository is the most common way a
    system like this is compromised: it reaches production untouched."""
    a = _settings(ENVIRONMENT="demo").secret_key
    b = _settings(ENVIRONMENT="demo").secret_key
    assert a and b and a != b, "the demo key must be generated, not constant"
    assert len(a) >= 40


def test_reid_dimension_matches_the_database_column():
    """The vehicle.embedding column is VECTOR(512) to match OSNet-AIN's
    native output. A mismatch here fails only on the first insert."""
    assert _settings(SECRET_KEY="k" * 40).reid_dim == 512
