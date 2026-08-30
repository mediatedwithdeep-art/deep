"""Message bus abstraction.

Why an abstraction rather than "just use Kafka": the MVP must run on a
laptop with one command. Kafka (even Redpanda) is another service to
operate, and at 50 cameras it buys nothing that Redis Streams does not
already provide -- consumer groups, persistence, at-least-once delivery,
and replay from an offset.

At 80,000 cameras the calculus flips (see docs/SCALING.md) and Kafka's
partition-level parallelism and retention become necessary. Switching is
then a single environment variable, because no application code imports a
broker client directly -- everything goes through `Bus`.

Backends:
    memory  -- in-process, for unit tests. No external service.
    redis   -- Redis Streams. The MVP default.
    kafka   -- aiokafka. The scale-out path.
"""

from .base import Bus, BusMessage, Topics
from .factory import create_bus

__all__ = ["Bus", "BusMessage", "Topics", "create_bus"]
