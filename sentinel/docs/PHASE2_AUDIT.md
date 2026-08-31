# Phase 2 Audit — what actually works today

**Audit date:** 2026-08-31 · **Commit audited:** `d0d0b97` · **Branch:** `claude/gujarat-police-vms-mvp-jnlpy6`

This document is the starting point for Phase 2. It records what was
**executed**, not what the README claims. Everything below was run on the
audit machine before any Phase 2 code was written, so the baseline is
honest and later work can be measured against it.

Where a Phase 1 claim did not survive being re-run, it is marked
**DRIFT** and corrected here rather than quietly left in place.

---

## Status vocabulary

Used consistently in this document and every Phase 2 document that follows.

| Label | Meaning |
|---|---|
| **IMPLEMENTED** | Code exists and runs. |
| **TESTED** | Code exists and is covered by an automated test that was executed in this audit. |
| **SIMULATED** | Runs against synthetic input; real-world input has never touched it. |
| **DESIGNED** | Documented and schema'd, but no executable implementation. |
| **PENDING EXTERNAL ACCESS** | Blocked on a credential, host or network the team does not have. |
| **BROKEN** | Code exists and does not work. Found by running it. |

---

## 1 · Executed baseline

Every command in this section was actually run on 2026-08-31. Nothing here
is quoted from a previous session.

| What | Command | Result |
|---|---|---|
| Test suite | `pytest tests/ -q` (live PostgreSQL 16 + PostGIS + pgvector) | **186 passed, 1 warning, 39.11 s** |
| Benchmark | `python3 scripts/benchmark.py` | ran all 7 suites, see drift below |
| Frontend typecheck | `npm run typecheck` (`tsc --noEmit`) | **clean, no errors** |
| Frontend build | `npm run build` | **built in 6.77 s**; maplibre chunk 801 kB (218 kB gzip) |
| API import + OpenAPI | `app.openapi()` | **42 documented operations** + 1 WebSocket |
| Migrations | 6 files, checksum ledger | applied; `sentinel` and `sentinel_test` both live |
| Compose syntax | `docker compose config -q` | **valid**, exit 0 |
| Compose runtime | `docker compose up` | **NOT EXECUTED** — see §2 |

### Environment the baseline was taken on

PostgreSQL 16 on `127.0.0.1:5433`, Redis 7 on `127.0.0.1:6380`,
ffmpeg/ffprobe **6.1.1-3ubuntu5**, Python 3.11, Node 20, Docker Engine
29.3.1 client + daemon. No GPU. Single container, shared CPU — absolute
timings are indicative, ratios are meaningful.

---

## 2 · Docker Compose could not be executed, and why

This is the single largest unverified claim inherited from Phase 1, so it
is worth being precise rather than repeating "no Docker daemon".

1. The Docker **daemon does start** in this environment (`dockerd` runs as
   root; `docker info` reports a healthy server, overlayfs, buildkit
   initialised).
2. `docker compose config -q` **validates the whole file**, exit 0.
3. `docker pull redis:7-alpine` **fails**: the registry manifest resolves,
   then the blob CDN is refused —
   `Get "https://production.cloudfront.docker.com/..." : Forbidden`.
4. The session's egress proxy confirms this is a **policy denial**, not a
   network fault:
   `{"kind":"connect_rejected","detail":"gateway answered 403 to CONNECT (policy denial or upstream failure)","host":"production.cloudfront.docker.com:443"}`

Base images cannot be pulled, so images cannot be built either. The
documented rule for this environment is to report a policy denial rather
than route around it, so no attempt was made to do so.

> **`make demo` therefore remains DESIGNED / NOT EXECUTED.** The Compose
> file is syntactically valid and the daemon works; the image layers are
> unreachable from this network. Every component it orchestrates has been
> run individually, host-native, and that is the evidence this submission
> stands on. This must be stated plainly in the submission rather than
> implied to be working.

**Required change:** run `make demo` end-to-end on any machine with an
open path to Docker Hub and capture the terminal output as evidence. This
is a 15-minute task on an unrestricted network and it closes the gap
completely.

---

## 3 · DRIFT — Phase 1 claims that did not survive re-running

