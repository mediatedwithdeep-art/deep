# 01 — System Architecture

**Project:** Sentinel — Unified VMS & AI Analytics Ecosystem
**Target scale:** 80,000+ heterogeneous cameras, 26 departments, State of Gujarat
**MVP scope:** 50 heterogeneous live feeds, cross-camera vehicle tracking, real-time alerts, GIS history

---

## 0. The one architectural decision that determines everything

**Do not centralise video. Centralise metadata.**

Run the numbers before designing anything else:

| Scale | Streams | Bitrate (main) | Aggregate | Storage @ 30-day retention |
|---|---|---|---|---|
| MVP | 50 | 4 Mbps | 200 Mbps | 65 TB |
| District | 3,000 | 4 Mbps | 12 Gbps | 3.9 PB |
| State | 80,000 | 4 Mbps | **320 Gbps** | **104 PB** |

320 Gbps of sustained ingress into a central cloud is not a budget problem, it is a physics-and-GSWAN problem. It will never be sanctioned and it would not work if it were.

So Sentinel is a **federated, metadata-centric** system:

- **Video stays where it is born.** Existing NVRs, DVRs and departmental VMS keep recording. Sentinel does not replace them.
- **Only metadata flows up.** Detections, ANPR reads, ReID embeddings, track fragments, health beacons. A vehicle sighting is ~400 bytes; a 30-second evidence clip is 15 MB. 80,000 cameras of *metadata* is ~40 Mbps state-wide — three orders of magnitude cheaper than video.
- **Video is pulled on demand.** When an operator clicks a sighting, and only then, the edge node uploads that clip. Evidence is exported deliberately, not hoarded speculatively.

This inverts the usual VMS pitch and it is the reason the design survives contact with 80,000 cameras. Every other choice below follows from it.

```
┌──────────────────────── SITE / STATION (×N) ────────────────────────┐
│  Analog cams ──BNC──▶ DVR ──┐                                       │
│  IP cams ───────RTSP────────┼──▶ EDGE NODE (Jetson Orin / NUC)      │
│  Legacy VMS ────SDK/ONVIF───┘      • ONVIF discovery + adapters     │
│                                    • NVDEC decode-once              │
│                                    • YOLO detect → ByteTrack        │
│                                    • ANPR + ReID embedding          │
│                                    • 7-day local ring buffer        │
│                                    • outbound-only mTLS / WireGuard │
└─────────────────────────────┬───────────────────────────────────────┘
                              │  metadata only (~5 kbps/camera)
                              ▼
┌──────────────────────── STATE CORE (GSWAN / MeghRaj GCC) ───────────┐
│  Ingest GW ─▶ Kafka ─▶ Cross-camera Matcher ─▶ Alerts ─▶ WebSocket  │
│                 │                                                    │
│                 ├─▶ PostGIS + TimescaleDB   (registry, sightings)   │
│                 ├─▶ pgvector / Milvus       (ReID embeddings)       │
│                 ├─▶ MinIO / S3              (evidence clips only)   │
│                 └─▶ MediaMTX                (on-demand WebRTC view) │
└──────────────────────────────────────────────────────────────────────┘
```

For the 50-camera MVP the "edge node" and the "core" collapse onto one or two GPU boxes. The *code* stays split along that seam so the same binaries scale out later without a rewrite.

---

## 1. Ingest: making 26 departments' cameras look like one camera

### 1.1 The heterogeneity you will actually meet

| Class | Share (est.) | How you get pixels | Pain |
|---|---|---|---|
| ONVIF Profile-S IP cams | ~45% | WS-Discovery → GetStreamUri → RTSP | Mostly clean |
| Non-ONVIF IP cams | ~25% | Vendor URL template | Need a template table |
| Analog on DVR | ~20% | DVR's RTSP channel, or BNC→encoder | Auth quirks, EoL firmware |
| Proprietary VMS (Milestone, Genetec, CP Plus) | ~8% | Vendor SDK / RTSP gateway | Licence + SDK wrapping |
| Cloud/HLS (municipal, toll, smart-city) | ~2% | HLS pull | 6–30 s latency, unusable for pursuit |

**Design conclusion:** you need a pluggable **Source Adapter** interface, not a giant `if vendor ==` block. Every adapter answers the same three questions: *how do I discover you*, *how do I get an RTSP/HLS URL*, *how do I check you are alive*. Everything downstream sees only a URL and a `camera_id`.

### 1.2 Discovery

