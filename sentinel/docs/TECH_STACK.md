# 02 — Tech Stack

Selection criteria, in order: **(1)** permissive licence — this is a government deployment and AGPL in the inference path is a procurement blocker; **(2)** single-binary / low-operator-burden; **(3)** a credible path from 50 → 80,000 cameras without a rewrite; **(4)** buildable by a small team in eight days.

---

## Recommended stack

| Layer | MVP choice | Licence | State-scale evolution |
|---|---|---|---|
| Stream ingest / decode | **GStreamer 1.24** (+ NVIDIA DeepStream 7.x optional) | LGPL-2.1 | Same, sharded per edge node |
| Media server (view path) | **MediaMTX** | MIT | Sharded fleet + Pion/Janus SFU |
| Message broker | **Redpanda** (Kafka API) | BSL→Apache | Kafka/Redpanda, partitioned by district |
| Stream processing | Python consumers + **Faust-streaming** / plain `aiokafka` | Apache/BSD | Flink for stateful cross-district joins |
| Inference serving | **NVIDIA Triton 24.x** (TensorRT FP16/INT8) | BSD-3 | Triton fleet + KServe |
| Relational + spatial + TS | **PostgreSQL 16 + PostGIS 3.4 + TimescaleDB + pgvector** | PostgreSQL/Apache | Split: Postgres (registry) + ClickHouse (sightings) + Milvus (vectors) |
| Object store | **MinIO** | AGPL (server, unmodified use OK) | S3 / NIC MeghRaj object store |
| Cache / hot state | **Redis 7** / Valkey | BSD | Redis Cluster |
| Routing / travel time | **OSRM** on Gujarat OSM extract | BSD-2 | Same, pre-baked matrices |
| API | **FastAPI + Uvicorn** (Python 3.12) | MIT | Same; Go for ingest supervisor |
| Realtime push | FastAPI WebSocket (MVP) → **Centrifugo** | MIT | Centrifugo cluster |
| Frontend | **React 18 + TypeScript + Vite** | MIT | Same |
| Map | **MapLibre GL JS + deck.gl** | BSD-3/MIT | Same |
| Basemap | **ISRO Bhuvan WMTS** + OSM fallback, self-hosted tiles | Open | Self-hosted vector tiles (air-gapped) |
| Video in browser | WebRTC **WHEP** via MediaMTX | — | Same |
| Identity | **Keycloak** (OIDC, department realms) | Apache-2.0 | Same + SPIFFE for machine identity |
| Orchestration | Docker Compose (MVP) → **K3s** | Apache-2.0 | K8s / K3s at edge |
| Observability | Prometheus + Grafana + Loki | Apache-2.0 | Same + Tempo |

---

## Why these, specifically

**Redpanda over Kafka for the MVP.** Kafka-wire-compatible, single binary, no ZooKeeper/KRaft ceremony, no JVM tuning. You get the Kafka API — so you are never locked in — with roughly a tenth of the operational surface. Eight days is not enough time to debug a JVM GC pause. Swap to Apache Kafka at state scale if procurement insists; not a line of application code changes.

**MediaMTX for the view path.** One Go binary that ingests RTSP/RTMP/SRT/WebRTC and republishes as any of them, including WHEP for browsers. It is the shortest path from "50 RTSP URLs" to "50 tiles in a React grid at sub-second latency." Its on-demand mode (`runOnDemand`) is important: it only pulls a camera when someone is actually watching it, which is exactly the federated model from doc 01.

**PostGIS + TimescaleDB + pgvector in one Postgres.** Three services collapsed into one. TimescaleDB hypertables give automatic time partitioning and continuous aggregates for the sightings firehose; PostGIS gives the spatial gating; pgvector with an HNSW index handles ReID search comfortably to a few million vectors. At 50 cameras you will have ~200k embeddings/day — pgvector handles that with room to spare, and you avoid running Milvus during a hackathon. Plan the migration to Milvus/Qdrant at roughly 50M+ live vectors.

