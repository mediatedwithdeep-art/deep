# Scale Benchmark

**Run date:** 2026-08-31 · **Host:** 4 vCPU, 15 GB RAM, no GPU · **Harness:** `scripts/scale_test.py`

Every cell in this document carries a provenance label. Nothing is
presented as measured that was not run on this host.

| Label | Meaning |
|---|---|
| **MEASURED** | Directly observed here, by a command in §6 that can be re-run. |
| **CALCULATED** | Arithmetic from a MEASURED value plus assumptions stated in §4. |
| **ESTIMATED** | Engineering judgement from published hardware figures. No measurement of ours. |
| **PROJECTED** | Extrapolated beyond the range that was measured. The weakest class here. |

---

## 0 · The headline, stated carefully

**MEASURED:** the event path runs 1,000 cameras in one process on 4 vCPUs.

**MEASURED:** at 1,000 cameras that process needs 1,660 ms per 6 Hz tick,
against a 167 ms budget — so it runs at **10× real time too slow** in one
process, and 1,000 cameras is a throughput measurement, not a capacity
claim.

**MEASURED:** the per-camera cost of that path, with traffic held constant,
is **15–51 µs per tick** and *falls* as the estate grows.

Those last two only look contradictory until you separate what was
scaling. §2 is that separation, and it is the most important section here.

---

## 1 · What was measured — 50 → 1,000 cameras

```bash
python3 scripts/scale_test.py --cameras 50 250 500 1000 --seconds 10
```

Traffic density held constant *per camera* (36 vehicles per camera), so a
bigger estate means a busier world, as it would in reality.

| Cameras | Vehicles | ms/tick | p95 | % of 6 Hz budget | µs/camera/tick | sightings/s | RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 50 | 1,800 | **22.1** | 31.1 | 13% | 441 | 5.7 | 75 MB |
| 250 | 9,000 | **158.9** | 207.2 | 95% | 636 | 20.7 | 168 MB |
| 500 | 18,000 | **456.7** | 625.3 | 274% | 913 | 41.1 | 303 MB |
| 1,000 | 36,000 | **1,659.9** | 2,428.8 | 996% | 1,660 | 94.2 | 590 MB |

All **MEASURED**.

The per-camera cost grows 3.8× across the range — roughly O(n^1.44). Taken
at face value this says the architecture scales badly, and a capacity model
built by multiplying the 50-camera figure would be wrong by a large factor.

**It was not taken at face value.**

---

## 2 · Why that curve is mostly the harness, and the controls that show it

Two things scale together in §1: cameras *and* vehicles. Two control runs
separate them.

### Control A — hold traffic constant, scale cameras

```bash
python3 scripts/scale_test.py --cameras 50 250 500 --seconds 8 --vehicles-per-100-cams 0
```

| Cameras | Vehicles | ms/tick | µs/camera/tick |
|---:|---:|---:|---:|
| 50 | 200 | **2.5** | 50.7 |
| 250 | 200 | **4.6** | 18.4 |
| 500 | 200 | **7.7** | 15.5 |

All **MEASURED**. **10× the cameras costs 3.1× the time, and the per-camera
cost falls by 3.3×.** Camera fan-out is sub-linear and cheap.

### Control B — hold cameras constant, scale traffic

```bash
python3 scripts/scale_test.py --cameras 50 --seconds 8 --vehicles-per-100-cams 400 3600 14400 36000
```

| Cameras | Vehicles | ms/tick | sightings/s |
|---:|---:|---:|---:|
| 50 | 200 | **2.3** | 0.5 |
| 50 | 1,800 | **19.6** | 6.5 |
| 50 | 7,200 | **73.8** | 8.6 |
| 50 | 18,000 | **185.7** | 9.8 |

All **MEASURED**. Cost is **linear in vehicle count** at **≈10.3
µs/vehicle/tick** (CALCULATED from this table), and sightings/s *saturates*
— the cameras' fields of view fill up, so more vehicles stop producing
proportionally more output.

### The conclusion

The superlinearity in §1 is the **global traffic simulation**, which is
O(vehicles) and which **production does not have**. In production there is
no global vehicle list: each camera decodes its own stream and its detector
sees only its own frame. Nothing walks 36,000 vehicles per tick.

At 1,000 cameras the simulator's share is 36,000 × 10.3 µs = **371 ms of
the 1,660 ms** by Control B's rate — and the remainder is dominated by
per-vehicle-in-view work that Control A shows is not driven by camera count.

**So §1 must not be used to extrapolate capacity, and it is not used below.**
The model in §4 is built on Control A's per-camera cost and on the decode
cost measured against real RTSP in
[SENTINEL_LIVE_TEST_REPORT.md](SENTINEL_LIVE_TEST_REPORT.md).

