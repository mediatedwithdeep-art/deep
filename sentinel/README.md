# Sentinel — Unified VMS & AI Analytics

**Gujarat Police Sentinel Innovation Challenge 2026**

One pane of glass over a heterogeneous camera estate: ingest disparate
feeds, detect and track vehicles, read plates, follow one vehicle across
cameras, alert in real time, and show its movement on a map.

```bash
git clone <this repo> && cd sentinel
make demo
```

That is the whole setup. It builds, starts PostgreSQL/PostGIS/pgvector,
Redis, the API, the AI pipeline, the event processor and the command centre,
applies migrations, seeds a 50-camera Ahmedabad estate, and prints your
login. **No GPU, no model weights, no cameras required.**

- Command centre → `http://localhost:3000`
- API docs → `http://localhost:8000/docs`

Presenting it? → **[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)**
Connecting real cameras? → **[docs/REAL_CAMERAS.md](docs/REAL_CAMERAS.md)**

---

## The three claims this is built on

**1 · Video never centralises. Metadata does.**
80,000 cameras at 4 Mbps is **320 Gbps and 104 PB/month**. That is not a
budget problem, it is physics, and no centralised design survives it. Video
stays at the edge; ~5 kbps per camera of metadata reaches the core; clips are
pulled on demand. Every other decision follows.

**2 · The spatio-temporal gate matters more than the model.**
Given a sighting, only cameras reachable by road in a plausible travel window
are candidates. Measured on the demo estate: **1.2 candidates out of 49**
within a 3-minute window (**3.3** within 5 minutes) — a 97.6% reduction. Appearance matching at 0.9% false
positives is unusable against 49 cameras and trustworthy against 3. It is a
PostGIS query plus a cached routing matrix. No GPU.

**3 · ANPR alone cannot track a vehicle across a real estate.**
Only 26% of the demo estate can physically resolve a plate; the rest are
wide-angle. Requiring a plate read at every hop yields a dotted line. Sentinel
fuses plate (precision) with appearance and attributes (recall), under the gate.

---

## What is actually built

| | |
|---|---|
| **Tests** | **359 collected** — unit, integration against a live PostgreSQL, live-feed tests against a real RTSP server, plus dedicated Sentinel-contract and security-regression suites |
| Database | 10 migrations, PostGIS + pgvector, range partitioning, HOT/WARM/COLD tiering |
| API | 45 operations across 41 paths, JWT + RBAC, WebSocket, audit log, Prometheus |
| AI pipeline | detect → track → quality gate → ANPR + ReID → sighting |
| Ingestion | RTSP / ONVIF / HLS / DVR adapters + a 1,800-vehicle traffic world |
| Event processor | cross-camera identity, 8 configurable alert rules |
| Frontend | 8 pages, React + MapLibre, verified in a real browser |
| Deployment | one-command Compose; Kubernetes manifests for scale-out |

Measured, not asserted — `make benchmark`:

| | |
|---|---|
| Whole 50-camera estate | 14.2–22.0 ms/tick, **9–13% of one core** (three hosts; see [BENCHMARKS.md](docs/BENCHMARKS.md)) |
| ANPR, dedicated lane, day | **92.5%** end-to-end |
| ANPR, wide-angle, night | **37.9%** — published because it is true |
| Gate reduction (3-min window) | **97.6%** fewer comparisons — 20,000 pairs → 473, **2,240 ms → 53 ms** |
| ReID same-ID vs different-ID | 0.721 ± 0.112 vs 0.361 ± 0.078 |

Full results and method: **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)**

---

## How it fits together

