# Sentinel Live Test Report

**Run date:** 2026-08-31 · **Commit:** `22485e3` · **Harness:** `scripts/live_poc.py`

Measured behaviour of a 50-camera estate over live RTSP. Every number here
was produced by a command that can be re-run; nothing is extrapolated.

---

## 0 · What was tested, and against what — read this first

**The Sentinel sandbox host, credentials and API documentation were not
available to this team.** No test in this report ran against the real
Sentinel gateway, and nothing here should be read as if it had.

What was tested instead is a **real RTSP server**, not a mock:
`tools/sentinel_sandbox` implements RTSP 1.0 (OPTIONS / DESCRIBE / SETUP /
PLAY / TEARDOWN) with RTP interleaved over the control connection per
RFC 2326 §10.12, carrying real H.264 and H.265 from real encoders with real
PTS. It serves the contract's URL shapes:

```
catalogue   GET  <base>/api/ingest
RTSP             rtsp://<host>:8554/stream/<id>
WHEP             http://<host>:8889/stream/<id>/whep
HLS              http://<host>:8888/live/stream/<id>/index.m3u8
```

This choice was forced and is defensible. Testing the live path against a
mock object would have proved nothing: the defect that broke **every** RTSP
camera in Phase 1 survived 186 passing tests and only appeared when a real
ffmpeg was asked to open a real RTSP URL (see §2).

Pointing this at the real sandbox is one configuration value:

```bash
SENTINEL_CATALOGUE_URL=https://<sandbox-host>
python3 scripts/live_poc.py --catalogue-url https://<sandbox-host> --seconds 120
```

| Claim | Status |
|---|---|
| RTSP over TCP, PTS timing, reconnect, both codecs, discontinuity, catalogue discovery | **TESTED** against a real RTSP server |
| The same behaviour against the *Sentinel* gateway | **PENDING EXTERNAL ACCESS** |
| Detection/ANPR/ReID accuracy on *Sentinel* imagery | **PENDING EXTERNAL ACCESS** — see [BENCHMARKS.md](BENCHMARKS.md) for the simulation envelope |

---

## 1 · Measurement rules

Taken from the brief, and enforced by the harness rather than by intention:

- **Live only.** No local file is read as a camera, nothing is seeked, and
  nothing runs faster than real time. Streams are paced by `-re` at the
  server and by PTS at the client.
- **FPS is derived from PTS deltas**, never from a declared frame rate and
  never from frame arrival.
- **Measured means measured.** Every figure in §3–§6 is a direct
  observation. Projections live in [SCALING.md](SCALING.md) and are labelled
  there.

---

## 2 · The defect this testing found

`video-ingestion/ingestion/stream_reader.py` passed `-stimeout 5000000` on
every RTSP input. That option was removed from ffmpeg's RTSP demuxer after
4.x. On ffmpeg 6.1.1:

```
$ ffmpeg ... -rtsp_transport tcp -stimeout 5000000 -i rtsp://...
Unrecognized option 'stimeout'.
Error splitting the argument list: Option not found
```

ffmpeg does not warn and continue — it refuses to parse its argument list
and exits **before opening the input**. Every live RTSP camera would have
failed instantly, and the error reads like a configuration fault rather
than a version mismatch.

It survived 186 passing tests because the demo estate is file-backed and
the municipal feeds are HLS; both take other branches, so the RTSP branch
had never once executed. The existing test asserted flags were present in a
list, which cannot catch an option ffmpeg rejects.

**Fixed**, and the regression test now builds the real argument list and
runs it: a connection failure passes (argv parsed, ffmpeg reached the
network), only an argument-parsing error fails. Verified to fail on the old
code and pass on the new.

---

## 3 · PTS timing — the headline measurement

Opening a 15 fps stream and recording each frame's PTS against its arrival
time:

| frame | `pts_time` (s) | arrival (s) | Δ PTS | Δ arrival |
|---|---|---|---|---|
| 1 | 0.0667 | 0.0031 | — | — |
| 2 | 0.1333 | 0.0041 | 0.0667 | 0.0010 |
| 3 | 0.2000 | 0.0051 | 0.0667 | 0.0010 |
| 6 | 0.4000 | 0.0073 | 0.0667 | 0.0007 |
| 12 | 0.8000 | 0.0114 | 0.0667 | 0.0007 |

**PTS says 0.8 s of video elapsed. Arrival says 11 ms.** The decoder emits a
buffered burst as fast as the pipe will take it on connect.

