# Scaling to 80,000 Cameras

**The MVP is not a small version of the state system. It is one
district-scale cell of it, and the seam is already in the code.**

Every step below is a scaling exercise. None of them is a redesign, and that
is the claim worth checking.

---

## The arithmetic that decides everything

| Scale | Cameras | Aggregate bitrate | 30-day storage |
|---|---|---|---|
| MVP | 50 | 200 Mbps | 65 TB |
| District | 3,000 | 12 Gbps | 3.9 PB |
| **State** | **80,000** | **320 Gbps** | **104 PB** |

320 Gbps of sustained ingress into a central cloud is not a budget problem.
It will not be sanctioned, and it would not work if it were.

So: **video never centralises, metadata does.**

| | Video | Metadata |
|---|---|---|
| Per camera | 4 Mbps | ~5 kbps |
| At 80,000 cameras | 320 Gbps | **~400 Mbps** |

Three orders of magnitude. Everything else follows from that one decision.

---

## What changes, layer by layer

| Layer | 50 (Compose) | 3,000 (district) | 80,000 (state) |
|---|---|---|---|
| **Ingestion** | 1 process | 1 pod / ~250 cameras | K3s at ~2,000 edge sites |
| **AI** | simulation, CPU | ONNX, 1–2 GPUs/pod | GPU at every edge site |
| **Bus** | Redis Streams | Redis Cluster | Kafka, partitioned by camera |
| **Sightings** | PostgreSQL, partitioned | same, larger | ClickHouse |
| **Registry** | PostgreSQL | PostgreSQL | PostgreSQL (small, unchanged) |
| **Vectors** | pgvector | pgvector | Milvus, sharded by district |
| **Matcher** | 1 process | 3–6, one group | 1 consumer group per district |
| **API** | 1 | 3+ behind a Service | regional, behind a gateway |
| **Deployment** | `docker compose` | K8s | K8s core + K3s edge |

**No application code changes in that table.** Two things do change, and both
are flagged in the code where they live.

---

## The two things that must change with replica count

### 1 · Rate limiting must move to Redis

`backend/app/deps.py` keeps counters in process memory. N API replicas
therefore allow N × the configured limit. The interface does not change —
only the store.

### 2 · The WebSocket consumer group is per-pod, and must stay that way

`backend/app/ws.py` uses `sentinel-api-ws-{hostname}`. This looks like a bug
and is not.

Every API replica must receive **every** alert, because each has its own set
of connected operators. A shared consumer group would deliver each alert to
exactly one replica, and the operators connected to the other replicas would
silently never see it — a control room quietly missing alerts, with nothing
in the logs.

The K8s README says this explicitly so a future engineer does not "fix" it.

---

## Why the gate is what makes 80,000 tractable

Cross-camera matching is naively O(sightings × live vehicles). At state
scale that is unbounded.

The spatio-temporal gate bounds it: only cameras reachable by road within a
plausible travel window are candidates. Measured on the demo estate,
**3.3 candidates out of 49** in a 3-minute window.

Critically, the candidate count does **not** grow with estate size. Adding
Surat's cameras does not add candidates for a vehicle in Ahmedabad, because
no road connects them within 5 minutes. **The gate makes the matcher's
per-sighting cost independent of total estate size**, which is the property
that makes 80,000 possible at all.

Two implementation details make it real:

1. The gate is used as an **index**, not a post-hoc filter. Vehicles are
   bucketed by reachable camera and time window, so a sighting only ever
   meets vehicles that could physically have produced it. Measured effect:
   344 pairs scored instead of ~500,000.
2. The adjacency query is **pushed into SQL**, so only candidate vehicles
   are read from the database at all.

---

## Sharding

The natural shard key is **district**, because the road network already
partitions the problem: a vehicle in Rajkot is not a candidate for a
sighting in Vadodara.

```
   Ahmedabad shard        Surat shard         Rajkot shard
   ├ ingestion pods       ├ ingestion pods    ├ ingestion pods
   ├ matcher group        ├ matcher group     ├ matcher group
   ├ ClickHouse shard     ├ ClickHouse shard  ├ ClickHouse shard
   └ Milvus shard         └ Milvus shard      └ Milvus shard
              │                  │                  │
              └──────── border cameras ─────────────┘
                   (in BOTH adjacent shards)
```

Border cameras belong to both adjacent shards. Their adjacency edges cross
the boundary, so a vehicle leaving Ahmedabad toward Vadodara is a candidate
in both — no special inter-shard protocol, just overlapping membership. This
falls out of the adjacency graph rather than needing new machinery.

---

## Storage at state scale

| Tier | Store | Retention | Why |
|---|---|---|---|
| Video | edge NVR / ring buffer | 7–30 days | Never uploaded wholesale. This is the decision that makes the whole thing affordable. |
| Sightings | ClickHouse | 90 days | ~800k rows/s state-wide; PostgreSQL is the wrong shape for that firehose |
| Registry | PostgreSQL | permanent | 80,000 rows. Small, relational, transactional |
| Vectors | Milvus, sharded | 30 days | ~50M+ live vectors exceeds pgvector's comfortable range |
| Evidence | S3 / MeghRaj | 1–7 years | Case-linked, on-demand only |
| Audit | PostgreSQL | 7 years | Outlives every other table (DPDP Act) |

The MVP already partitions by time and drops old data with `DROP PARTITION`
rather than `DELETE` — instant, no bloat, no vacuum storm. That mechanism is
unchanged at scale; only the row counts differ.

---

## Cost shape

Rough orders of magnitude, not a quotation.

| | Centralised video | Federated metadata |
|---|---|---|
| WAN | 320 Gbps sustained | ~400 Mbps |
| Central storage, 30 days | 104 PB | ~40 TB metadata + evidence |
| GPU | central, 2,000+ | edge, ~2,000 (same count, distributed) |
| Feasible? | **No** | Yes |

GPU count is comparable either way — you still have to decode 80,000 streams
somewhere. The difference is that decoding at the edge means the pixels never
traverse the WAN, and the pixels are the expensive part.

---

## What we would need to prove next

The honest gap: this has been measured at 50 cameras, not 3,000.

1. **Shard the matcher by district** and demonstrate that per-sighting cost
   stays flat as estate size grows. This is the central claim and it is
   currently argued rather than measured.
2. **Replace the sightings consumer target with ClickHouse** and re-measure
   write throughput.
3. **Run one real edge node** (Jetson Orin) with real cameras and real
   models, and measure decode capacity against the 30–45 sub-streams-per-GPU
   estimate.
4. **Measure Kafka at district partition counts** rather than assuming
   Redis Streams semantics carry over.

Points 1 and 3 are the ones that could change the design. The rest is
engineering.