| Claim (README / docs) | Re-measured 2026-08-31 | Verdict |
|---|---|---|
| "14.2 ms/tick, **9%** of one core" | **17.1 ms/tick, 10%** | **DRIFT** — same code, different host. Ratio holds, absolute number does not. |
| "implied capacity ~**585** cameras per core" | **~488 cameras per core** | **DRIFT** — derived from the tick figure above. |
| "**38** endpoints" | **39** operations under `/api/v1` (+3 ops, +1 WS) | **DRIFT** — undercount. |
| "**186 passing**" | **186 passed** | **CONFIRMED** |
| "8 pages" | 9 page components (8 + Login) | Defensible, but say "8 + login". |
| "6 migrations" | 6 | **CONFIRMED** |
| "50 cameras / 13 ANPR-capable" | 26/50 produced sightings in a 30 s window | **CONFIRMED** (not all cameras see traffic in 30 s) |
| ANPR 92.5% day / 37.9% night wide-angle | reproduced exactly | **CONFIRMED** |
| Gate: 3.3 of 49 at 180 s | this run: **1.2 of 49 at 180 s**, 3.3 at **300 s** | **DRIFT** — the 3.3 figure is the **300 s** window, not 180 s. |

The gate drift matters most: **the README, SCALING.md, DEMO_SCRIPT.md and
the "numbers to memorise" table all attribute 3.3 candidates to a 3-minute
window, when the benchmark attributes it to a 5-minute window.** At 3
minutes the true figure is 1.2 of 49, which is a *better* number. This is a
documentation error that a judge could catch by running `make benchmark`,
and it must be corrected everywhere before submission.

**Priority: P0 — correctness of a headline claim.**

---

## 4 · BROKEN — found by running, not reading

### 4.1 Every live RTSP camera would fail to open

`video-ingestion/ingestion/stream_reader.py` passes `-stimeout 5000000`
for RTSP inputs. On ffmpeg 6.1.1:

```
$ ffmpeg ... -rtsp_transport tcp -stimeout 5000000 -i rtsp://...
Unrecognized option 'stimeout'.
Error splitting the argument list: Option not found
```

ffmpeg does not merely warn — it **refuses to parse the argument list and
exits before opening the input**. The RTSP demuxer's option is `-timeout`
(microseconds); `-stimeout` was removed after ffmpeg 4.x. Verified against
`ffmpeg -h demuxer=rtsp`, which lists `-timeout` and `-listen_timeout`
only.

The same command with `-timeout` reaches the network correctly:

```
$ ffmpeg ... -rtsp_transport tcp -timeout 5000000 -i rtsp://127.0.0.1:9999/x
[tcp @ ...] Connection to tcp://127.0.0.1:9999?timeout=5000000 failed: Connection refused
```

**Why Phase 1 never caught it:** the demo estate is file-backed and the
municipal feeds are HLS. Both take the *other* branches of
`_input_options()`. The RTSP branch has never once been executed against a
real RTSP source. Every test that exercises ingestion exercises the
simulated world, not this code path.

This is exactly the class of bug the Phase 1 retrospective warned about —
found by running, not by reading — and it sits directly in front of the
Sentinel sandbox integration.

**Status: BROKEN. Priority: P0.** Nothing in PART 3–13 can work until this
is fixed, because every Sentinel camera is RTSP.

**Required change:** use `-timeout` for RTSP, add a version-aware option
builder, and add a regression test that asserts the generated argument list
is accepted by the installed ffmpeg.

---

## 5 · Requirement-by-requirement audit

Priorities: **P0** blocks the Sentinel integration · **P1** required by a
mandatory submission item · **P2** strengthens the submission · **P3** nice
to have.

### PART 3–5 · Sentinel sandbox integration, protocol routing, RTSP over TCP