```
 CAMERAS                    EDGE                          CORE
 ───────                    ────                          ────
 IP / RTSP  ─┐
 ONVIF      ─┤   ┌────────────────────────┐      ┌──────────────────────┐
 Analog/DVR ─┼──▶│ decode (ffmpeg/NVDEC)  │      │  Event processor     │
 HLS        ─┤   │ detect  → YOLOX        │      │  ├ cross-camera match│
 Vendor VMS ─┘   │ track   → ByteTrack    │      │  ├ alert rules       │
                 │ QUALITY GATE ~80% out  │      │  └ persist + publish │
                 │ ANPR + ReID + colour   │      └──────────┬───────────┘
                 └───────────┬────────────┘                 │
                             │ Sightings (~5 kbps/camera)   │
                             ▼                              ▼
                     ┌───────────────┐          ┌───────────────────────┐
                     │ Redis Streams │─────────▶│ PostgreSQL + PostGIS  │
                     │ (→ Kafka)     │          │ + pgvector, partitioned│
                     └───────┬───────┘          └───────────┬───────────┘
                             │                              │
                             ▼                              ▼
                     ┌───────────────────────────────────────────┐
                     │  API (FastAPI)  ── WebSocket ──▶ Command  │
                     │  45 operations, RBAC, audit      Centre   │
                     └───────────────────────────────────────────┘

  Video NEVER crosses the edge/core boundary except as an on-demand clip.
  Browsers fetch video directly from the media server over WebRTC.
```

Detail: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

---

## Repository layout

```
sentinel/
├── shared/sentinel_core/   domain models, config, logging, message bus,
│                           plate rules, cross-camera fusion scoring
├── ai/sentinel_ai/         detector, tracker, quality gate, ANPR, ReID
│                           backends: simulation (no GPU) | onnx (real)
├── video-ingestion/        camera adapters, ffmpeg reader, worker
│                           supervisor, and the demo traffic world
├── event-processor/        cross-camera matcher, alert engine, persistence
├── backend/                FastAPI: auth, cameras, search, tracking,
│                           alerts, analytics, WebSocket
├── frontend/               React + TypeScript + MapLibre command centre
├── database/               migrations, migration runner, Ahmedabad seed
├── config/cameras.yaml     ← THE file you edit for real cameras
├── infrastructure/         nginx, Prometheus, Grafana, Kubernetes
├── scripts/benchmark.py    every number in the docs comes from here
├── tests/                  359 tests
└── docs/
```

---

## Running it

```bash
make demo            # everything, one command
make logs            # follow the pipeline
make down            # stop (keeps data)
make clean           # stop and delete data
make test            # 359 tests
make verify          # the ten verification questions, answered by running it
make benchmark       # measure it yourself
make observability   # + Prometheus and Grafana
make help            # all targets
```

Host-native development, without Docker:

```bash
make dev-db          # just PostgreSQL + Redis
make migrate seed
make dev-api         # in one terminal
make dev-ingestion   # in another
make dev-processor   # in another
make dev-web         # in another
```

---

## Connecting real Sentinel Gujarat cameras

Edit **`config/cameras.yaml`**, then `make hybrid`. No source changes, no
rebuild. Credentials are referenced by name and resolved from the
environment or a secret store — inline passwords are refused outright when
`ENVIRONMENT=production`.

```yaml
- camera_id: AHM-SAT-101
  name: Jodhpur Cross Roads - NE approach
  latitude: 23.02705
  longitude: 72.51192
  heading_deg: 47          # capture this during survey; see below
  protocol: RTSP
  substream_url: rtsp://10.42.7.14:554/Streaming/Channels/102
  credential_ref: env:SENTINEL_CAM_AHM101
```

`heading_deg` is not optional in practice: without it a camera is a dot with
no field of view, and because the adjacency graph is directional the
cross-camera gate is materially weaker. One compass reading per camera during
survey; very expensive to retrofit across thousands of sites.

Full guide, including analog DVRs and the security model:
**[docs/REAL_CAMERAS.md](docs/REAL_CAMERAS.md)**

---

## Things worth knowing before you judge it

**It tells the truth about uncertainty.** Every cross-camera hop is labelled
*confirmed* (plate-verified) or *probable* (appearance), with the full score
breakdown one click away. Plate search is fuzzy by design and says so. The
false-positive rate is displayed, not hidden. Camera Health reports how much
of the estate physically cannot read a plate.