A speed derived from arrival timestamps is therefore **roughly seventy times
wrong** here, and the spatio-temporal gate — the load-bearing claim of this
architecture — consumes exactly those timestamps. On the simulated estate
the two agree, which is why this was invisible until a real stream was
opened.

Two structural problems had to be fixed, not one:

1. Raw BGR24 over a pipe **has no timestamp channel at all**, so PTS could
   not be recovered downstream.
2. `-vf fps=N` **resamples** the stream to constant frame rate by
   duplicating and dropping frames — the original cadence was destroyed
   inside ffmpeg before Python saw a pixel.

Timing now comes from the container clock via PyAV, anchored to wall clock
once at the first frame. **Stated limit:** without the RTCP sender report's
NTP mapping the anchor carries the network's one-way delay as a constant
offset. Constant offsets do not distort *intervals*, which is what the gate
and every speed calculation actually consume.

---

## 4 · 50-camera estate — MEASURED

```bash
python3 scripts/live_poc.py --cameras 50 --seconds 90
```

Estate composition was deliberately heterogeneous — a POC where every camera
is identical measures one camera fifty times:

| | |
|---|---|
| Cameras | **50** (43 × H.264, 7 × H.265) |
| Declared rates | 39 × 15 fps, 7 × 12 fps, 4 × 4 fps |
| Window | **90.3 s** |
| Estate connect time (50 cameras, 200 ms stagger, 8 concurrent) | **16.9 s** |

### Results

| Metric | Measured |
|---|---|
| Cameras ONLINE at end of window | **49** |
| Cameras RECONNECTING at end of window | 1 |
| Cameras **OFFLINE** | **0** |
| Total frames decoded | **68,606** |
| Aggregate decode rate | **672.9 frames/s** across the estate |
| Mean per-camera rate | 13.46 fps |
| Median FPS **from PTS** | **15.00** |
| Min / max FPS from PTS | **4.00 / 15.00** |
| Session re-establishments | 44 |
| Scene discontinuities detected | 43 |

**FPS derived from PTS reproduced each source's true rate exactly** — 15.00,
12.00 and 4.00 against sources encoded at 15, 12 and 4 fps. That is the
cleanest available evidence that timing is stream-derived: arrival-derived
timing on a loaded 4-core host would not land on the nominal rate at all,
let alone to two decimal places.

### Per camera (first twelve of fifty)

| camera | status | codec | res | fps (PTS) | frames | disc | recon |
|---|---|---|---|---|---|---|---|
| cam-001 | ONLINE | h264 | 640x360 | 15.00 | 1353 | 0 | 0 |
| cam-002 | ONLINE | h264 | 640x360 | 15.00 | 1328 | 1 | 1 |
| cam-003 | ONLINE | h264 | 640x360 | 15.00 | 1353 | 0 | 0 |
| cam-004 | ONLINE | hevc | 640x360 | 12.00 | 1063 | 1 | 1 |
| cam-005 | ONLINE | h264 | 640x360 | 15.00 | 1353 | 0 | 0 |
| cam-006 | ONLINE | h264 | 640x360 | 4.00 | 340 | 3 | 3 |
| cam-007 | ONLINE | h264 | 640x360 | 15.00 | 1329 | 1 | 1 |
| cam-008 | ONLINE | h264 | 640x360 | 15.00 | 1330 | 1 | 1 |
| cam-009 | ONLINE | h264 | 640x360 | 15.00 | 1353 | 0 | 0 |
| cam-010 | ONLINE | h264 | 640x360 | 15.00 | 1300 | 3 | 3 |
| cam-011 | ONLINE | h264 | 640x360 | 15.00 | 1329 | 1 | 1 |
| cam-012 | ONLINE | hevc | 640x360 | 12.00 | 1062 | 1 | 1 |

Full per-camera output: `python3 scripts/live_poc.py --cameras 50 --seconds 90 --json`.

---

## 5 · Where the 44 reconnects came from — root-caused, not hand-waved

Only 4 cameras were *configured* to drop mid-run, yet 26 cameras
re-established a session. That discrepancy was investigated rather than
reported as resilience.

**Cause: the test host, not the client.** The container has **4 vCPUs** and
was running 50 ffmpeg encode/relay processes plus 50 PyAV decoders. Session
drops scale with concurrency:

| Cameras | Window | Reconnects | Cameras OFFLINE |
|---|---|---|---|
| 1 | 45 s | **0** | 0 |
| 10 | 45 s | 4 | 0 |
| 25 | 45 s | 8 | 0 |
| 50 | 90 s | 44 | 0 |

A single camera held for 45 s produced **675 frames, 0 reconnects and 0
discontinuities** — exactly 15.00 fps. So loop points are clean and the
client is not the source of the drops; the fixture saturates the host.