| Requirement | Current implementation | Status | Evidence | Problem | Required change | Pri |
|---|---|---|---|---|---|---|
| Consume `GET /api/ingest` as catalogue source of truth | none — no HTTP client for a catalogue exists anywhere | **PENDING** | `grep -rni "/api/ingest\|catalogue"` → no hits in `.py` | The single highest-priority Phase 2 requirement is entirely absent | Build a catalogue client with reconciliation (add/remove/change) against the DB | **P0** |
| Never hard-code the camera catalogue | `config/cameras.yaml` + `load_from_database`; nothing hard-codes a live estate | **IMPLEMENTED** | `ingestion/camera_config.py` | Good foundation, but has no notion of a remote catalogue | Extend `load_cameras()` with a `sentinel` source | **P0** |
| RTSP → AI pipeline | `FrameReader` exists | **BROKEN** | §4.1 | `-stimeout` kills the process before connect | Fix option; then test against a real RTSP server | **P0** |
| WHEP → browser | `/cameras/{id}/stream` returns a WHEP URL | **IMPLEMENTED (wrong shape)** | `routers/cameras.py:216-238` | Builds `{base}/cam-{id}/whep` from a **local MediaMTX** convention; the Sentinel contract is `:8889/stream/<id>/whep`. Also derives the HLS port by `base.replace('8889','8888')`, which silently produces a wrong URL if the base port ever differs | Drive playback URLs from the catalogue entry, not string surgery | **P0** |
| HLS fallback | `llhls_url` returned | **IMPLEMENTED (wrong shape)** | same | same as above; contract path is `/live/stream/<id>/index.m3u8` | same | **P0** |
| Browser must never consume RTSP | API returns URLs only, never proxies video | **TESTED** | `test_api.py:310` asserts a WHEP URL, not video | none | none | — |
| Force RTSP over TCP everywhere | `probe()` and `FrameReader` both pass `-rtsp_transport tcp` | **IMPLEMENTED** | `stream_reader.py:57, 123` | No regression test asserts it; a future edit could drop it silently | Add a test that greps every generated ffmpeg command for `-rtsp_transport tcp` | **P1** |

### PART 6–8 · PTS timing, variable FPS, codecs

| Requirement | Current implementation | Status | Evidence | Problem | Required change | Pri |
|---|---|---|---|---|---|---|
| **All timing derived from PTS** | **none** | **PENDING** | `grep -rn "pts\|best_effort_timestamp"` → zero hits outside an unrelated `geo.py` local variable | This is the deepest architectural gap in the audit. See below. | Carry PTS from decode through to every timestamp | **P0** |
| Never use frame arrival time | `self.last_frame_at = time.time()` | **BROKEN** | `stream_reader.py:180` | Wall-clock arrival is precisely what PART 6 forbids | Replace with PTS-derived stream clock | **P0** |
| Never use `CAP_PROP_FPS` | no OpenCV; but `-vf fps={self.fps}` **resamples** | **BROKEN** | `stream_reader.py:143` | Worse than `CAP_PROP_FPS`: the filter *forces CFR* by duplicating and dropping frames, destroying original frame timing **inside ffmpeg** before Python ever sees it. Raw `bgr24` over a pipe carries no timestamps at all, so PTS is unrecoverable downstream. | Emit timestamps alongside frames (e.g. `-vsync passthrough` + a PTS side-channel, or `showinfo`/`-f nut`), and stop resampling | **P0** |
| Tracker uses PTS | `ByteTracker` is frame-index based | **PENDING** | `ai/sentinel_ai/tracker.py` | Track age, velocity and `max_age` are all in frames, so they mean different wall-times on a 25 fps and a 6 fps camera | Age tracks in seconds from PTS | **P0** |
| Travel-time gate uses PTS | gate uses `Sighting.timestamp` (wall clock) | **PARTIAL** | `event-processor/processor/store.py` | Correct *if* the sighting timestamp is true capture time; today it is arrival time | Falls out of the PTS fix | **P0** |
| Variable FPS tolerated, not a failure | `fps=` filter forces CFR; `is_stalled` uses a fixed 15 s | **PARTIAL** | `stream_reader.py:198` | A legitimately low-rate camera looks stalled; a variable-rate one is silently resampled | Per-camera adaptive stall threshold from observed PTS cadence | **P1** |
| Mixed resolutions | `scale={w}:{h}` normalises | **IMPLEMENTED** | `stream_reader.py:143` | Aspect ratio is not preserved — a 4:3 camera is stretched into 16:9, which distorts plate glyphs and the ReID crop | Letterbox instead of stretch, or scale by long edge | **P1** |
| H.264 **and** H.265 | ffmpeg decodes both; never tested | **SIMULATED** | no codec test exists | Unverified on H.265, which is common on newer Indian estates | Test both codecs against a real server | **P1** |
| Keyframe-startup warnings not fatal | decoder stderr is captured and truncated to 300 chars | **PARTIAL** | `stream_reader.py:185` | Startup warnings before the first keyframe are recorded as `last_error`, making a healthy camera look faulty | Classify stderr: warning vs fatal | **P1** |

