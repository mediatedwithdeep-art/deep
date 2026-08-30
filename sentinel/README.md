# Sentinel — Unified VMS & AI Analytics Ecosystem

Technical blueprint and working scaffold for the **Gujarat Police Sentinel Innovation Challenge 2026**.

**MVP target (7 Sep 2026):** 50 heterogeneous live camera feeds, AI vehicle tracking across them, real-time alerts, GIS movement history.
**Design target:** 80,000+ cameras, 26 departments, state-wide.

---

## The three claims this design rests on

**1. Video never centralises. Metadata does.**
80,000 cameras at 4 Mbps is 320 Gbps and 104 PB per month. That is not a budget problem, it is a physics problem. Sentinel leaves video at the edge, ships ~5 kbps of metadata per camera to the core, and pulls full-resolution clips only on demand. Every other decision follows from this. → [docs/01](docs/01-architecture.md)

**2. The spatio-temporal gate matters more than the model.**
Given a sighting at camera A, only cameras reachable by road in a plausible travel window are candidates — typically 2–5 out of 50. That gate removes 95–98% of comparisons, and it is what makes an 85%-mAP ReID model *operationally trustworthy* rather than a false-positive generator. It is a PostGIS query plus a cached OSRM matrix. → [docs/03 §4.3](docs/03-cv-pipeline.md)

**3. ANPR alone cannot track a vehicle across a real estate.**
Only 10–15% of general surveillance cameras can resolve a plate. Requiring a plate read at every hop yields a dotted line. Sentinel fuses plate (precision) with ReID and attributes (recall) under the gate. → [docs/03 §0](docs/03-cv-pipeline.md)

---

## Documents

| | |
|---|---|
| [01 — System Architecture](docs/01-architecture.md) | Federation, ingest, decode-once fan-out, latency budgets, storage tiers, camera health |
| [02 — Tech Stack](docs/02-tech-stack.md) | Component choices with rationale, **licence warnings**, hardware sizing |
| [03 — CV Pipeline](docs/03-cv-pipeline.md) | Detection, tracking, Indian ANPR, ReID, fusion scoring, compute budget, honest limitations |
| [04 — Legacy Integration](docs/04-legacy-integration.md) | Analog DVR bridging, outbound-only edge gateway, DPDP/BSA §63 compliance |
| [05 — Phase 1 Roadmap](docs/05-phase1-roadmap.md) | Day-by-day to 7 Sep, plus the concrete first-feed ingest sequence |
| [API contract](docs/api/openapi.yaml) | OpenAPI 3.1 — camera onboarding, targets, tracks, alerts, evidence |
| [Event contracts](docs/events/) | Kafka topic layout and JSON Schemas |

---

## What is implemented here

This is a scaffold with the hard parts written and tested, not a finished product. What exists:

| Path | What it is | Tested |
|---|---|---|
| `db/migrations/001_init.sql` | Full schema: PostGIS registry, Timescale sighting hypertable, pgvector embeddings, tracks, alerts, audit, evidence chain | — |
| `db/migrations/002_gating.sql` | `candidate_cameras()` spatio-temporal gate, `st_feasibility()`, plate canonicalisation, FOV polygon derivation | — |
| `services/cv/plate_rules.py` | Indian plate grammar, lexicon-constrained OCR correction, confusion-weighted fuzzy matching | ✅ 11 tests |
| `services/matcher/fusion.py` | Gating, score fusion, ReID calibration, Hungarian assignment | ✅ 14 tests |
| `services/ingest/adapters.py` | Vendor RTSP URL templates, credential-safe URL building, ffprobe capability probe, vendor autodetect | ✅ |
| `services/ingest/onvif_discovery.py` | WS-Discovery + GetProfiles/GetStreamUri, dependency-free | ✅ |
| `scripts/build_adjacency.py` | OSRM travel-time graph builder — populates the gate | ✅ |
| `scripts/simulate_feeds.sh` | N looped RTSP feeds for reproducible 50-camera testing | ✅ |
| `docker-compose.yml`, `mediamtx.yml` | Single-host stack | ✅ |

Not yet written: the FastAPI service bodies, the CV inference workers, and the React dashboard. Those are Days 1–4 work in [docs/05](docs/05-phase1-roadmap.md) and the contracts they must satisfy are already fixed in `docs/api/openapi.yaml`.

Run the tests:

```bash
python3 services/cv/test_plate_rules.py     # 11 passing
python3 services/matcher/test_fusion.py     # 14 passing (scipy optional)
```

---

## Quick start

```bash
cp .env.example .env && $EDITOR .env        # set PG_PASSWORD, MINIO_PASSWORD

docker compose up -d postgres redpanda redis minio mediamtx
docker compose exec -T postgres psql -U sentinel -d sentinel < db/migrations/001_init.sql
docker compose exec -T postgres psql -U sentinel -d sentinel < db/migrations/002_gating.sql

./scripts/prepare_osrm.sh                   # ~25 min, start it early
docker compose up -d osrm

./scripts/simulate_feeds.sh 35 ./data/samples    # simulated cameras
# ...onboard real cameras via POST /api/v1/cameras

python3 scripts/build_adjacency.py --max-dist 5000
```

Read the last line of `build_adjacency.py`'s output. Gate selectivity is the number that predicts your cross-camera precision.

---

## Two things worth knowing before you build

**Ultralytics YOLO is AGPL-3.0.** For a state-government deployment that is normally a procurement blocker. Prototype with it if it is fastest, ship YOLOX or RT-DETRv2 (both Apache-2.0), keep the detector behind an interface, and say so in the submission. See the licence table in [docs/02](docs/02-tech-stack.md).

**Do not bet the demo on 50 live cameras.** Run ~35 as looped republishes and 10–15 genuinely live. Venue networks fail. This is standard practice for testing this class of system, it is reproducible, and stating it openly reads as competence.

---

## Expected accuracy — plan for these, not for 99%

| Condition | ANPR exact-match | With lexicon + fuzzy |
|---|---|---|
| Dedicated ANPR lane, day | 92–97% | 96–99% |
| Dedicated ANPR lane, night + IR | 85–93% | 92–96% |
| General surveillance cam, day | 55–70% | 70–82% |
| General surveillance cam, night | 20–40% | 35–55% |
| Wide-angle junction (plate < 60 px) | < 10% | < 15% |

The full limitations list is [docs/03 §6](docs/03-cv-pipeline.md). Present it. A team that knows its own error envelope is far more credible than one claiming numbers an evaluator can disprove with a single question.
