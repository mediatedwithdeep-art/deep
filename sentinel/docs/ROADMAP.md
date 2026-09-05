# 05 — Phase 1: the 50-camera prototype in 8 days

**Window:** Sunday 30 August → Monday 7 September 2026.

Eight days is enough for a genuinely working system *if* you refuse to build
the things that do not demo. The plan below is ordered so that **you have a
demonstrable system from Day 3 onward** and every day after that improves a
system that already works. Never let the first end-to-end run happen on Day 7.

## Team split (assumes 3–4 people; compress by dropping the web polish)

| Role | Owns |
|---|---|
| **A — Infra/Backend** | Compose stack, DB, API, Kafka, MediaMTX, adjacency |
| **B — CV** | Detector, tracker, ANPR, ReID, quality gate, TensorRT export |
| **C — Frontend/GIS** | React wall, MapLibre dashboard, alert feed, WebRTC |
| **D — Data/Demo** *(or shared)* | Labelling, camera onboarding, demo script, rehearsal |

---

## Day 0 — Sun 31 Aug *(today: setup and de-risking)*

**Goal: prove every risky external dependency works before you build on it.**

- [ ] `docker compose up -d postgres redpanda redis minio mediamtx` — stack healthy
- [ ] Apply `db/migrations/001_init.sql` and `002_gating.sql`; confirm PostGIS, TimescaleDB and pgvector all load
- [ ] `scripts/prepare_osrm.sh` — start this early, it takes 25 minutes and it will be needed on Day 4
- [ ] Point **one** real camera at the stack. Get one RTSP URL playing in a browser over WebRTC. **This single task is the whole project in miniature — if it is hard, everything after it is harder.**
- [ ] Collect 20–40 minutes of footage from your actual cameras. B needs it for fine-tuning; D needs it for the simulated feeds.
- [ ] Confirm GPU: `nvidia-smi`, TensorRT available, one YOLO inference runs.

**Exit criterion:** one live camera visible in a browser at < 1 s latency.

---

## Day 1 — Mon 1 Sep — Ingest at scale

**A:** camera registry API (`POST /cameras`, `/cameras/bulk`, `/cameras/{id}/probe`) against the schema in `docs/api/openapi.yaml`. Probe job writes back codec/resolution/fps/RTT and promotes `PENDING → ACTIVE`.
**A:** MediaMTX path registration driven from the registry, `sourceOnDemand: yes`.
**B:** GStreamer decode pipeline, one process per camera, NVDEC. Prove **20 concurrent sub-streams decode on one GPU** — this is the number that determines whether 50 is feasible.
**C:** React shell + camera grid, WebRTC (WHEP) tiles, camera list from the API.
**D:** `scripts/simulate_feeds.sh 35` running; onboard all 50 cameras (15 real + 35 simulated) via bulk import.

**Exit criterion:** 50 cameras registered, ACTIVE, and viewable as a wall.

> **Highest-risk day.** If decode does not scale, cut to 10 fps sampling and 704×576 immediately rather than debugging elegance. Falling back to `opencv-python` + CPU decode for 50 streams is *not* viable — do not spend a day discovering that.

---

## Day 2 — Tue 2 Sep — Detection and tracking

**B:** YOLO detection on all 50 feeds, batched through Triton. ByteTrack per camera. Publish tracklets to `sentinel.tracklets.v1`. Fine-tune on your own footage with the `auto_rickshaw` class added — COCO does not have it and it is a large share of Indian traffic.
**A:** Kafka topics created; consumer writing sightings into the `sighting` hypertable. Verify write throughput at ~500 rows/s.
**C:** map view live — cameras as points with FOV wedges, live detection counts, camera trust indicators.
**D:** begin labelling plate crops (target 3,000–5,000). Start now; it is the long pole and it gates Day 3.

**Exit criterion:** live detections from 50 cameras landing in Postgres and rendering on the map.

---

## Day 3 — Wed 3 Sep — ANPR + first end-to-end

**B:** plate detector on vehicle crops + PARSeq/PP-OCRv4 recogniser. Wire in `services/cv/plate_rules.py` for lexicon-constrained correction. **Implement the quality gate first, not last** — without it ANPR alone needs four GPUs (docs/03 §5).
**A:** `POST /targets` with `plate_query`; hotlist matching against the fuzzy matcher; `sentinel.alerts.v1` producing; WebSocket `/ws/stream` pushing to the browser.
**C:** alert feed panel with the evidence breakdown, plate crop thumbnail and a jump-to-camera button.
**D:** measure ANPR accuracy on a held-out set. **Write the number down.** You will quote it in the submission and you need it to be real.