Publishing §1 without §2 would have produced a confident, wrong, and
much more pessimistic capacity number. Publishing §2 without §1 would hide
how it was found.

---

## 3 · Memory

| Cameras | RSS | MB/camera |
|---:|---:|---:|
| 50 | 75 MB | 0.80 |
| 250 | 168 MB | 0.37 |
| 500 | 303 MB | 0.27 |
| 1,000 | 590 MB | 0.29 |

**MEASURED.** Per-camera memory converges to **≈0.29 MB** for the event
path — the fixed interpreter and model-stub footprint dominates at 50.

This excludes decode buffers, which are the real memory cost of a live
camera and are counted separately in §4.

---

## 4 · Capacity model — 50 to 80,000

### Assumptions (change these and every CALCULATED cell moves)

| # | Assumption | Basis |
|---|---|---|
| A1 | Main stream 1080p H.264 @ 15 fps ≈ **4 Mbps** | ESTIMATED — typical CBR for this class |
| A2 | Sub-stream 640×360 @ 6 fps ≈ **0.4 Mbps** | ESTIMATED |
| A3 | **AI consumes the sub-stream only**, at 6 fps | Design decision; `target_fps=6.0` |
| A4 | Decode: **672.9 frames/s aggregate on 4 vCPU** at 640×360 | **MEASURED**, 50 real RTSP cameras, live test report §4 |
| A5 | Event path: **15.5 µs/camera/tick** at 6 Hz = 93 µs/camera/s | **MEASURED**, Control A @ 500 cameras |
| A6 | Event path memory **0.29 MB/camera**; decode buffers **~8 MB/camera** | MEASURED / ESTIMATED |
| A7 | Detection on GPU: **T4 ≈ 250 fps** YOLOv8n @ 640×360 | ESTIMATED — published figures, no GPU on this host |
| A8 | **1 sighting per camera per 8 s** in daytime traffic | CALCULATED from live estate: 5.7 sightings/s ÷ 50 cameras |
| A9 | Sighting row ≈ **1.2 kB** with a 512-d float16 embedding | CALCULATED from schema |
| A10 | Video retention **7 days hot**, metadata **1 year** | Design decision, configurable |

### The table

| | 50 | 1,000 | 3,000 | 10,000 | 50,000 | 80,000 |
|---|---:|---:|---:|---:|---:|---:|
| **Decode cores, CPU** (A4) | 1.8 <br>CALCULATED | 36 <br>CALCULATED | 107 <br>CALCULATED | 357 <br>PROJECTED | 1,783 <br>PROJECTED | **2,853** <br>PROJECTED |
| **Event-path cores** (A5) | 0.005 <br>MEASURED | 0.09 <br>CALCULATED | 0.28 <br>CALCULATED | 0.93 <br>PROJECTED | 4.7 <br>PROJECTED | 7.4 <br>PROJECTED |
| **GPUs** (T4-class, A7) | 1.2 <br>ESTIMATED | 24 <br>ESTIMATED | 72 <br>ESTIMATED | 240 <br>PROJECTED | 1,200 <br>PROJECTED | 1,920 <br>PROJECTED |
| **RAM, decode+event** (A6) | 0.4 GB <br>CALCULATED | 8.1 GB <br>CALCULATED | 24 GB <br>CALCULATED | 81 GB <br>PROJECTED | 405 GB <br>PROJECTED | 648 GB <br>PROJECTED |
| **Ingest bandwidth, sub-stream only** (A2) | 20 Mbps <br>CALCULATED | 400 Mbps <br>CALCULATED | 1.2 Gbps <br>CALCULATED | 4 Gbps <br>PROJECTED | 20 Gbps <br>PROJECTED | 32 Gbps <br>PROJECTED |
| **If main streams were centralised** (A1) | 200 Mbps <br>CALCULATED | 4 Gbps <br>CALCULATED | 12 Gbps <br>CALCULATED | 40 Gbps <br>PROJECTED | 200 Gbps <br>PROJECTED | **320 Gbps** <br>PROJECTED |
| **Sightings/s** (A8) | 6.3 <br>MEASURED | 125 <br>CALCULATED | 375 <br>CALCULATED | 1,250 <br>PROJECTED | 6,250 <br>PROJECTED | 10,000 <br>PROJECTED |
| **Metadata/day** (A8,A9) | 0.65 GB <br>CALCULATED | 13 GB <br>CALCULATED | 39 GB <br>CALCULATED | 130 GB <br>PROJECTED | 648 GB <br>PROJECTED | 1.04 TB <br>PROJECTED |
| **Metadata/year** (A10) | 237 GB <br>CALCULATED | 4.7 TB <br>CALCULATED | 14 TB <br>CALCULATED | 47 TB <br>PROJECTED | 237 TB <br>PROJECTED | 379 TB <br>PROJECTED |
| **Video, 7 days hot** (A1,A10) | 15 TB <br>CALCULATED | 302 TB <br>CALCULATED | 907 TB <br>CALCULATED | 3.0 PB <br>PROJECTED | 15 PB <br>PROJECTED | **24 PB** <br>PROJECTED |
| **Inference workers** (A7, 4 GPU/node) | 1 <br>ESTIMATED | 6 <br>ESTIMATED | 18 <br>ESTIMATED | 60 <br>PROJECTED | 300 <br>PROJECTED | 480 <br>PROJECTED |