1. **ONVIF WS-Discovery** — multicast `Probe` to `239.255.255.250:3702`, then `GetProfiles` → `GetStreamUri` per profile. This auto-onboards roughly half the estate with zero manual data entry, and it also yields the profile list so you can pick the sub-stream automatically.
2. **Vendor URL templates** for the non-ONVIF half:
   - Hikvision `rtsp://{u}:{p}@{ip}:554/Streaming/Channels/{ch}02` (`02` = sub-stream)
   - Dahua/CP Plus `rtsp://{u}:{p}@{ip}:554/cam/realmonitor?channel={ch}&subtype=1`
   - Axis `rtsp://{u}:{p}@{ip}/axis-media/media.amp?resolution=640x480`
   - Uniview `rtsp://{u}:{p}@{ip}:554/media/video2`
   - Generic ONVIF fallback `rtsp://{u}:{p}@{ip}:554/onvif1`
3. **Bulk CSV import** for the long tail, validated by a probe job that hits every URL once and records codec, resolution, fps and RTT before the camera is marked `ACTIVE`.

### 1.3 The single biggest scale lever: use the sub-stream

Essentially every IP camera and DVR channel published in the last decade exposes **two encodings of the same scene**: a main stream (1080p/4K, 4–8 Mbps) and a sub-stream (D1–720p, 0.3–1 Mbps).

- **AI consumes the sub-stream.** 704×576 or 1280×720 is enough for vehicle detection, colour, type and ReID.
- **The main stream is only recorded locally and only pulled for evidence**, where a human needs to read a face or a plate at full resolution.

This cuts decode and inference cost **6–8×** and network cost **~8×**. It is the difference between 3 GPUs and 25 GPUs for the same camera count.

The one exception: **dedicated ANPR lanes.** Plate OCR needs ≥ 90–110 px of plate width. On a wide-angle junction sub-stream a plate is 25 px and no model on earth will read it. So cameras are tagged by role — `SURVEILLANCE` (sub-stream, detection + ReID) vs `ANPR` (main stream, plate-optimised, narrow FoV, IR). Expect only 10–15% of the estate to be genuinely ANPR-capable; design so the other 85% still contribute through ReID and attributes.

### 1.4 Decode-once, fan-out-many

The classic failure mode is decoding the same RTSP stream three times — once for the operator wall, once for recording, once for AI. At 50 cameras nobody notices. At 3,000 it is a 3× hardware bill.

Decode **once** into GPU memory, then fan out:

```
RTSP ─▶ rtspsrc ─▶ rtph264depay ─▶ h264parse ─┬─▶ nvv4l2decoder ─▶ NVMM frames ─▶ [AI batcher]
                                              │
                                              ├─▶ (no re-encode) ──▶ local ring buffer .mp4
                                              │
                                              └─▶ (no re-encode) ──▶ MediaMTX ─▶ WebRTC/LL-HLS
```

Note the two passthrough branches: **recording and viewing never re-encode.** They remux the original compressed bitstream. Only the AI branch pays decode cost.

Implementation: **GStreamer** with `nvv4l2decoder` (Jetson) or `nvh264dec` (dGPU), or **NVIDIA DeepStream** which is GStreamer with the batching, tracking and inference elements already written. DeepStream's `nvstreammux` batching across 30+ sources into one inference call is worth a lot of engineering time you do not have before September 7.

Rough decode capacity, 1080p H.264: T4 ≈ 35–40 streams, A2000 ≈ 30, RTX 4000 Ada ≈ 45, Jetson Orin NX ≈ 12–16. On sub-streams (720p) roughly double. **50 sub-streams fits comfortably on one T4-class GPU for decode; budget a second GPU for inference headroom.**

### 1.5 Latency budget

Two paths with very different requirements, and conflating them is the most common design error.

**Path A — detection-to-alert (must be fast):**

| Stage | Budget |
|---|---|
| Camera encoder + GOP buffer | 100–200 ms |
| Network (RTSP over GSWAN) | 30–120 ms |
| Jitter buffer (`latency=100` on `rtspsrc`) | 100 ms |
| NVDEC decode | 15–30 ms |
| Batched inference (detect+track+ReID) | 30–60 ms |
| Kafka produce→consume | 5–15 ms |
| Matcher + alert rules | 20–50 ms |
| WebSocket push to browser | 20–50 ms |
| **Total** | **≈ 320–625 ms** |

**Path B — operator video view (must be watchable):**

| Transport | Glass-to-glass | Use |
|---|---|---|
| WebRTC (WHEP) | **200–500 ms** | ✅ live wall, pursuit |
| LL-HLS | 2–4 s | Fallback, poor networks |
| HLS | 6–30 s | ❌ never for live ops |
| RTSP direct to client | 200 ms | ❌ doesn't traverse browsers |

Use **WebRTC via MediaMTX (WHEP)** for the wall. Critical detail: **the alert arrives ~400 ms after the event, the video arrives ~400 ms after the event.** They are aligned. If you had used HLS the alert would beat the video by 10 seconds and operators would lose trust in the system immediately.