**On PART 6 specifically.** This is not a small fix. The current design
converts video to raw frames over a pipe, which is a format with no
timestamp channel. Every timestamp in the system is therefore the moment
Python read bytes off a socket — after network jitter, decoder buffering
and ffmpeg's own CFR resampling. The spatio-temporal gate, which is the
central claim of the whole architecture, is computed on those timestamps.
On the simulated estate the two agree, which is why 186 tests pass. On a
real network they will not, and the gate will silently widen or produce
false negatives. **This must be fixed before any live-feed measurement is
credible.**

### PART 9–13 · Resilience and live POC

| Requirement | Current implementation | Status | Evidence | Problem | Required change | Pri |
|---|---|---|---|---|---|---|
| Exponential backoff 2→4→8→…→30 s | fixed `time.sleep(3.0)` | **PARTIAL** | `stream_reader.py:191` | Constant retry; a down camera is hit every 3 s forever, and 50 workers do it in lockstep | Exponential backoff with jitter, capped at 30 s | **P0** |
| ONLINE / DEGRADED / OFFLINE / RECONNECTING | `CameraStatus` enum has PENDING/PROBING/ONLINE/DEGRADED/OFFLINE/DISABLED | **PARTIAL** | `shared/sentinel_core/domain.py` | **No `RECONNECTING` state**, and `grep -n "status" worker.py` returns nothing — the reader never drives the state machine | Add RECONNECTING; drive transitions from the reader | **P0** |
| Scene-discontinuity recovery at loop points | `_measure_scene_change()` exists in the worker | **PARTIAL** | `ingestion/worker.py` | Detects a frozen picture, but nothing resets tracker/ReID state on a hard scene cut, so tracks bridge across a loop point and fabricate an impossible journey | Reset tracker + flush open tracks on discontinuity; add a test | **P0** |
| Live-only evaluation, no seeking | demo path uses `-stream_loop -1 -re` on local files | **IMPLEMENTED** | `stream_reader.py:133` | Correct for demo; must be provably unused for Sentinel cameras | Assert the file branch is unreachable for `sentinel`-sourced cameras | **P1** |
| Connection pacing, one capture per camera | **none** | **PENDING** | `grep -rn "stagger\|semaphore\|max_concurrent"` → no hits | 50 workers will open 50 RTSP sessions simultaneously at startup. Against a real gateway this looks like a burst and can get the client throttled or banned | Add a startup stagger and a concurrency semaphore | **P0** |
| Internal fan-out, close unused | one `FrameReader` per worker | **IMPLEMENTED** | `ingestion/worker.py` | No reference counting, but also no duplicate readers today | Verify under the catalogue reconciler | **P2** |
| `docs/SENTINEL_LIVE_TEST_REPORT.md` | does not exist | **PENDING** | — | Mandatory Phase 2 deliverable | Produce from a real ~50-camera run | **P0** |

### PART 14–17 · Analytics quality

| Requirement | Current implementation | Status | Evidence | Problem | Required change | Pri |
|---|---|---|---|---|---|---|
| Full designated-vehicle pipeline, not ANPR-only | detect → track → gate → ANPR + ReID + colour + type, fused | **TESTED** | `ai/sentinel_ai/pipeline.py`; benchmark suites 1–5 | Runs on synthetic imagery only | Re-measure on real frames | **P1** |
| ANPR quality gate on real imagery | `quality.py` thresholds, ~80% rejection | **SIMULATED** | benchmark suite 3 | Thresholds tuned against the simulator, never against a real plate crop | Re-tune on Sentinel frames | **P1** |
| Use camera capability metadata | `anpr_capable` per camera | **IMPLEMENTED** | `config/cameras.yaml` | Set by hand; the Sentinel catalogue may expose resolution/FOV to infer it | Derive from catalogue where possible | **P2** |
| Cross-camera ReID with confidence | fusion scoring, CONFIRMED vs PROBABLE | **TESTED** | `shared/sentinel_core/fusion.py`; suite 5 | Surfaces as "Probable", not "POSSIBLE MATCH / XX%" as PART 16 asks | Add explicit percentage to the UI label | **P2** |
| Measure gating before/after | benchmark suite 7 reports candidate reduction | **TESTED** | suite 7 output | Reports candidates, **not** pairs-scored or processing time as PART 17 requires | Extend to pairs and milliseconds | **P1** |