**Exit criterion — the big one:** drive a known vehicle past a camera, get an alert on screen in under 2 seconds. **You now have a demoable system with four days to spare.**

---

## Day 4 — Thu 4 Sep — Cross-camera tracking

**A:** `scripts/build_adjacency.py` — populate `camera_adjacency` from OSRM. Verify gate selectivity: it should report ~2–5 candidates per sighting instead of 49.
**B:** OSNet ReID embeddings per tracklet, written to `reid_embedding` with the HNSW index.
**A+B:** matcher service consuming tracklets, applying `candidate_cameras()`, scoring with `services/matcher/fusion.py`, Hungarian assignment, writing `global_track` and `track_link`.
**C:** trajectory rendering — LineString path, sighting markers, **inferred corridors in a distinct style**. Never draw an inferred segment as though it were observed.
**D:** annotate ~50 ground-truth cross-camera transitions; tune the fusion weights against them.

**Exit criterion:** one vehicle tracked across ≥ 3 cameras with the path drawn on the map.

---

## Day 5 — Fri 5 Sep — The differentiators

Pick from these in order; each is independently demoable, so stop when time runs out rather than leaving two half-finished.

1. **Predictive next-camera highlight** (`GET /targets/{id}/candidates`) — the map lights up *ahead* of the vehicle. Highest impact per hour of work, and it falls straight out of the adjacency graph you already built.
2. **Analog DVR bridge, live on stage.** One real analog camera through an encoder or DVR RTSP, on an isolated VLAN, reached only through the edge gateway. Show the topology while it plays. Section 04 is the story; this is the proof.
3. **Estate health dashboard** — trust score per camera, dead/frozen/misaimed counts. This is the pain the challenge is actually about and almost nobody demos it.
4. **BSA 2023 §63 evidence export** — clip + hash + auto-generated certificate. Turns a demo into something a prosecutor could use.
5. **Visual search** — click any vehicle, find it everywhere else (`POST /sightings/similar`).

**Exit criterion:** two of the five working.

---

## Day 6 — Sat 6 Sep — Harden and measure

- [ ] **Soak test:** run all 50 feeds for 4 hours straight. Fix whatever leaks, stalls or drifts. Systems that work for 10 minutes routinely die at 90.
- [ ] **Kill-test:** yank a camera's network mid-demo. The wall must show it offline and the other 49 must keep working. Rehearse this — then do it *deliberately* on stage, because recovering gracefully from a failure is more persuasive than a demo where nothing goes wrong.
- [ ] **Measure and write down:** end-to-end alert latency (p50/p95), ANPR accuracy by day/night, cross-camera precision and recall on your annotated set, GPU utilisation, decode throughput.
- [ ] Freeze the code. No new features after tonight.

---

## Day 7 — Sun 7 Sep — Rehearse

- [ ] Full run-through **three times**, timed, on the actual demo hardware and network.
- [ ] Record a **video of a successful run.** If the venue network fails, you still have a demo. This has saved more hackathon teams than any technical decision.
- [ ] Prepare the honest-limitations slide (docs/03 §6). Stating your error envelope reads as competence; claiming 99% accuracy invites an evaluator to disprove it in one question.
- [ ] Prepare the scale slide: the 320 Gbps arithmetic from docs/01 §0 and why federation is the only answer. This is the argument that separates a hackathon demo from a state-deployable design.
- [ ] Sleep.

---

## Cut list — build these only if you are ahead

Behaviour analytics, crowd counting, face recognition, PTZ control, mobile app, multi-tenant RBAC UI, Kubernetes, model retraining loops, video archival, HLS fallback. **All defensible to omit.** Describe them as Phase 2 with the architecture that supports them, and spend the hours on making the core path bulletproof instead.

---

## Ingesting the first feed — the concrete sequence

The full contract is `docs/api/openapi.yaml`. This is the minimum path from nothing to a tracked vehicle.

### 1. Register the camera

