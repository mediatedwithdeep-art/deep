# Demo Script

**Total time: 9 minutes.** Rehearse it three times on the actual machine.

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

## The run — 16 steps, 9 minutes

Timings are the target. If you are running long, **cut steps 11 and 14**;
never cut 8 or 13.

---

### 1 · Login, and what a login means here (25 s)

Sign in as **`controller`** — an OPERATOR in Ahmedabad City Police.

> "Twenty-six departments own cameras in Gujarat and they cannot see each
> other's. That is the problem. So the first thing this system does is
> decide what this officer is allowed to see."

Say the number: **26 departments seeded.**

### 2 · Command Centre (30 s)

> "One screen. Live estate, live detections, live alerts."

Point at the **LIVE** badge. Point at the department name beside the
username.

**The moment worth having:** open a second browser as **`traffic`**
(Ahmedabad *Traffic* Police, a different department). The camera list is
different. Same system, same database, different estate.

> "That is not a UI filter. It is enforced in the query, on every surface
> that carries camera-derived data — cameras, vehicles, sightings, alerts,
> the watchlist, analytics, the audit log and the live WebSocket. 38 tests
> hold it, including that a department admin cannot switch off a
> neighbour's camera during an incident, and that an operator cannot
> acknowledge a neighbour's CRITICAL alert."

### 3 · The estate, honestly (40 s)

Map view. Field-of-view wedges, not dots.

> "Fifty cameras, four protocols, nine vendors. Eighteen percent are analog
> on legacy DVRs. Ten percent are municipal HLS feeds with 10-second
> latency. This is what a real government estate looks like, and a system
> that only works on clean modern IP cameras has not solved the problem."

Point at a red camera. **Do not hide it.**

> "Roughly a fifth of a real estate is dead, frozen or misaimed at any
> moment. We show that on the front page."

### 4 · Select the target vehicle (25 s)

Vehicle Search → plate **`GJ01AB1234`** → open it.

> "An officer has a plate. Everything from here is: where has this vehicle
> been, and can I trust the answer."

### 5 · ANPR, with its limits stated (40 s)

Show the plate read and its confidence.

> "Dedicated ANPR lane in daylight: **92.5%** end to end. Wide-angle camera
> at night: **37.9%**."

Say the second number deliberately.

> "We publish our worst number because an officer needs to know when not to
> trust it. And a read below 0.72 confidence never reaches a government
> record system at all — we do not query a citizen's registration on the
> strength of a misread character."

### 6 · Vehicle detection and the live path (35 s)

Open a camera tile.

> "Detection runs on the sub-stream at 6 fps, not the main stream at 25.
> That is a 10× bandwidth saving and it costs nothing, because a plate needs
> pixels on the plate, not pixels in the frame."

The one live-path claim worth making:

> "Timing comes from the stream's own presentation timestamps, not from
> when bytes arrive. On connect, a decoder hands you 0.8 seconds of video in
> 11 milliseconds — arrival-based timing is 70× wrong, and every speed and
> every travel-time check is computed from it."

### 7 · Tracking within a camera (30 s)

Show the track and its trail.

> "ByteTrack, with the low-confidence second pass — that is what holds a
> track through a vehicle passing behind a bus. Track age is in seconds
> from PTS, not frames, so a 6 fps camera and a 25 fps camera agree about
> how long a vehicle has been missing."

### 8 · Cross-camera match — the centrepiece (90 s)

**Do not rush this.** Open the vehicle's cross-camera view.

Walk one hop out loud:

> "Camera 14 to camera 22. Plate matched with one character corrected by
> the plate grammar. Appearance similarity 0.83. Travel time 47 seconds
> against a road distance of 610 metres — that is 47 km/h, which is
> possible. **CONFIRMED.**"

Then the one that was rejected:

> "This one was rejected. Same plate read, but the travel time implies
> 180 km/h on an urban road. Physically impossible, so it is not a match —
> whatever the appearance score said."

> "And appearance alone can never confirm. Ceiling of 0.79 without a plate.
> That is a safety property, not a tuning choice: a system that identifies
> a car by colour and shape will eventually identify the wrong car, and an
> officer will act on it."

### 9 · GIS route (45 s)

Show the route drawn on the map.

> "This is the road route, not straight lines between cameras. The
> adjacency graph carries road distance and travel time between every pair
> of cameras within 15 minutes — 994 edges for 50 cameras, about 20
> reachable neighbours each."

**If a judge asks what the gate is worth in compute**, not just in
candidates: one batch of 50 sightings against 400 live vehicles is 20,000
comparisons without the gate and **473** with it — 2,240 ms against 53 ms,
at a measured 112 µs per scored pair. The matcher counts both in
production, so the claim is checkable on a running system rather than only
in a benchmark.