**Privacy is in the schema, not the roadmap.** Viewing a vehicle's movement
history requires a stated purpose, which is written to a 7-year audit log
(DPDP Act 2023 purpose limitation). Reading the audit log is itself audited.
No table can hold a camera password.

**It runs air-gapped.** No font CDN, no map token, no external model service.
When the tile server is unreachable the overlays still render — that is the
correct degraded behaviour for a control room, and it is tested.

**Known limits are documented, not buried:**
**[docs/LIMITATIONS.md](docs/LIMITATIONS.md)**

---

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Federation, ingest, decode-once fan-out, latency budgets |
| [CV_PIPELINE.md](docs/CV_PIPELINE.md) | Models, Indian ANPR, ReID, fusion scoring, compute budget |
| [TECH_STACK.md](docs/TECH_STACK.md) | Component choices with rationale and **licence analysis** |
| [LEGACY_INTEGRATION.md](docs/LEGACY_INTEGRATION.md) | Analog DVRs, edge gateways, DPDP / BSA §63 |
| [SCALING.md](docs/SCALING.md) | The path from 50 to 80,000 cameras |
| [BENCHMARKS.md](docs/BENCHMARKS.md) | Measured performance and method |
| [SENTINEL_LIVE_TEST_REPORT.md](docs/SENTINEL_LIVE_TEST_REPORT.md) | 50 cameras over live RTSP: PTS, codecs, reconnection |
| [SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) | **Adversarial test of the authorisation boundary: 18 findings, how each was proved, and the fix** |
| [PHASE2_AUDIT.md](docs/PHASE2_AUDIT.md) | What was executed, and what is still pending |
| [REQUIREMENT_TRACEABILITY_MATRIX.md](docs/REQUIREMENT_TRACEABILITY_MATRIX.md) | **Every requirement → implementation → test → status.** Start here to check a claim |
| [GOVERNMENT_INTEGRATION.md](docs/GOVERNMENT_INTEGRATION.md) | VAHAN / SARTHI / eGujCop / AFIS / NAFIS adapters, and why none is connected |
| [SCALE_BENCHMARK.md](docs/SCALE_BENCHMARK.md) | 50 → 1,000 cameras measured; capacity to 80,000 with per-cell provenance |
| [NETWORK_BANDWIDTH_PLAN.md](docs/NETWORK_BANDWIDTH_PLAN.md) | Centralised vs federated vs hybrid, with the arithmetic |
| [DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md) | Nine failure domains, RPO/RTO, behaviour at 1 min / 10 min / 1 h |
| [INFRASTRUCTURE_SIZING.md](docs/INFRASTRUCTURE_SIZING.md) | Node archetypes, build-out to 80,000, storage tiers and retention |
| [COST_BENEFIT.md](docs/COST_BENEFIT.md) | Federated vs centralised, with every unit cost labelled an estimate |
| [STATEWIDE_ROLLOUT.md](docs/STATEWIDE_ROLLOUT.md) | Five phases, per-district sequence, and what must be true before Phase 1 |
| [LIMITATIONS.md](docs/LIMITATIONS.md) | What this does not do |
| [REAL_CAMERAS.md](docs/REAL_CAMERAS.md) | Connecting live feeds |
| [DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | Click-by-click presentation runbook |
| [ROADMAP.md](docs/ROADMAP.md) | Day-by-day build plan |
| [api/openapi.yaml](docs/api/openapi.yaml) | API contract |

---

## Licence note

The ONNX backend deliberately avoids the Ultralytics package (**AGPL-3.0**),
which is normally a procurement blocker for a state-government deployment. It
loads a plain ONNX graph, so YOLOX, RT-DETRv2 and D-FINE (all Apache-2.0)
drop straight in. Full table in [TECH_STACK.md](docs/TECH_STACK.md).
