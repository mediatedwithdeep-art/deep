# Demo Script

**Total time: 8 minutes.** Rehearse it three times on the actual machine.

This is what to click and what to say. The reasoning behind each claim is in
[ARCHITECTURE.md](ARCHITECTURE.md) and [BENCHMARKS.md](BENCHMARKS.md); this
page is the runbook.

---

## Before the room fills (T-30 minutes)

```bash
cd sentinel
make demo                      # builds, starts, migrates, seeds
```

Wait for the login line to print, then:

- [ ] Open `http://localhost:3000` and sign in. Leave it on the Command Centre.
- [ ] Confirm the top-right badge reads **LIVE** (green). If it reads
      RECONNECTING, the WebSocket is down — `make logs` and fix it *now*,
      not on stage.
- [ ] Let it run **at least 10 minutes** before you present. Cross-camera
      tracks need time to form; a system started 60 seconds ago has nothing
      to show.
- [ ] Check the target vehicle exists:
      Vehicle Search → plate `GJ01AB1234` → it should appear.
- [ ] **Record a screen capture of a full successful run.** If the venue
      network or the projector fails, you still have a demo. This has saved
      more teams than any technical decision.
- [ ] Open a second browser tab on **Camera Health** — you will switch to it.

If you have real cameras at the venue, put them in `config/cameras.yaml`
and run `make hybrid`. Real feeds appear alongside the simulated estate, so
a network failure degrades the demo rather than ending it.

---

## The run

### 1 · Open on the problem, not the product (30 s)

> "Gujarat has cameras in 26 departments that cannot see each other. A
> vehicle crossing the city today is tracked by a person phoning another
> person. Sentinel is one pane of glass over all of it."

Point at the Command Centre.

> "Fifty cameras, four protocols, nine vendors. Modern IP cameras, analog
> cameras on 2011 DVRs, and municipal HLS feeds — all normalised into one
> view. Nothing downstream knows the difference."

**Say the number:** cameras online / total, top-left.

---

### 2 · Show the estate honestly (45 s)

Click **Camera Health**.

> "Here is the part most systems hide. Of fifty cameras, only thirteen can
> physically resolve a number plate — the rest are wide-angle. Fourteen run
> firmware that is end-of-life or has a known CVE."

> "That is not a defect in our system. It is what a real government estate
> looks like, and a VMS that reports one blended accuracy figure across
> those two classes is telling its operators something untrue."

Scroll to a camera with a low trust score.

> "Trust score decays fast on failure and recovers slowly, so a camera that
> flaps is never treated as reliable in between."

*This is the single most credible thing you will say. Do not skip it.*

---

### 3 · Live detection (45 s)

Click **Live Cameras**.

> "Every tile is a real pipeline: decode, detect, track, then ANPR and
> re-identification only on the crops that pass a quality gate. The gate
> refuses roughly 80% of candidate crops, which is what makes fifty cameras
> fit on one GPU instead of four."

Point at a tile's detection count ticking up.

> "The number is live detections in the last minute. Video is negotiated
> browser-to-media-server over WebRTC — it never passes through the API, so
> one slow viewer cannot affect anyone else."

Filter to **ANPR-capable only**.

> "Thirteen cameras. That constraint drives the whole design."

---

### 4 · The alert fires (60 s)

Click **Alerts**.

> "A watchlist entry for FIR 0142/2026, plate GJ01AB1234."

Click a **WATCHLIST HIT** alert to open the detail panel.

> "The alert carries its evidence: the characters actually read, the OCR
> confidence, whether the lexicon corrected it, and the case reference. An
> operator who cannot interrogate an alert learns to mute it."

If the alert says **Probable**, read the caveat aloud:

> "This one is a probable match — the plate was read as one string and
> matched to the target after allowing for OCR confusion. The system says
> so, and says to verify. It does not present a guess as a fact."

Point at the **false-positive rate** tile.

> "We show that on purpose. It is the number that decides whether operators
> keep trusting the system."

---

### 5 · Cross-camera tracking — the centrepiece (2 min)

From the alert, click **Track this vehicle**.

The Vehicle Tracking page loads. **Type a purpose** into the box:

> `FIR 0142/2026 vehicle movement enquiry`

> "It will not show me movement history without a stated purpose, and that
> purpose is written to the audit log against my account. The DPDP Act 2023
> requires purpose limitation for personal data, and video of identifiable
> people is personal data."

Click **Show history**. Point at the timeline:

> "Camera A, then B, then C, with timestamps. Each hop is labelled
> **confirmed** or **probable**. Confirmed means a plate read tied it.
> Probable means appearance matched it, and appearance cannot distinguish
> two similar vehicles with certainty."

Click **Why this match?** on a probable hop.

> "Plate, appearance, colour, type, and road-network reachability, each
> scored separately. It travelled in 218 seconds; the road network expects
> about 240. That last one is the important one."

Point at the map.

> "The blue line is the observed path. Every segment joins two real
> sightings — we never draw an inferred segment as if it were observed."

Scroll to **Where to look next**.

