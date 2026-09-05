# Network and Bandwidth Plan

**Centralised vs federated vs hybrid, with the arithmetic that decides it.**

All figures derive from the assumptions and measurements in
[SCALE_BENCHMARK.md](SCALE_BENCHMARK.md) §4 and carry the same provenance
labels.

---

## 0 · The number that settles the argument

| At 80,000 cameras | Backbone required | Provenance |
|---|---:|---|
| Centralise **main** streams (1080p, 4 Mbps) | **320 Gbps** | PROJECTED |
| Centralise **sub** streams only (640×360, 0.4 Mbps) | **32 Gbps** | PROJECTED |
| Centralise **metadata** only (10,000 sightings/s × 1.2 kB) | **96 Mbps** | PROJECTED |

**Metadata is 3,333× smaller than main video.** That ratio, not a
preference for distributed systems, is why this architecture processes at
the edge.

A 320 Gbps state-wide backbone dedicated to CCTV is not a budget line, it
is a civil-works programme. 96 Mbps is a leased line.

---

## 1 · The three models

### Model A — Centralised

Every camera streams to a state data centre. All decode, all inference,
all storage happens there.

```
  camera ──4 Mbps──┐
  camera ──4 Mbps──┼──► 320 Gbps ──► [ state DC: decode, AI, storage ]
  camera ──4 Mbps──┘
```

| | |
|---|---|
| **Backbone** | 320 Gbps (PROJECTED) |
| **Sites needing engineering** | 1 |
| **Works when the WAN drops** | Nothing. Every camera is blind. |
| **Verdict** | **Rejected.** Not on cost — on the fact that a single fibre cut blinds a district, and on 24 PB of hot video landing in one building. |

Defensible only below ~1,000 cameras in one city, where the backbone is
4 Gbps and a single site is genuinely simpler.

### Model B — Fully federated

Each district runs its own complete stack. The centre holds only a
registry and a search index.

```
  [ district: decode, AI, storage, alerts ] ──metadata──┐
  [ district: decode, AI, storage, alerts ] ──metadata──┼──► 96 Mbps ──► [ centre ]
  [ district: decode, AI, storage, alerts ] ──metadata──┘
```

| | |
|---|---|
| **Backbone** | 96 Mbps for 80,000 cameras (PROJECTED) |
| **Sites needing engineering** | 26+ |
| **Works when the WAN drops** | Each district keeps working locally; only cross-district correlation stops. |
| **Verdict** | Correct on bandwidth. Expensive in operations: 26 sites to patch, secure, staff and audit. |

### Model C — Hybrid, edge → regional → central ◀ **chosen**

Three tiers, each doing what only it can do.

```
EDGE (at or near the camera)
  decode · detect · track · ANPR · ReID · quality gate
  local ring buffer, 72 h
       │ metadata only, ~1.2 kB per sighting
       ▼
REGIONAL (one per zone / group of districts)
  aggregate · cross-camera matching within the region
  event processing · alert engine · regional storage
  clip extraction on demand
       │ alerts, cross-region candidates, registry sync
       ▼
CENTRAL (state)
  camera registry · GIS · state-wide search
  cross-region intelligence · command centre · governance · audit
```

**Why this and not B:** the number of sites that need real engineering
drops from 26 to the regional tier, while the edge stays dumb enough to be
an appliance. Cross-camera correlation happens where the cameras actually
are — a vehicle crossing Ahmedabad is matched in Ahmedabad, not in a
building 300 km away.

| | |
|---|---|
| **Edge → regional** | 0.4 Mbps/camera *only while a clip is being pulled*; otherwise metadata |
| **Regional → central** | 96 Mbps state-wide (PROJECTED) |
| **Works when the WAN drops** | Edge buffers 72 h; regional keeps alerting; centre loses live view only |

---

## 2 · The techniques that produce those numbers

### 2.1 Edge processing

The single largest reduction. A 4 Mbps video stream becomes ~1.2 kB per
sighting, at roughly one sighting per camera per 8 s (A8, CALCULATED from
the measured estate) — **150 bytes/s per camera against 500,000 bytes/s.**