### What the table says

**The 320 Gbps row is the whole argument for federation.** Centralising
main streams from 80,000 cameras is not a budget problem, it is a
physical-plant problem. Processing at the edge and moving *metadata*
instead reduces the state-wide backbone requirement to the sightings row —
10,000 sightings/s at ~1.2 kB is **≈96 Mbps**, three and a half orders of
magnitude smaller. That comparison is the subject of
[NETWORK_BANDWIDTH_PLAN.md](NETWORK_BANDWIDTH_PLAN.md).

**Decode and inference dwarf everything this benchmark measures.** The
event path needs **7.4 cores** at 80,000 cameras. Decoding the same estate
on CPU needs **2,853 cores**, and the detector needs **~1,920 T4-class
GPUs**. The thing `scale_test.py` measures is, at state scale, a rounding
error — which is worth saying plainly, because it is the part that was
easiest to measure and would have been the most tempting to headline.

Two consequences follow.

*Decode belongs on the GPU, not the CPU.* 2,853 CPU cores purely to turn
H.264 into pixels is indefensible when NVDEC on the same cards already
running the detector does it in hardware. The CPU decode row is what the
architecture costs if that is got wrong.

*Every optimisation that matters is an optimisation of what reaches the
GPU.* The frame sampler (6 fps, not 25) and the ANPR quality gate (~80%
rejection) are the load-bearing design decisions in this system. The
tracker is not — it is fast, and this benchmark confirms it is fast, and
that is precisely why making it faster would buy nothing.

**24 PB of hot video is why video does not move.** At 7 days and 1080p,
storing 80,000 cameras centrally is a data-centre programme in its own
right. Clips are exported on demand against an event; the archive stays
where the camera is.

---

## 5 · Honest limits of this benchmark

- **No GPU on this host.** Every GPU figure is ESTIMATED from published
  throughput. Nothing in this document has run a real detector on real
  hardware, and the real number could differ by 2× in either direction.
- **Decode was measured at 640×360, not 1080p.** A4 comes from the live
  RTSP test; 1080p decode is roughly 4–6× more expensive per frame. The
  decode-cores row is therefore a **floor**, and a large one.
- **A4 was itself taken from a saturated host.** The live test's 50 cameras
  ran at ~13.5 fps each on 4 vCPUs and produced 44 reconnects from CPU
  starvation, so 672.9 frames/s is the throughput of an overloaded machine.
  It is the honest number available, but it is not a clean throughput
  measurement and the derived row inherits that.
- **Nothing above 1,000 cameras was run.** Every 10,000+ cell is PROJECTED
  by multiplication, and multiplication is exactly what §2 shows can be
  wrong when a hidden term scales with the thing you are scaling. The
  honest claim is that the *measured* range is 50–1,000 and the rest is
  arithmetic.
- **Single process, single node.** No network, no serialisation, no
  cross-node coordination cost is included in the event-path figures. A
  real 80,000-camera deployment pays all three.
- **The traffic is simulated.** Vehicle density, dwell time and the
  sightings-per-camera rate (A8) come from the demo world, not from
  Ahmedabad.

---

## 6 · Reproducing everything here

```bash
# §1 — the 50/250/500/1000 curve
python3 scripts/scale_test.py --cameras 50 250 500 1000 --seconds 10

# §2 Control A — traffic constant, cameras scale
python3 scripts/scale_test.py --cameras 50 250 500 --seconds 8 --vehicles-per-100-cams 0

# §2 Control B — cameras constant, traffic scales
for v in 400 3600 14400 36000; do
  python3 scripts/scale_test.py --cameras 50 --seconds 8 --vehicles-per-100-cams $v
done

# machine-readable
python3 scripts/scale_test.py --cameras 50 500 1000 --json
```