> "Given the last sighting, these are the cameras the vehicle can physically
> reach, with arrival windows from the road graph. The map is showing you
> where to look *ahead* of the vehicle."

---

### 6 · The claim that makes it scale (60 s)

Stay on that table.

> "That reachability calculation is not a nicety, it is the thing that makes
> cross-camera tracking work at all."

> "Appearance matching against all fifty cameras produces false positives at
> a rate that destroys operator trust in about ten minutes. We measured the
> gate: within a three-minute arrival window it leaves **3.3 candidate
> cameras out of 49** — a 93% reduction. The same model that is unusable
> against fifty is trustworthy against three."

> "And it is a PostGIS query plus a cached routing matrix. No GPU."

---

### 7 · Search (45 s)

Click **Vehicle Search**. Type a plate with a deliberate error, e.g.
`GJO1AB1Z34` (letter O for zero, Z for two).

> "I typed it wrong on purpose — O for zero, Z for two. Those are the
> confusions OCR makes systematically. Exact matching would find nothing."

Press Search. The vehicle appears.

> "Fuzzy by design, and the interface says so and says to verify. An officer
> who thinks they have an exact read may go to the wrong vehicle."

---

### 8 · Scale, in one breath (45 s)

Click **System Analytics**.

> "80,000 cameras at 4 Mbps is 320 gigabits per second and 104 petabytes a
> month. That is not a budget problem, it is a physics problem, and no
> centralised design survives it."

> "So video never centralises. It stays at the edge; about 5 kilobits per
> camera of metadata comes to the core; full-resolution clips are pulled
> only on demand. The MVP is not a small version of the state system — it is
> one district-scale cell of it, and the seam between edge and core is
> already in the code."

Point at the throughput tiles.

> "Measured: the whole fifty-camera estate costs 14 milliseconds per tick,
> about 9% of one core."

---

### 9 · Close on the limits (30 s)

> "Three things this does not do. Night ANPR on a wide-angle camera is
> around 38% and we publish that. Two white hatchbacks at 720p can be
> confused, which is why appearance matches surface for confirmation instead
> of auto-confirming. And a fake plate defeats ANPR entirely — only
> appearance helps there."

> "Everything I have shown runs from one command on a laptop, with no GPU
> and no model weights, and the same code path takes real weights and real
> cameras by changing configuration."

---

## If something breaks

| Symptom | Do this, out loud |
|---|---|
| Feed shows RECONNECTING | "The socket dropped — it reconnects with backoff." Refresh. The data is in PostgreSQL; nothing is lost. |
| Map tiles are blank | "No internet here; the basemap is a CDN. The overlays are ours and still work." **This is the air-gapped behaviour — say so.** |
| A camera tile shows no video | "Video needs the media server; analytics are running regardless." Point at the live detection count. |
| No cross-camera tracks yet | It has not run long enough. Use Vehicle Search on any plate instead, and say tracks need a few minutes to form. |
| Everything is broken | Play the recording. Say: "The system is running; the venue network is not." |

**Deliberately break a camera if you have 30 seconds spare.**
`docker compose stop ingestion` → Camera Health goes red → restart it.
Recovering gracefully from a visible failure is more persuasive than a demo
where nothing goes wrong.

---

## Numbers to have memorised

| | |
|---|---|
| Cameras / protocols / vendors | 50 / 4 / 9 |
| ANPR-capable | 13 of 50 (26%) |
| Gate reduction (3-min window) | 3.3 candidates of 49 — **93%** |
| ANPR, dedicated lane, day | **92.5%** end-to-end |
| ANPR, wide-angle, night | **37.9%** — say this one |
| Estate cost | 14 ms/tick, **9%** of one core |
| Implied capacity | ~585 cameras per core |
| State-scale arithmetic | 320 Gbps, 104 PB/month |
| Tests | 186 passing |

Every one of these comes from `make benchmark`. If a judge asks, run it.

---

## Questions you will be asked

**"Is this real or a simulation?"**
> "Both, and the same code. The pipeline, matcher, database and interface
> are production code. The demo estate is a traffic simulator so it runs
> without 50 cameras — and because it gives us ground truth, we can measure
> accuracy instead of asserting it. Point it at real RTSP URLs in
> `config/cameras.yaml` and nothing else changes."

**"What accuracy do you get?"**
> "It depends entirely on the camera, which is why we never quote one
> number. Dedicated ANPR lane in daylight: 92%. Wide-angle camera at night:
> 38%. Both are in the benchmark."

**"How is this different from an existing VMS?"**
> "A VMS shows you cameras. This tells you where one vehicle went, across
> departments that do not share a system, and shows its reasoning for every
> hop so an officer can judge whether to act."

**"What about privacy?"**
> "Purpose required before movement history, every access audited for seven
> years, credentials never stored where the database can leak them, and
> retention limits per data class. It is in the schema, not the roadmap."

**"Can it run on our existing cameras?"**
> "That is the design premise. Analog on a DVR, ONVIF, HLS, proprietary VMS
> — one file, `config/cameras.yaml`, no code change. And nothing on a camera
> VLAN is reachable from anywhere except its edge gateway."
