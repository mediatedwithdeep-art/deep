# Kafka Event Contracts

All topics are `sentinel.<name>.v<major>`. Schemas are JSON Schema here for
readability; register the Avro equivalents in Schema Registry for production
(compact wire format matters at 800k msg/s state-wide).

**Rule: Kafka carries metadata and pointers. Never pixels.** Crops go to
Redis (short TTL) or MinIO; the message carries the key.

| Topic | Key | Partitions (MVP / state) | Retention | Producer |
|---|---|---|---|---|
| `sentinel.sightings.v1` | `camera_id` | 12 / 512 | 6 h | edge CV |
| `sentinel.tracklets.v1` | `camera_id` | 12 / 512 | 24 h | edge CV |
| `sentinel.reid.v1` | `camera_id` | 12 / 256 | 1 h | edge CV |
| `sentinel.alerts.v1` | `target_id` | 6 / 64 | 30 d | matcher |
| `sentinel.camera.health.v1` | `camera_id` | 3 / 64 | 24 h | edge agent |
| `sentinel.commands.v1` | `gateway_code` | 3 / 64 | 7 d | core → edge |

Partition by `camera_id` so one camera's events stay ordered and the matcher
can shard by geography without reshuffling.

**At state scale, do not publish per-frame sightings.** The edge aggregates a
tracklet (one vehicle, one camera, entry→exit) and publishes that plus its
best 1–3 crops. This is a ~40× volume reduction and loses nothing the matcher
needs.

## Envelope

Every message carries the same envelope so consumers can route, trace and
reject uniformly:

```json
{
  "schema": "sentinel.sightings.v1",
  "event_id": "uuid",
  "produced_at": "RFC3339 with microseconds",
  "gateway_code": "EDGE-AHM-03",
  "trace_id": "W3C traceparent",
  "payload": { }
}
```