### PART 18–22 · Integration, departments, federation, legacy, edge

| Requirement | Current implementation | Status | Evidence | Problem | Required change | Pri |
|---|---|---|---|---|---|---|
| VAHAN / SARTHI / eGujCop / AFIS / NAFIS adapters | **none** — mentioned in prose only | **PENDING** | `grep -rli "vahan\|sarthi"` → only `LEGACY_INTEGRATION.md`, `LIMITATIONS.md` | A mandatory deliverable with zero code | Build adapter interfaces + clearly labelled mocks | **P1** |
| `docs/GOVERNMENT_INTEGRATION.md` | does not exist | **PENDING** | — | Mandatory | Write it | **P1** |
| 26-department model | `department` table; 8 seeded | **PARTIAL** | `0002_core_entities.sql`; seed | Only 8 departments, and the brief specifies 26 | Seed all 26 | **P1** |
| Roles: State Admin, Dept Admin, Police Operator, Investigator, Auditor | VIEWER/OPERATOR/INVESTIGATOR/ADMIN/SYSTEM | **PARTIAL** | `security.py:30-36` | No State-vs-Department admin split, and **no Auditor role** | Extend the enum and permission map | **P1** |
| Department-scoped access | `department_id` on `app_user` and `camera` | **BROKEN (as a control)** | `grep -rn "department_id" backend/app/` → 5 hits, all `JOIN … FOR DISPLAY`; **no `WHERE` clause filters by the caller's department** | Every authenticated user can see every camera in every department. For a 26-department federated system this is the central access-control requirement, and it is absent | Enforce department scoping in queries; add tests | **P1** |
| VMS federation models 1–4 | ARCHITECTURE.md argues federation | **DESIGNED** | `docs/ARCHITECTURE.md` | Not enumerated as four models with selection criteria | Write the comparison | **P2** |
| Legacy analog/DVR | vendor URL templates, DVR adapter | **IMPLEMENTED** | `ingestion/adapters.py` | Untested against real hardware | Label honestly | **P2** |
| Edge / regional / central | argued in SCALING.md | **DESIGNED** | `docs/SCALING.md` | No executable artefact | Keep as design; label it | **P2** |

### PART 23–30 · Scale, storage, network, DR

| Requirement | Current implementation | Status | Evidence | Problem | Required change | Pri |
|---|---|---|---|---|---|---|
| Capacity at 50/1k/3k/10k/50k/80k, labelled | SCALING.md gives 50/3k/80k | **PARTIAL** | `docs/SCALING.md` | Missing 1k, 10k, 50k; and figures are not labelled MEASURED vs CALCULATED vs ESTIMATED vs PROJECTED | Rebuild the table with per-cell labels | **P1** |
| Load simulator → `docs/SCALE_BENCHMARK.md` | `scripts/benchmark.py` measures 50 cameras | **PARTIAL** | suite 6 | Only 50; the 3,000-camera claim is argued, never run | Build a load simulator; measure at least 500–1,000 | **P1** |
| District sharding + border cameras | argued; adjacency graph supports it | **DESIGNED** | `docs/SCALING.md` | Never exercised | Validate with a two-district run | **P2** |
| Tiered storage HOT/WARM/COLD | retention days per partitioned table | **PARTIAL** | `0005_partitions_and_functions.sql:15-19` | Retention exists; **no tier concept**, and 7/15-day configurability is not exposed | Add tiering + configurable retention | **P2** |
| `docs/NETWORK_BANDWIDTH_PLAN.md` | arithmetic scattered in README/SCALING | **PENDING** | — | Mandatory deliverable | Write it, comparing centralised vs federated vs hybrid | **P1** |
| Disaster recovery | LIMITATIONS notes single PostgreSQL, no PITR | **PENDING** | `docs/LIMITATIONS.md` | No DR document for the eight failure domains | Write `docs/DISASTER_RECOVERY.md` | **P1** |