Practical tuning that actually matters:
- Set camera GOP/I-frame interval to **1× fps** (1 second), not the default 50–100. Long GOPs add up to 4 s of startup delay and make seek-to-evidence horrible.
- `rtspsrc latency=100 drop-on-latency=true protocols=tcp` — TCP for reliability over GSWAN, small jitter buffer, and *drop* rather than accumulate when late. A pursuit system must prefer a dropped frame to a growing delay.
- Never let a stalled camera back-pressure the pipeline. Each source is an independent process/thread with its own watchdog.

### 1.6 Standardisation contract

Everything downstream of ingest sees exactly one shape, regardless of whether the pixels came from a 2009 analog DVR or a 2025 4K ONVIF camera:

```
Frame {
  camera_id: uuid          # stable, from registry
  ts_utc:    int64 µs      # NTP-disciplined, PTP where available
  seq:       int64
  gpu_buf:   NvBufSurface  # NV12 in device memory, never CPU-copied
  meta:      { site_id, role, geo, homography_id, calib_ok }
}
```

**Time is the hard part.** Cross-camera tracking is meaningless if clocks disagree. A vehicle "seen at camera B before camera A" because a DVR drifted 40 seconds will corrupt every trajectory. So:
- NTP (`chrony`) on every edge node against a state-level stratum-2 source; PTP where the switch fabric supports it.
- The edge node stamps frames with **its own** disciplined clock, never the camera's RTC (analog DVR clocks drift minutes per week).
- Every sighting carries `clock_confidence`; the matcher widens its time gates when confidence is low rather than producing a false negative.

---

## 2. Data plane

### 2.1 What goes through Kafka

Kafka carries **metadata and pointers, never pixels**.

| Topic | Key | Rate @ 50 cams | Rate @ 80k cams | Retention |
|---|---|---|---|---|
| `sentinel.detections.v1` | `camera_id` | ~500/s | ~800k/s | 6 h |
| `sentinel.anpr.reads.v1` | `camera_id` | ~10/s | ~16k/s | 7 d |
| `sentinel.reid.embeddings.v1` | `camera_id` | ~50/s | ~80k/s | 1 h |
| `sentinel.tracklets.v1` | `camera_id` | ~20/s | ~32k/s | 24 h |
| `sentinel.alerts.v1` | `target_id` | bursty | bursty | 30 d |
| `sentinel.camera.health.v1` | `camera_id` | 0.05/s | 80/s | 24 h |

Partition by `camera_id` so a camera's events stay ordered, and so the matcher can shard by geography later. At state scale `detections` becomes the firehose — that tier belongs in ClickHouse, not Postgres, and most of it should be aggregated at the edge before it is ever published (publish *tracklets*, not per-frame boxes).

**Frame crops** (the 64×64 plate crop, the 256×256 vehicle crop) go to Redis with a short TTL, or to MinIO for anything retained. Kafka carries the key. At 50 cameras you could get away with base64 in the message; do not, because the code will not survive the demo.

### 2.2 Storage tiers

| Tier | Store | Content | Retention |
|---|---|---|---|
| Hot | Redis | latest frame per camera, dedup windows, live track state | seconds–minutes |
| Warm | PostGIS + TimescaleDB | camera registry, sightings, tracks, alerts, audit | 90 days |
| Vector | pgvector (MVP) → Milvus (scale) | ReID embeddings | 30 days |
| Evidence | MinIO / S3 | on-demand clips, plate crops, exports | 1–7 years (case-linked) |
| Edge | Local NVR / ring buffer | full-resolution video | 7–30 days, never uploaded wholesale |

One database with three extensions (PostGIS + TimescaleDB + pgvector) instead of four services is an enormous operational win with eight days on the clock, and it holds fine to district scale.

### 2.3 Why PostGIS specifically

The GIS layer is not decoration. It is a **query accelerator** for the CV problem:

- `ST_DWithin` on camera geometry gives the candidate camera set.
- A road-network graph (OSRM on the Gujarat OSM extract) turns "which cameras next" into a travel-time query, not a straight-line guess.
- Trajectories are `LINESTRING M` with the M-ordinate carrying the timestamp — one geometry per track, directly renderable and directly queryable.

This is what makes cross-camera ReID tractable, and section 3 explains why.

---

## 3. Failure, health and trust

At 80,000 cameras, a meaningful fraction is *always* broken. Field surveys of Indian city surveillance estates routinely find 20–35% of cameras dead, misaimed, spider-webbed, IR-blinded or recording a wall. A VMS that does not measure this is lying to its operators.

Every edge node emits a health beacon carrying: RTSP connect success, fps delivered vs expected, decode error rate, **scene-change score** (a frozen frame is the classic silent failure — the stream is alive, the picture is a still), mean brightness (IR failure at night), blur variance (defocus/dirt), and clock offset.

Surface it as a **camera trust score** on the map, and let the matcher down-weight low-trust cameras rather than blindly trusting them. For the hackathon this is also a demo moment: a live "estate health" panel showing which of the 26 departments' cameras are actually usable is exactly the operational pain the challenge is about.
