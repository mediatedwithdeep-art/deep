# Benchmarks

Every number here is produced by `make benchmark` (`scripts/benchmark.py`).
If this page and the script disagree, **the script is right and this page is
stale** — re-run it and correct the page.

```
Measured on: 4 vCPU, 16 GB RAM, no GPU, Python 3.11
Backend:     simulation (no model weights)
Command:     python scripts/benchmark.py
```

---

## What "simulation backend" means, and why these numbers are meaningful

The simulation backend is not a stub returning canned values. It converts
known ground truth into **realistically noisy observations**, with failure
modes calibrated against published model behaviour:

- detector recall degrades with apparent size and occlusion, and it only
  confuses class pairs a real detector confuses (car↔auto-rickshaw, never
  car↔bus);
- ANPR read rate is driven by plate pixel width, blur and illumination, and
  degrades non-uniformly at night depending on whether the installation has
  IR aimed at the plate;
- ReID embeddings reproduce OSNet-AIN's same-ID and different-ID
  distributions on VeRi-776 **including the overlap between them**.

Because ground truth is known, accuracy is *measured* rather than asserted.
What these numbers do **not** tell you is how a specific real model performs
on a specific real camera — that needs weights, a GPU and your own footage.
The section [Real models](#real-models-what-changes) says what changes.

---

## 1. Detection

Recall against known ground truth, 600 frames per condition.

| Condition | Recall |
|---|---|
| Clear, 140×95 px | **95.2%** |
| Small, 60×42 px | 73.5% |
| Tiny, 30×22 px | 64.3% |
| Occluded 40% | 70.0% |
| Occluded 70% | 49.8% |

The gradient is the point. A detector that reported 95% on a 30×22 px box
would be lying, and every downstream estimate built on it would be wrong.

---

## 2. Tracking

ByteTrack's second association pass over low-confidence detections is what
holds a track through occlusion instead of splitting it into two vehicles.

| Occlusion length | Outcome |
|---|---|
| 2 low-confidence frames | track held |
| 4 low-confidence frames | track held |
| 8 low-confidence frames | track held |

**Throughput: 0.374 ms per frame** with 12 simultaneous objects, on CPU.
At 50 cameras × 6 fps = 300 frames/s, tracking costs ~11% of one core.
Tracking is never the bottleneck.

---

## 3. ANPR

480 reads per condition. "End-to-end" is the fraction of *all* attempts that
produced the exactly correct plate — the number that matters operationally.

| Condition | Read rate | Exact, of those read | **End-to-end** |
|---|---|---|---|
| Dedicated ANPR lane, day | 99% | 93% | **92.5%** |
| Dedicated ANPR lane, night + IR | 93% | 85% | **79.0%** |
| General surveillance, day | 84% | 90% | **75.6%** |
| General surveillance, night | 44% | 85% | **37.9%** |
| Wide-angle junction, 72 px plate | 39% | 86% | **33.1%** |
| Wide-angle junction, 58 px plate | 15% | 77% | **11.2%** |

Two things to read out of this table:

**Night is not a uniform penalty.** A dedicated lane has IR aimed at a
retro-reflective plate and loses ~13 points after dark. A wide-angle camera
has no useful illumination at plate distance and loses ~38. Modelling night
as a single multiplier is the most common error in ANPR estimates.

**Below ~55 px of plate width, nothing works.** That is not a tuning
threshold, it is optics. This is why 74% of the demo estate reports a 0%
read rate and why the design leans on ReID.

### Fuzzy plate matching

OCR confusions are systematic (`O/0`, `I/1`, `8/B`, `5/S`, `2/Z`, `6/G`).

| | |
|---|---|
| Single-confusion reads recovered by the fuzzy matcher | **6 / 6** |
| Recovered by exact string comparison | 0 / 6 |
| False matches against 8 unrelated plates | **0 / 8** |

Exact comparison would have discarded every one of those true matches.

---

## 4. Re-identification

800 pairs per condition, 512-d embeddings.

| Pair type | Cosine similarity |
|---|---|
| Same vehicle, different view | **0.721 ± 0.112** |
| Different vehicle, same type + colour | 0.361 ± 0.078 |
| Different vehicle, different type + colour | 0.019 ± 0.076 |

**Distribution overlap: 66 of 800** hard negatives score above the weakest
true match. That overlap is deliberate and load-bearing: real vehicle ReID
overlaps, and a simulator with clean separation would make cross-camera
matching look trivial while hiding every false-positive mode.

| Raw cosine threshold | Recall | False positive |
|---|---|---|
| 0.55 | 95.5% | 4.2% |
| 0.62 | 80.5% | 0.9% |
| 0.70 | 53.6% | 0.0% |

**Read the false-positive column against 49 cameras.** At 0.62, a 0.9% rate
across every camera in the estate is an unusable number of wrong matches per
hour. Against the 3.3 candidates the gate leaves, it is fine. That is the
whole argument for the gate, in one table.

**Comparison throughput: 14.7 µs per pair** (vectorised). Before
vectorisation it was 96 µs, and profiling showed cosine at 60% of matcher
runtime.

---

## 5. Spatio-temporal gate

Cameras reachable by road from a given camera, averaged over all 50.

| Arrival window | Mean candidates (of 49) | Comparisons avoided |
|---|---|---|
| 60 s | 0.1 | 99.8% |
| 180 s | 1.2 | 97.6% |
| **300 s** | **3.3** | **93.3%** |
| 900 s | 19.9 | 59.4% |
| *no gate* | *49* | *0%* |

### The same reduction, priced in pairs and milliseconds

Candidate cameras are a property of the road graph. What the running system
costs is comparisons and time, which is what PART 17 asks for. The per-pair
cost is **measured** against the real scorer; the pair counts follow from
the 180 s reachability above, over one batch of 50 sightings against 400
live vehicles.

| | Without the gate | With the gate |
|---|---:|---:|
| Pairs scored | 20,000 | **473** |
| Scoring time | 2,240 ms | **53 ms** |

Measured cost per scored pair: **112 µs** (`fusion.score_pair`, 4,000
repetitions). The pair counts are exact; the milliseconds are that measured
cost multiplied by them, and are labelled as such in the benchmark output.

The matcher carries the same instrumentation in production —
`MatcherStats.scored_pairs` against `ungated_pairs`, with the gate and the
scorer timed separately, because a slow gate is a database problem and a
slow scorer is an embedding problem, and one combined number optimises the
wrong one.

This is the highest-leverage component in the system and it involves no
model at all — a PostGIS query against a cached routing matrix.

Its second-order effect matters too: the same graph tells the operator which
cameras to watch *next*, so the map highlights ahead of the vehicle.

---

## 6. Estate throughput

50 cameras, 1,800 simulated vehicles, 6 Hz, single process, no GPU.

| | |
|---|---|
| Per tick, whole estate | **14.2 – 22.0 ms** (three hosts) |
| Tick budget used at 6 Hz | **9 – 13%** |
| Sightings produced | 10 / s |
| Plate reads | 13.6% of sightings |
| Cameras producing sightings | 26 / 50 in a 30 s window |
| **Implied capacity, single core** | **~380 – 585 cameras** |


**This figure is host-dependent and has been measured three times on three
different containers: 14.2, 17.1 and 22.0 ms/tick, implying ~585, ~488 and
~379 cameras per core.** The same code, the same seed, the same estate —
the variation is shared-CPU contention, not a change in the system. It is
quoted as a range rather than corrected to the latest value, because
correcting it each time produced three confident numbers that were each
wrong within a month.

Use the **ratio** (≈10–13% of one core for 50 cameras) rather than the
absolute figure, and treat ~380 as the conservative end of the capacity
implication.

Treat the top of that range as an upper bound for the *simulation* path.
With real decode and inference the binding constraint is NVDEC and GPU,
not this loop:
roughly **30–45 sub-streams per T4-class GPU** (see [ARCHITECTURE.md](ARCHITECTURE.md#15-latency-budget)).

### End-to-end pipeline, measured against PostgreSQL

From a 9-simulated-minute run of ingestion → matcher → alerts:

| | |
|---|---|
| Sightings processed | 1,733 |
| Cross-camera links by plate | 62 |
| Cross-camera links by appearance (probable) | 131 |
| Pairs actually scored | 344 |
| Pairs a naive matcher would have scored | ~500,000 |
| Mean match time per batch | 49 ms |
| Mean database write per batch | 75 ms |

The 344-versus-500,000 line is the gate used as an *index* rather than as a
post-hoc filter.

---

## 7. Database

| | |
|---|---|
| Migrations applied | 6, in 995 ms total |
| Partition pruning | confirmed (`Subplans Removed: 1`) |
| Partitions created ahead | 33 across 7 partitioned tables |
| Retention by `DROP PARTITION` | instant, no bloat |
| FOV polygon accuracy | within 5% of π·r²·(fov/360) |
| Gate query | 1.2 candidates within a 3-minute window, of 49 |

---

## Real models — what changes

Swapping `AI_BACKEND=simulation` for `onnx` changes the detector, OCR and
ReID implementations. Nothing else in the pipeline changes. Expect:

| Stage | Simulation | Real, T4 FP16 |
|---|---|---|
| Detection | 0.02 ms | 3–6 ms (YOLOX-S / RT-DETR-R18, batched) |
| Plate detect + OCR | ~0 | 8–10 ms per gated crop |
| ReID embedding | ~0 | 1.8 ms (OSNet-AIN), batched |
| Decode | n/a | 0.8 ms per 720p frame (NVDEC) |

The published accuracy envelope for real models on Indian plates is in
[CV_PIPELINE.md §3.5](CV_PIPELINE.md). The simulation is calibrated to it,
which is why the two tables agree — but a real deployment must re-measure on
its own footage. A model fine-tuned on your own cameras will beat any of
these numbers; a model that has never seen your camera angles will not.

---

## Reproducing

```bash
make benchmark                              # all suites
python scripts/benchmark.py --suite gate    # one suite
python scripts/benchmark.py --json          # machine-readable
```

The suite is deterministic: fixed seeds throughout, so the same machine
gives the same numbers and a regression is visible rather than dismissed as
noise.