---

## 6 · What is genuinely strong and must not be broken

Phase 2 must not regress any of this. It is the load-bearing part of the
submission.

- **186 tests against a live PostgreSQL**, not mocks. They caught real bugs
  in Phase 1 and are the reason the audit could move fast.
- **The spatio-temporal gate as an index, not a filter** — 344 pairs scored
  instead of ~500,000. This is the claim that makes 80,000 cameras
  arguable, and it is implemented, not just described.
- **Honest accuracy reporting.** 37.9% night wide-angle ANPR is published.
  Very few submissions will publish their worst number.
- **Credentials cannot reach the database.** A schema-level test asserts it.
- **Fusion never lets appearance auto-confirm** (`NO_PLATE_CEILING = 0.79`).
  This is a safety property, not a tuning choice.
- **Migration checksum ledger** refuses to run if an applied migration file
  changed.
- **Air-gapped frontend** — no font CDN, no map token, verified in a real
  browser with the tile CDN blocked.

---

## 7 · Ordered plan out of this audit

**P0 — blocks everything (do first, in this order):**

1. Fix `-stimeout` → `-timeout`; add a regression test that the generated
   ffmpeg argv is accepted by the installed binary. (§4.1)
2. Build a **contract-conformant local RTSP gateway** so PARTS 4–13 can be
   genuinely tested without the real sandbox host. Real H.264 + H.265, real
   PTS, real loop points, real disconnects.
3. **PTS end-to-end.** Stop resampling with `-vf fps=`; carry capture
   timestamps from decode to sighting to gate. Re-verify the gate.
4. Exponential backoff + `RECONNECTING` state + reader-driven state machine.
5. Scene-discontinuity reset of tracker/ReID state, with a test.
6. Connection pacing: startup stagger + concurrency cap.
7. Sentinel catalogue client with reconciliation; protocol routing from
   catalogue data rather than string surgery.

**P1 — mandatory submission items:**

8. Correct the **3.3-candidates-at-180 s** error everywhere. (§3)
9. Department scoping as an enforced control; 26 departments; Auditor and
   the State/Dept admin split.
10. Government adapter interfaces + labelled mocks + `GOVERNMENT_INTEGRATION.md`.
11. Load simulator + `SCALE_BENCHMARK.md`; capacity table with per-cell
    provenance labels.
12. `SENTINEL_LIVE_TEST_REPORT.md`, `NETWORK_BANDWIDTH_PLAN.md`,
    `DISASTER_RECOVERY.md`.
13. Correct the endpoint count (39) and the benchmark drift in the README.

**P2 — strengthens the submission:** federation models 1–4, tiered storage,
district-shard validation, POSSIBLE MATCH percentage in the UI, letterbox
instead of stretch.

---

## 8 · Honest summary

The Phase 1 system is real: it runs, it is tested against a live database,
its numbers are reproducible, and its architecture is sound. Three things
are true at the same time and all three belong in the submission:

1. **The core is genuinely built and measured** — the pipeline, the gate,
   the matcher, the API, the frontend, and 186 executed tests.
2. **The live-feed path has never been executed against a real RTSP
   source**, and it contains a fatal argument bug that proves it. Every
   PART 3–13 requirement rests on code that has only ever run against
   simulated input.
3. **Timing is architecturally wrong for live video.** Nothing derives from
   PTS, and the current decode command actively destroys the information
   needed to fix it.

Items 2 and 3 are the real work of Phase 2, and neither is discoverable by
reading the code — only by running it against something that behaves like a
real camera. Building that something is therefore the first task.

---

## 9 · Resolution record — what this audit found, and where it stands

**Added 2026-08-31, after Phase 2B.** Everything above is left exactly as
it was written at commit `d0d0b97`: it is the baseline, and editing a
baseline to make it agree with later work destroys the only thing it was
for. This section records what has since happened to each P0 finding, with
the test that holds it.

