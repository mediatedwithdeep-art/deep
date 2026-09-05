from __future__ import annotations

from .base import Bus


def create_bus(backend: str = "redis", *, redis_url: str = "redis://localhost:6379/0",
               kafka_bootstrap: str = "localhost:9092") -> Bus:
    """Build the configured bus. The only place a backend is named."""
    match backend:
        case "memory":
            from .memory import MemoryBus
            return MemoryBus()
        case "redis":
            from .redis_bus import RedisBus
            return RedisBus(redis_url)
        case "kafka":
            from .kafka_bus import KafkaBus
            return KafkaBus(kafka_bootstrap)
        case _:
            raise ValueError(f"unknown bus backend: {backend!r} "
                             "(expected 'memory', 'redis' or 'kafka')")