### 10 · Movement timeline (35 s)

Show the timeline.

> "Every hop, every confidence, every reason. An officer can see why the
> system believes this journey happened, and disagree with any hop."

**Purpose is required to open this.** Point at the reason prompt.

> "Movement history needs a stated purpose before it opens. DPDP Act 2023
> makes purpose limitation an obligation, not a preference."

### 11 · Search (35 s)

Search by attributes rather than plate: white car, Satellite zone, last hour.

> "Most investigations do not start with a plate. They start with 'white
> hatchback, near this junction, around this time'."

### 12 · Alert (40 s)

Open the alert list.

> "Alerts are deduplicated and severity-ranked. A watchlist hit on a
> stolen vehicle is CRITICAL; a camera going offline is not."

Open the audit trail.

> "Every access is audited — including refusals. An attempt to reach
> another department's camera is recorded with who tried, because a refusal
> that leaves no record is one an attacker can retry all year."

### 13 · Intelligence lookup — real vs mock (60 s)

**The most important honesty moment in the demo.** Do not skip it.

Open the intelligence panel on the target vehicle.

> "This is a VAHAN lookup. The vehicle comes back REPORTED STOLEN — and
> look at the banner: **DEMO DATA, not a real record.**"

Point at it.

> "We have no VAHAN credentials. Nobody gave us access, and we did not go
> looking for a way around that. So every one of the five adapters — VAHAN,
> SARTHI, eGujCop, AFIS, NAFIS — runs against a mock backend, and every
> record it returns is stamped, all the way to this screen."

Then the part that shows it is real engineering:

> "Screening a plate releases a status flag and nothing else. Not the
> owner's name, not their address. So a false-positive alert cannot expose
> a citizen who was never relevant. Owner details need a registered
> investigation with a case reference, because the audit log has to be able
> to answer 'under which case was this address retrieved' in five years."

> "And the fingerprint systems never return an identification. Only a
> candidate list and REFER TO EXAMINER. A match is an examiner's
> determination — a VMS printing a name from a score is manufacturing
> evidence."

### 14 · Camera health (30 s)

Switch to the Camera Health tab.

> "Estate health is a first-class view, not a diagnostic. Trust score per
> camera, firmware risk, frozen-picture detection — a live socket
> delivering an unchanging image is the most common silent failure in the
> field, and every other health signal looks perfect while it happens."

**If you have 30 seconds spare:** `docker compose stop ingestion`, watch it
go red, restart it. Recovering visibly from a real failure persuades more
than a demo where nothing goes wrong.

### 15 · Edge → regional → central (45 s)

One diagram, three numbers.

> "Eighty thousand cameras. Centralise the main streams and you need
> **320 gigabits per second** of state backbone. Centralise the metadata
> instead and you need **96 megabits**. That is a factor of 3,300, and it
> is the entire argument for processing at the edge."

> "Edge decodes and detects. Regional aggregates and does cross-camera
> matching, because vehicles do not teleport between districts. Central
> holds the registry, the map and the search."

> "Video does not move. Twenty-four petabytes of hot video at seven days'
> retention is a data-centre programme, not a storage line."

### 16 · Scale, and what we did not measure (50 s)

> "We measured the event path at 50, 250, 500 and 1,000 cameras. The first
> curve looked bad — cost per camera growing 3.8×. So we ran two controls."

> "Hold traffic constant and scale cameras: 10× the cameras costs 3.1× the
> time, and per-camera cost *falls*. Hold cameras constant and scale
> traffic: linear. The superlinearity was our own traffic simulator, which
> production does not have."

> "Above 1,000 cameras, everything is arithmetic over stated assumptions,
> and every cell in the capacity table says which it is — measured,
> calculated, estimated or projected."

Close on the limits:

> "Nothing here has run against the real Sentinel gateway. No government
> record system is connected. The accuracy figures are the simulator's.
> All three are in the traceability matrix, and we would rather you heard
> them from us."

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
| Gate reduction (3-min window) | 1.2 candidates of 49 — **97.6%** |
| Gate reduction (5-min window) | 3.3 candidates of 49 — **93.3%** |
| ANPR, dedicated lane, day | **92.5%** end-to-end |
| ANPR, wide-angle, night | **37.9%** — say this one |
| Estate cost | 14-22 ms/tick, **9-13%** of one core |
| Implied capacity | ~380-585 cameras per core (host-dependent) |
| Centralised vs metadata, 80k cameras | **320 Gbps vs 96 Mbps** |
| Departments / roles | 26 / 6 |
| Government systems connected | **0 of 5** — all mock, all stamped |
| Tests | **359 collected** |
| Gate, in comparisons | 20,000 pairs → **473**; 2,240 ms → **53 ms** |
| Migrations / API operations | 10 / 45 across 41 paths |

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
