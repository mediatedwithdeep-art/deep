"""Central configuration. Every service reads settings from the environment.

Rule: no secret ever has a usable default. `SECRET_KEY` and `POSTGRES_PASSWORD`
have no default at all in production mode -- the service refuses to start
rather than run on a guessable key. Camera credentials never live here; they
are referenced by `credential_ref` and resolved from the secret store.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        env_nested_delimiter="__",
    )

    # ── Environment ──────────────────────────────────────────────────
    environment: Literal["development", "demo", "production"] = "demo"
    debug: bool = False
    service_name: str = "sentinel"

    # ── Database ─────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "sentinel"
    postgres_user: str = "sentinel"
    postgres_password: str = "sentinel"
    postgres_pool_min: int = 2
    postgres_pool_max: int = 20

    @property
    def database_url(self) -> str:
        return (f"postgresql://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}")

    @property
    def async_database_url(self) -> str:
        return (f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}")

    # ── Message bus ──────────────────────────────────────────────────
    # Redis Streams for the MVP: consumer groups, persistence, at-least-once
    # delivery, and one fewer service to operate. `kafka` swaps the backend
    # without touching application code -- see sentinel_core/bus/.
    bus_backend: Literal["redis", "kafka", "memory"] = "redis"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    kafka_bootstrap: str = "localhost:9092"

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # ── Security ─────────────────────────────────────────────────────
    secret_key: str = Field(default="", description="JWT signing key")
    access_token_ttl_minutes: int = 60
    refresh_token_ttl_days: int = 7
    password_min_length: int = 12
    rate_limit_per_minute: int = 300
    # Both the Vite dev server (5173) and its preview server (4173), and
    # both hostname forms. Browsers treat localhost and 127.0.0.1 as
    # different origins, so listing only one produces a CORS failure that
    # looks like a broken API rather than a config gap.
    # NoDecode is load-bearing. Without it pydantic-settings tries to
    # json.loads() the environment value BEFORE any validator runs, so a
    # perfectly ordinary CORS_ORIGINS=http://a,http://b crashes the process
    # at startup with a JSONDecodeError that names neither the variable nor
    # the cause. The validator below accepts the comma-separated form.
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:4173", "http://127.0.0.1:4173",
        "http://localhost:3000", "http://127.0.0.1:3000",
    ]

    # ── Object storage (evidence) ────────────────────────────────────
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "sentinel"
    s3_secret_key: str = "sentinel"
    s3_bucket: str = "sentinel-evidence"
    s3_secure: bool = False

    # ── AI pipeline ──────────────────────────────────────────────────
    # `simulation` needs no model weights and no GPU: it is what makes the
    # demo runnable on a laptop. `onnx` runs real models on CPU or GPU.
    ai_backend: Literal["simulation", "onnx"] = "simulation"
    ai_device: Literal["auto", "cpu", "cuda"] = "auto"
    ai_target_fps: float = 6.0
    ai_batch_size: int = 8
    detector_model: str = "yolov8n"
    detector_conf: float = 0.35
    reid_dim: int = 512
    anpr_min_plate_px: int = 90
    anpr_min_blur_var: float = 100.0

    # ── Ingestion ────────────────────────────────────────────────────
    ingest_mode: Literal["demo", "live", "hybrid"] = "demo"
    demo_camera_count: int = 50
    demo_vehicle_count: int = 1800
    demo_tick_hz: float = 6.0
    # Scales the demo CLOCK, not vehicle speed -- see world.py.
    demo_time_scale: float = 3.0
    rtsp_transport: Literal["tcp", "udp"] = "tcp"
    camera_config_path: str = "config/cameras.yaml"
    stream_probe_timeout_s: int = 12

    # ── Matcher ──────────────────────────────────────────────────────
    matcher_tick_seconds: float = 1.0
    matcher_track_ttl_seconds: int = 900
    alert_dedup_seconds: int = 60

    # ── Observability ────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    metrics_enabled: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _enforce_secrets(self):
        if not self.secret_key:
            if self.environment == "production":
                raise ValueError(
                    "SECRET_KEY must be set in production. Refusing to start "
                    "with a generated key: tokens would be invalidated on every "
                    "restart and would differ between replicas."
                )
            # Dev/demo convenience only, and deliberately ephemeral.
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(48))
        if self.environment == "production":
            weak = {"sentinel", "postgres", "password", "changeme", "admin"}
            if self.postgres_password.lower() in weak:
                raise ValueError("POSTGRES_PASSWORD is a known-weak value; refusing to start in production.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
