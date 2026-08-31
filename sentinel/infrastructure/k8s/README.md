# Kubernetes deployment

**The MVP does not need this.** `docker compose up` is the supported path
for the hackathon and for any single-district pilot; these manifests are the
scale-out path and are included so the architecture claim is checkable
rather than asserted.

## What changes at scale, and what does not

The application code is identical. What changes is how many of each service
run and where.

| | 50 cameras (Compose) | District (~3,000) | State (80,000+) |
|---|---|---|---|
| Ingestion | 1 process | 1 pod per ~250 cameras | K3s at each edge site |
| AI backend | simulation | ONNX on 1–2 GPUs | GPU nodes at the edge |
| Event processor | 1 | 3–6, sharded by district | 1 consumer group per district |
| Bus | Redis Streams | Redis Cluster | Kafka, partitioned by camera |
| Sightings store | PostgreSQL | PostgreSQL, partitioned | ClickHouse |
| Vectors | pgvector | pgvector | Milvus, sharded |
| API | 1 | 3+ behind a Service | Regional, behind a gateway |

Two things must change with replica count and are called out in the code:

1. **Rate limiting is in-process** (`backend/app/deps.py`). N API replicas
   allow N times the configured limit. Move it to Redis before scaling out.
2. **The WebSocket consumer group is per-pod** (`backend/app/ws.py`), which
   is deliberate: every replica must receive every alert, because each has
   its own connected operators. A shared group would deliver each alert to
   exactly one replica and the rest of the control room would silently miss
   it. This is correct as written — do not "fix" it.

## Apply

```bash
kubectl create namespace sentinel

# Secrets first. Never commit these.
kubectl -n sentinel create secret generic sentinel-secrets \
  --from-literal=SECRET_KEY="$(openssl rand -base64 48)" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 24)"

kubectl -n sentinel apply -f .
kubectl -n sentinel rollout status deploy/sentinel-api
```

PostgreSQL here is a single StatefulSet, which is fine for a pilot and not
for production. Use a managed PostGIS instance (or CloudNativePG) with
point-in-time recovery: this database holds evidence.