**Triton for inference.** Dynamic batching is the whole game. 50 cameras × 10 fps = 500 inferences/s; served one at a time a T4 will choke, batched at 16 it is idle. Triton also gives you model versioning and A/B — which matters when you swap the plate recogniser mid-competition and need to prove the new one is better.

**MapLibre over Mapbox GL.** Mapbox GL JS went proprietary at v2 and requires a token and a network call per session. For a police system that may run air-gapped on GSWAN, MapLibre + self-hosted tiles is the only defensible choice. **Bhuvan (ISRO) WMTS** for the basemap is a genuine differentiator in an Indian government evaluation: Indian imagery, Indian hosting, no foreign dependency, and it satisfies data-localisation questions before they are asked.

**Keycloak with a realm per department.** 26 departments with different authorities over different camera subsets is an access-control problem, not a feature. Model it as: `department → role → camera_group → permission`. Home Department sees everything; a municipal corporation sees its own cameras plus a break-glass request path. Get this in the schema on day 1 even though the MVP demo will run as a single admin, because retrofitting multi-tenancy is a rewrite.

---

## Licence warnings (read before you commit code)

| Component | Licence | Why it matters here |
|---|---|---|
| **Ultralytics YOLOv8/YOLO11** | **AGPL-3.0** | AGPL obliges source disclosure for network-delivered services. For a state-government deployment this is usually a procurement blocker. Fine for a hackathon demo; **flag it explicitly in your submission and name the migration path**, which is credibility rather than a weakness. |
| YOLOX, RT-DETR, D-FINE, DAMO-YOLO | Apache-2.0 | Safe. Use these for anything that ships. |
| ByteTrack | MIT | Safe. |
| PaddleOCR | Apache-2.0 | Safe. |
| PARSeq | Apache-2.0 | Safe. |
| DeepSORT (original) | GPL-3.0 | Avoid; ByteTrack/BoT-SORT are better and permissive anyway. |
| MinIO server | AGPL-3.0 | Unmodified use as a service is fine; do not link or fork into your code. |
| NVIDIA DeepStream | Proprietary EULA, free to use | Allowed, but it is a hard NVIDIA lock-in. Keep the plain-GStreamer path working as your escape hatch. |

**Recommendation:** build the demo with whatever is fastest, but keep the detector behind an interface (`services/cv/detector.py`) so swapping Ultralytics → YOLOX is a config change. Then say so in the submission. Evaluators for government challenges notice licence awareness; almost no hackathon team demonstrates it.

---

## Hardware for the 50-camera MVP

| Item | Spec | Qty | Notes |
|---|---|---|---|
| GPU node | 1× NVIDIA T4 / RTX 4000 Ada, 16 GB, 16 vCPU, 64 GB RAM | 1–2 | Decode + inference. One handles 50 sub-streams; two gives headroom and a failover story. |
| Core services node | 16 vCPU, 64 GB RAM, 1 TB NVMe | 1 | Postgres, Redpanda, Redis, MinIO, API |
| Edge node (demo) | Jetson Orin NX 16 GB **or** NUC i7 | 1–2 | Proves the federated model on stage |
| Video encoder | 4-ch analog→RTSP (Axis M7104 / Hikvision DS-6704) | 1 | Proves the analog bridge on stage |

Total: two servers plus one edge box demonstrates the entire architecture end to end. If cloud, an `g4dn.2xlarge`-class GPU instance plus an `m6i.4xlarge` is the equivalent.

**A note on the demo itself:** do not depend on 50 live cameras being reachable on competition day. Build the system so that a camera is a URL, then run ~35 of the 50 as looped RTSP republishes of pre-recorded footage through MediaMTX, and 10–15 as genuinely live feeds (phones via RTSP apps, a few real IP cameras, one analog camera through the encoder). This is not cheating — it is the standard way this class of system is tested, and it means a venue Wi-Fi failure does not end your demo. Say so openly; a reproducible test harness is a strength.