| # | Finding (§5) | Status now | Held by |
|---|---|---|---|
| 1 | `GET /api/ingest` catalogue absent | **CLOSED** | `test_sentinel_catalogue.py` — discovery, reconciliation, no hard-coded identifier |
| 2 | `-stimeout` aborts every RTSP camera | **CLOSED** | Flag probed against the installed binary; `test_generated_ffmpeg_argv_is_accepted_by_the_installed_binary` runs ffmpeg and asserts it parses. This ffmpeg wants `-timeout` |
| 3 | Nothing derives from PTS | **CLOSED** | `LiveStreamReader` via PyAV; `test_frame_timing_comes_from_pts_not_from_arrival` — PTS 0.8 s against arrival 11 ms on connect |
| 4 | `-vf fps=` forces CFR inside ffmpeg | **CLOSED** | Removed from the live path, and `FrameReader` **deleted** rather than deprecated. `test_no_forced_constant_frame_rate_survives_anywhere_in_ingestion` scans every module's string literals; `test_the_removed_cfr_reader_has_not_come_back` |
| 5 | Tracker ages in frames | **CLOSED** | `max_age_s`, per-interval velocity, `predict(dt)`; a 200 px/s vehicle through 40/80/45 ms gaps tracks at 198 px/s |
| 6 | Fixed `time.sleep(3.0)` retry, no RECONNECTING | **CLOSED** | `backoff_delay` 2→4→8→16→30 s jittered; migration 0007; `test_a_dropped_stream_reconnects_and_reports_reconnecting` |
| 7 | Nothing resets tracker/ReID at a scene cut | **CLOSED** | `reset_for_discontinuity`; `test_a_discontinuity_does_not_swallow_the_next_vehicle` |
| 8 | Codecs and resolutions untested | **CLOSED** | 43 × H.264 + 7 × H.265 concurrently over RTSP; aspect ratio preserved |
| 9 | TCP not asserted anywhere | **CLOSED** | Pinned with no config path to UDP, in both the reader and `probe()`; the sandbox refuses UDP with 461 |
| 10 | Live-only not provable | **CLOSED** | Non-live schemes refused by `start()`; AST scans for seeks and file opens |
| 11 | No connection pacing | **CLOSED** | Stagger plus concurrency cap; 50 opens over 16.9 s |
| — | Playback URLs built by string surgery | **CLOSED** | Migration 0009; served verbatim from the catalogue |
| — | Department scoping absent as a control | **CLOSED** | `dept_filter` on every query; 26 security-regression tests over a two-department estate |
| — | Government adapters absent | **CLOSED** as mocks | Five adapters, every record stamped `MOCK`, `RealBackend` raises and names what is missing |
| — | Gate reported candidates, not pairs or ms | **CLOSED** | `MatcherStats.scored_pairs` vs `ungated_pairs`, gate and scorer timed separately |
| — | Retention was one cliff, hard-coded | **CLOSED** | Migration 0010: HOT/WARM/COLD ordered by CHECK, configurable per table, COLD detaches rather than drops |

**Still open, and not closed by any amount of further work here:**

| Finding | Why it stays open |
|---|---|
| **Real Sentinel gateway** | Host, credentials and API documentation were never available. Every live-feed result is against `tools/sentinel_sandbox` — a real RTSP 1.0 server with real RTP and real PTS, but ours. **REAL SENTINEL VALIDATION — PENDING EXTERNAL ACCESS** |
| **Real government records** | Five separate institutional authorisations |
| **AI accuracy on real imagery** | 92.5% day / 37.9% night are the simulation backend's, and are labelled that way everywhere |
| **Anything above 1,000 cameras** | Arithmetic over stated assumptions, never executed |
| **Database replication / PITR** | Not built. The single largest unmitigated risk in the system |
| **`make demo`** | Compose validates; the Docker blob CDN returns 403 by network policy. The host-native path has been run end to end |

**Baseline against today:** 186 tests at audit → **346**. Migrations 6 → 10.
The two findings §8 called "the real work of Phase 2" — the live-feed path
and PTS timing — are both closed, and both are held by tests that run
against a real RTSP server rather than a simulated one.