```bash
curl -X POST http://localhost:8000/api/v1/cameras \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{
    "external_ref": "AHM-SG-014",
    "site_code": "AHM_SATELLITE_PS",
    "department_code": "GP_AHM",
    "name": "Satellite Rd / Jodhpur Cross — NE approach",
    "source_type": "ONVIF",
    "role": "SURVEILLANCE",
    "host": "10.42.7.14",
    "port": 80,
    "credentials": {"username": "viewer", "password": "..."},
    "location": {"lat": 23.02705, "lon": 72.51192, "altitude_m": 8.5},
    "optics": {"heading_deg": 47.0, "fov_deg": 82.0, "range_m": 65.0},
    "vendor": "Hikvision",
    "signal_class": "IP",
    "tags": ["junction", "arterial"]
  }'
```

Returns `201` with the camera in `PENDING` and a probe enqueued.

**`optics` is not optional in practice.** Without `heading_deg` the camera is a dot on a map with no field of view, no FOV polygon, and — because the adjacency graph is directional — a materially worse spatio-temporal gate. Capture the bearing during survey; it costs a compass reading per camera and it is very expensive to retrofit across 50 sites, let alone 2,000.

### 2. Confirm the probe

```bash
curl -X POST http://localhost:8000/api/v1/cameras/$CAM_ID/probe
```

```json
{
  "camera_id": "…", "reachable": true, "status": "ACTIVE",
  "codec": "h264", "width": 704, "height": 576, "fps": 12.0,
  "gop_size": 25, "rtt_ms": 48.2,
  "resolved_sub_stream_url": "rtsp://…/Streaming/Channels/102",
  "anpr_capable": false,
  "warnings": ["resolution too low for ANPR; camera contributes via ReID + attributes only"]
}
```

`anpr_capable: false` is the expected answer for most of the estate and it is the system telling you the truth. Do not "fix" it by pointing ANPR at a stream that cannot resolve a plate.

### 3. View it

```bash
curl http://localhost:8000/api/v1/cameras/$CAM_ID/stream?quality=sub
# -> { "whep_url": "http://localhost:8889/cam-<id>/whep", ... }
```

The browser POSTs its SDP offer to `whep_url`; MediaMTX pulls the camera on demand.

### 4. Rebuild the gate after onboarding

```bash
python3 scripts/build_adjacency.py --max-dist 5000
```

```
50 cameras
1,842 candidate pairs within 5000 m (75.2% of all ordered pairs)
  OSRM batch 0-49: 214 edges so far
214 adjacency edges (avg 4.3 downstream candidates per camera)
Gate selectivity: 4.3 candidates per sighting instead of 49 — a 91.2% reduction
```

**Do not skip this step and do not skip reading its output.** That last line is the number that determines cross-camera precision.

### 5. Designate a target

```bash
curl -X POST http://localhost:8000/api/v1/targets \
  -d '{"label":"Suspect vehicle — FIR 0142/2026",
       "plate_query":"GJ01AB1234",
       "priority":"HIGH",
       "case_ref":"FIR/0142/2026",
       "reason":"Vehicle linked to reported offence"}'
```

Or, when the plate is unknown, click the vehicle on the wall and pass `seed_sighting_id` — the API pulls that tracklet's embedding and tracks by appearance.

### 6. Watch it move

```bash
curl "http://localhost:8000/api/v1/targets/$TARGET_ID/track" | jq
```

Returns GeoJSON that MapLibre renders directly — observed path, sighting points, and inferred corridors as separate, distinctly styled features.

```javascript
const ws = new WebSocket("ws://localhost:8000/api/v1/ws/stream");
ws.onopen = () => ws.send(JSON.stringify({
  action: "subscribe", channels: ["alerts", `target:${targetId}`]
}));
```

---

## What to say about the 80,000-camera path

The MVP is not a small version of the state system; it is **one district-scale cell of it**. Say this explicitly, because it is the difference between a prototype and an architecture:

| MVP (50) | State (80,000) | What changes |
|---|---|---|
| One GPU box does decode + AI | ~2,000 edge nodes | Nothing in the code — the edge/core seam already exists |
| Redpanda, 12 partitions | Redpanda/Kafka, 512 partitions, per-district clusters | Configuration |
| Postgres for sightings | ClickHouse for sightings, Postgres for registry | Consumer target |
| pgvector | Milvus/Qdrant sharded by district | Vector store adapter |
| Adjacency across 50 cameras | Adjacency within district + border cameras across | Same query, partitioned |
| One Compose file | K3s per edge site, K8s at core | Deployment only |

The load-bearing claim is the one from docs/01 §0: **video never centralises, metadata does.** Everything in the table above is a scaling exercise. Nothing in it is a redesign — and that is the point worth making to the evaluators.