This is only possible because the pipeline is already per-camera and
shares no mutable state, which is what makes running it at the edge a
deployment decision rather than a rewrite.

### 2.2 Sub-streams for AI, main streams for evidence

AI runs on 640×360 at 6 fps (A2, A3). The main stream is decoded only when
a human asks for a clip.

A plate needs ~90 px of width to be readable, and that is a function of
lens and distance, not of whether the whole frame is 1080p. Running
detection on the main stream would cost 10× the bandwidth and 4–6× the
decode for no accuracy the quality gate would accept.

### 2.3 Metadata centralisation, not video centralisation

What crosses a district boundary: sighting rows, embeddings, alerts,
health beacons. What does not: pixels.

### 2.4 Event-driven clips

A clip moves only when an event justifies it — an alert, an operator
request, an evidence export. At 80,000 cameras, if 0.1% of cameras have a
clip pulled in any minute, that is 80 concurrent 4 Mbps pulls = **320
Mbps** (CALCULATED), which is why the regional tier holds the buffer and
the centre requests through it.

### 2.5 On-demand evidence, not bulk archive

The tamper-evident export ledger already models this: an export is an act
with a reason, a requester and a hash chain. Bulk-shipping video to the
centre so that it is "available" would move 24 PB to make a few hundred
exports convenient.

### 2.6 Regional aggregation

Cross-camera matching is a within-region operation in almost every real
case, because vehicles do not teleport. The spatio-temporal gate makes
this explicit: candidates are drawn from the adjacency graph, and the
adjacency graph is local by construction.

### 2.7 Low-bandwidth sites

Many real government sites have a 2–10 Mbps ADSL or 4G uplink and cannot
carry even a sub-stream reliably.

| Uplink | Strategy |
|---|---|
| ≥ 2 Mbps | Sub-stream to the regional tier; AI runs regionally |
| 512 kbps – 2 Mbps | Edge box on site; metadata only; clips on request |
| < 512 kbps or intermittent | Edge box, store-and-forward; metadata batched, clips only when someone asks and waits |

The store-and-forward case is why sighting timestamps must be
**PTS-derived capture time**, not arrival time. A batch that arrives four
hours late must still place its vehicles at the moment they were seen, or
the travel-time gate scores a journey against the wrong clock. That
requirement is the reason for the PTS work in
[SENTINEL_LIVE_TEST_REPORT.md](SENTINEL_LIVE_TEST_REPORT.md) §3, not a
theoretical nicety.

---

## 3 · Per-tier budget at 80,000 cameras

| Link | Load | Provenance |
|---|---:|---|
| Camera → edge (LAN) | 4.4 Mbps/camera (main + sub) | CALCULATED |
| Edge → regional, steady state | ~150 B/s/camera metadata | CALCULATED |
| Edge → regional, clip pull | 4 Mbps per concurrent pull | CALCULATED |
| Regional → central, steady state | 96 Mbps state-wide | PROJECTED |
| Regional → central, incident peak | +320 Mbps (2.4 §) | CALCULATED |
| Operator → regional, live view | 0.4–4 Mbps per open pane (WHEP) | CALCULATED |

**Operator live view is the one that surprises people.** A command centre
with 40 panes open at main quality is 160 Mbps *per wall*, and it is the
only reason a central site needs real downstream capacity at all. Panes
default to the sub-stream for this reason.

---

## 4 · What is not established here

- **No WAN was measured.** Every figure is arithmetic over the assumptions
  in SCALE_BENCHMARK.md §4. Nothing here has run over a real government
  network, and real GSWAN behaviour — jitter, contention, MTU, NAT — is
  unmeasured.
- **Bitrates are estimates.** A1 and A2 are typical values for the camera
  class, not measurements of the Gujarat estate. A single VBR camera
  watching a busy junction at night can exceed A1 substantially.
- **The 0.1% clip-pull rate in §2.4 is invented.** It is a plausible
  planning figure, not an observation, and the incident-peak row moves
  linearly with it.
- **Store-and-forward is designed, not built.** The edge buffer, the batch
  protocol and the late-arrival path are described here and in
  ARCHITECTURE.md; no code implements them.