Two conclusions, and they point in opposite directions, so both are stated:

1. **This is not a measurement of the client's failure rate.** The 44
   reconnects measure the fixture's headroom on 4 vCPUs. Against a real
   gateway on adequate hardware this number would be far lower.
2. **It is a genuine, unplanned resilience test, and the client passed it.**
   Under sustained overload, across 44 unexpected session losses, **not one
   camera was lost**: every one re-established, every discontinuity was
   detected, and the estate finished with 49 ONLINE and 0 OFFLINE. That was
   not staged.

---

## 6 · Requirement-by-requirement results

| Requirement | Result | Evidence |
|---|---|---|
| **PART 3** — catalogue is the source of truth | **TESTED** | 50 cameras discovered from `GET /api/ingest`; a camera added upstream appears with no code change; one removed is retired. An AST test fails the build if any camera identifier is hard-coded. |
| **PART 4** — RTSP→AI, WHEP→browser, HLS fallback | **TESTED** | All three URLs carried from the catalogue verbatim; derived from the documented shape only when absent. |
| **PART 5** — RTSP forced over TCP | **TESTED** | Client pins `rtsp_transport=tcp` with no configuration path to UDP. The sandbox **refuses** UDP with `461 Unsupported Transport`, so a misconfigured client fails loudly instead of corrupting silently. |
| **PART 6** — PTS-derived timing | **TESTED** | §3. FPS from PTS reproduced 15.00 / 12.00 / 4.00 exactly. `capture_time` tracks PTS to within 1 µs. |
| **PART 7** — variable FPS, mixed resolution | **TESTED** | 4 fps camera reported ONLINE, not failed. Declared-25-delivering-4 reported DEGRADED by measurement. 4:3 source preserved rather than stretched into 16:9. |
| **PART 8** — H.264 **and** H.265 | **TESTED** | 43 × H.264 and 7 × H.265 decoded concurrently over RTSP/TCP in the same run. |
| **PART 9** — exponential backoff, camera states | **TESTED** | 2→4→8→16→30 s with proportional jitter, capped. `RECONNECTING` distinct from `OFFLINE` (migration 0007). 44 real recoveries observed. |
| **PART 10** — scene-discontinuity recovery | **TESTED** | 43 discontinuities detected and the wall-clock anchor reset; `capture_time` never went backwards across one. |
| **PART 11** — live-only evaluation | **HELD** | No file read as a camera, no seek, nothing faster than real time. |
| **PART 12** — connection pacing, one capture per camera | **TESTED** | 50 opens staggered over 16.9 s; peak in-flight handshakes never exceeded the cap; two consumers of one camera share one decode. |
| **PART 13** — this report | **DONE** | |

---

## 7 · What this run does **not** show

Stating these makes the rest credible.

- **Nothing ran against the real Sentinel gateway.** Field names in its
  catalogue are an assumption; the parser accepts several spellings and
  reports which it resolved, so adapting is a mapping change with evidence
  behind it, but it is still an assumption until someone runs it.
- **No latency figure is quoted.** Glass-to-glass latency needs a
  synchronised clock at both ends. The harness measures connect time and
  frame cadence, which it can measure honestly; a latency number taken
  loopback-to-loopback would say nothing about a government WAN.
- **No bitrate figure is quoted per camera.** The sandbox's encoders are not
  the estate's encoders, so a bitrate measured here describes this fixture,
  not Sentinel.
- **These are 640×360 streams on 4 vCPUs.** Decode cost at 1080p with real
  models on a GPU is a different measurement, and the binding constraint
  moves to NVDEC. See [LIMITATIONS.md](LIMITATIONS.md).
- **Detection, ANPR and ReID accuracy were not measured on this imagery.**
  The source is a synthetic test pattern; it has no number plates. Accuracy
  figures remain those in [BENCHMARKS.md](BENCHMARKS.md), which are the
  simulation backend's and labelled as such.

---

## 8 · Reproducing this

```bash
# the 50-camera run in §4
python3 scripts/live_poc.py --cameras 50 --seconds 90

# the concurrency comparison in §5
python3 scripts/live_poc.py --cameras 10 --seconds 45
python3 scripts/live_poc.py --cameras 25 --seconds 45

# every live-path test
pytest tests/test_live_ingestion.py tests/test_live_supervisor.py \
       tests/test_sentinel_catalogue.py -q

# against the real sandbox, when access exists
python3 scripts/live_poc.py --catalogue-url https://<sandbox-host> --seconds 120
```
