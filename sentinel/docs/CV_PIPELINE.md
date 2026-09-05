# 03 — Computer Vision Pipeline

Goal: designate one vehicle, then follow it across ~50 heterogeneous cameras in real time, with enough precision that an operator trusts the alerts.

---

## 0. The premise that makes this work

**ANPR alone will not track a vehicle across a city estate.** Only 10–15% of a general surveillance estate is plate-readable — the rest is wide-angle, high-mounted, backlit, or looking at the wrong end of the car. If your system requires a plate read at every hop, you will get 4 sightings out of 50 cameras and the track will be a dotted line.

**ReID alone is not precise enough either.** A white Maruti Swift looks like every other white Maruti Swift. Pure appearance matching on Indian roads produces false positives at a rate that destroys operator trust within ten minutes.

So the design is **fusion with hard spatio-temporal gating**:

> **Plate = identity (high precision, low recall).
> ReID + attributes = continuity (high recall, low precision).
> Road-network reachability = the filter that makes low precision usable.**

The gating is the part most teams skip and it is worth more than any model upgrade. Details in §4.

---

## 1. Pipeline stages

```
sub-stream ─▶ [1] decode (NVDEC)
           ─▶ [2] vehicle detect        YOLOX-S / RT-DETRv2   ~4 ms  (batched)
           ─▶ [3] per-camera track      ByteTrack             ~1 ms
           ─▶ [4] quality gate          blur / size / angle   ~0.1 ms   ← drops ~80% of work
           ├─▶ [5a] ANPR   plate det → PARSeq/PP-OCRv4 → lexicon decode   ~8 ms
           ├─▶ [5b] ReID   OSNet-AIN embedding (512-d)                    ~2 ms
           └─▶ [5c] attributes  colour + type                             ~1 ms
           ─▶ [6] tracklet close-out ─▶ Kafka
                              │
                              ▼
           [7] CROSS-CAMERA MATCHER  (spatio-temporal gate → fused score → assignment)
                              │
                              ▼
           [8] global track ─▶ PostGIS LINESTRING M ─▶ alerts ─▶ operator
```

Stages 1–6 run **at the edge**, per camera group. Stages 7–8 run **centrally**. That seam is deliberate — it is what makes 80,000 cameras possible.

---

## 2. Detection and tracking

### Detector

| Model | mAP@50-95 (COCO) | T4 FP16 @640 | Licence | Verdict |
|---|---|---|---|---|
| YOLOv8n (Ultralytics) | 37.3 | 1.9 ms | **AGPL-3.0** | Fast demo, licence blocker |
| YOLO11s (Ultralytics) | 47.0 | 3.4 ms | **AGPL-3.0** | Best accuracy/speed, licence blocker |
| **YOLOX-S** | 40.5 | 3.1 ms | **Apache-2.0** | ✅ **Ship this** |
| **RT-DETRv2-R18** | 47.9 | 5.8 ms | **Apache-2.0** | ✅ NMS-free, best licence-safe accuracy |
| D-FINE-S | 48.5 | 5.2 ms | Apache-2.0 | ✅ Strong 2025 option |

**Recommendation:** prototype with YOLO11s (fastest to working), ship RT-DETRv2-R18 or YOLOX-S. Export to **TensorRT FP16**; INT8 with calibration on your own night footage gives another ~1.8× if you need it, at ~1 mAP cost.

Critical: **fine-tune on Indian road scenes.** COCO has `car/truck/bus/motorcycle` but no `auto-rickshaw`, and auto-rickshaws are a large share of Indian traffic that COCO models classify inconsistently as car/truck/motorcycle. Add classes: `car, motorcycle, auto_rickshaw, truck, bus, tractor, cycle`. A few thousand annotated frames from your own 50 cameras beats any amount of generic pretraining, because it also teaches the model your specific camera angles and IR characteristics.

### Tracker

**ByteTrack** (MIT). The insight that makes it right for this problem: it associates *low-confidence* detections in a second pass instead of discarding them, which is exactly what recovers a vehicle passing behind a bus or through IR glare. BoT-SORT adds camera-motion compensation and a ReID branch — worth it for PTZ cameras, unnecessary for the fixed cameras that dominate the estate.

Tune for traffic, not for the MOT17 benchmark defaults:
- `track_thresh=0.5`, `match_thresh=0.8`
- `track_buffer=60` frames — hold identity through ~2 s of occlusion at 30 fps; at 10 fps use 25.
- Add a **direction-consistency check**: reject associations that reverse heading between frames. Cheap, and it eliminates the ID-swap-at-crossing failure that dominates junction footage.

Output is a **tracklet**: one vehicle, one camera, entry→exit, with the best N crops selected by the quality gate.

---

## 3. ANPR for Indian plates

### 3.1 Two stages, not one

1. **Plate detection** — a tiny YOLO (YOLOX-Nano, 640→ plate bbox) run *on the vehicle crop*, not the full frame. Searching a 1280×720 frame for a 90 px plate is wasteful; searching a 256×256 vehicle crop is trivial. ~1.5 ms.
2. **Plate recognition** — **PARSeq** (Apache-2.0) fine-tuned on Indian plates, or **PP-OCRv4/v5 rec** (Apache-2.0). PARSeq's permuted-autoregressive decoding is unusually robust to the occlusion and partial blur that dominate real plate crops. ~5 ms, 36-class alphabet (A–Z, 0–9), no lowercase, no punctuation.

Datasets to bootstrap: **IDD (India Driving Dataset)**, CCPD (Chinese, useful for pretraining plate detection), plus your own — 3,000–5,000 hand-labelled Gujarat plates will outperform anything off-the-shelf. Budget one person for two days on labelling; it is the highest-ROI work in the whole project.

### 3.2 The quality gate is not optional

Run ANPR **only** when the crop can plausibly be read:

| Check | Threshold | Why |
|---|---|---|
| Plate bbox width | ≥ 90 px (≥ 110 px at night) | Below this, OCR is noise generation |
| Blur (variance of Laplacian) | ≥ 100 | Motion blur is the #1 cause of wrong reads |
| Yaw / skew angle | ≤ 35° | Rectify with a perspective warp first |
| Mean luminance | 40–230 | Rejects IR blowout and black frames |
| Track dedup | best-3 crops per tracklet | Don't OCR 40 frames of the same car |

This gate drops **~80–90%** of candidate crops and is what makes 50 cameras fit on one GPU. It also *raises* accuracy, because most wrong reads come from crops a human could not read either.

### 3.3 Lexicon-constrained decoding — the single best accuracy win

Indian plates are not free text. They follow BSR (Bharat Series) and state formats:

```
Standard :  ^[A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,3}\s?\d{1,4}$      GJ01AB1234, MH12DE1433
Bharat   :  ^\d{2}\s?BH\s?\d{4}\s?[A-Z]{1,2}$                 22BH1234AA
Military :  ^\d{2}[A-Z]\d{6}[A-Z]$
```

…and the first two characters are drawn from a **closed set of ~37 valid state/UT codes** (GJ, MH, RJ, DL, KA, …). Constrain the decoder's beam search to this grammar and you convert a 36^10 output space into something with maybe 10^7 valid strings.

**Measured effect in comparable systems: +8–15 percentage points of exact-match accuracy, for a few dozen lines of code.** No model swap gets you that.

### 3.4 Fuzzy matching, because you will never get clean reads

Confusion pairs are systematic, not random: `O↔0  I↔1  8↔B  5↔S  2↔Z  6↔G  D↔0  U↔V`.

Do **not** compare plates with `==`. Use a **confusion-weighted Levenshtein distance** where a confusable substitution costs 0.3 and a non-confusable substitution costs 1.0. Then:

- `distance ≤ 0.6` → confident match (alert)
- `0.6 < distance ≤ 1.5` → probable match (alert, flagged "probable", requires ReID or attribute corroboration)
- `> 1.5` → no match

This is implemented in `services/cv/plate_rules.py`. It is the difference between "the system found the car 6 times" and "the system found the car 31 times."

Also: **positionally weight the distance.** A mismatch in the 4-digit serial matters more than a mismatch in the state code, because the state code is grammar-constrained and therefore more reliable.

### 3.5 Expected real-world accuracy — plan for these numbers

| Condition | Exact-match | With lexicon + fuzzy |
|---|---|---|
| Dedicated ANPR lane, day | 92–97% | 96–99% |
| Dedicated ANPR lane, night + IR | 85–93% | 92–96% |
| General surveillance cam, day, plate ≥ 100 px | 55–70% | 70–82% |
| General surveillance cam, night | 20–40% | 35–55% |
| Wide-angle junction cam (plate < 60 px) | **< 10%** | < 15% |

Read the last row again. On most of a real estate, **ANPR contributes almost nothing** — which is precisely why the architecture leans on ReID and gating. Present these numbers honestly in your submission; a team that knows its own error envelope reads as far more competent than one claiming 99%.

---

## 4. Cross-camera ReID — and the gating that makes it work

### 4.1 Embedding model

| Model | Params | mAP on VeRi-776 | T4 latency | Verdict |
|---|---|---|---|---|
| **OSNet-AIN x1.0** | 2.2 M | ~80% | **1.8 ms** | ✅ real-time tier, run on every tracklet |
| ResNet50-IBN-a | 25 M | ~82% | 6 ms | Baseline |
| TransReID (ViT-B) | 86 M | ~82–84% | 18 ms | Re-rank tier |
| **CLIP-ReID (ViT-B/16)** | 86 M | **~85%** | 20 ms | ✅ re-rank tier, best accuracy |

**Two-tier design.** OSNet-AIN produces a 512-d embedding for *every* tracklet at 1.8 ms — cheap enough to run on all 50 cameras continuously. When a candidate passes the gate, CLIP-ReID re-ranks the top-K. You pay the expensive model only on the ~20 candidates per event that survived gating, not on the 500/s firehose.

Train on **VeRi-776 + VehicleID + VERI-Wild**, then fine-tune on crops harvested from your own cameras. Domain gap between benchmark datasets and a specific camera estate is large; even unlabelled fine-tuning with a self-supervised objective helps.

### 4.2 Attributes — cheap, and they carry real discriminative power

Alongside the embedding, emit: **colour** (11-class, from a small CNN on the vehicle crop — do *not* use raw RGB histograms, sodium-vapour street lighting destroys them; convert to a colour-constant space or train the classifier on night data), **type** (from the detector), and optionally **make/model** where a classifier is available.

`{white, sedan}` cuts the candidate pool by ~15×. It costs 1 ms.

### 4.3 Spatio-temporal gating — the part that actually makes this work

Given a confirmed sighting at camera A at time `t`, which cameras could plausibly see this vehicle next?

Naïve answer: all 50. Correct answer: usually 2–5.

Build a **camera adjacency graph** where the edge weight is *road-network travel time*, not straight-line distance:

1. Load the Gujarat OSM extract into **OSRM**.
2. For every camera pair within 5 km, compute the driving time `τ(A,B)` via OSRM's `/table` service. Precompute once, cache in Postgres.
3. Camera B is a candidate at time `t'` only if:

```
τ(A,B) · (v_typical / v_max)  ≤  (t' − t)  ≤  τ(A,B) · (v_typical / v_min) + dwell_margin
```

In practice: `v_max` = 1.6× the routed speed (speeding), `v_min` = 0.35× (traffic, signals), `dwell_margin` = 120 s (parking, fuel stop). Widen both bounds when `clock_confidence` is low.

**Effect: the candidate set drops by 95–98%.** A ReID model with 85% mAP that would produce unusable false-positive rates against 50 cameras produces *operationally trustworthy* results against 3 candidates. This is the highest-leverage component in the entire CV design and it is mostly a PostGIS query plus a cached OSRM matrix.

Second-order benefit: the graph makes **prediction** possible. When the target leaves camera A, pre-alert the 3 downstream cameras and raise their sampling rate. That is a genuinely impressive demo moment — the map lights up *ahead* of the vehicle.

### 4.4 The fusion score

```
S(a,b) = w_p · plate(a,b)          # 1.0 exact, 0.75 fuzzy≤0.6, 0.5 fuzzy≤1.5, 0 else
       + w_r · cosine(e_a, e_b)    # ReID, normalised to [0,1]
       + w_c · colour_match(a,b)
       + w_t · type_match(a,b)
       + w_s · st_feasibility(a,b) # 1.0 at expected travel time, decaying to 0 at gate edges

default: w_p=0.45  w_r=0.30  w_c=0.08  w_t=0.07  w_s=0.10
```

Decision bands:
- `S ≥ 0.80` → **auto-confirm**, extend the global track, fire alert
- `0.55 ≤ S < 0.80` → **probable**, show on map as a hollow marker, request operator confirmation
- `S < 0.55` → discard

Hard override: **an exact plate match inside the spatio-temporal gate is always a confirm**, regardless of ReID disagreement — appearance models fail on lighting changes far more often than a lexicon-validated plate read is wrong.

Tune the weights on a held-out set of *your own* annotated cross-camera pairs. Fifty hand-labelled ground-truth transitions is enough to set weights sensibly and, more importantly, gives you a **measured precision/recall number to put in the submission** rather than an adjective.

### 4.5 Assignment

Per matcher tick (1 s), collect open global tracks and new tracklets, build the cost matrix, apply the gate as a hard mask (`inf` cost outside the gate), and solve with **Hungarian assignment** (`scipy.optimize.linear_sum_assignment`). Global tracks with no match for > 15 minutes close out.

Do not use greedy nearest-neighbour: at a junction with three simultaneous candidate vehicles it makes exactly the wrong choice, and that error propagates through the rest of the trajectory.

---

## 5. Compute budget — 50 cameras, one T4

| Stage | Per frame | Frames/s (50 cam @ 10 fps) | GPU-ms/s |
|---|---|---|---|
| NVDEC decode | 0.8 ms | 500 | 400 |
| Detection (batch 16) | 0.25 ms/img | 500 | 125 |
| ByteTrack (CPU) | — | 500 | 0 |
| ReID (batch 32, ~3 veh/frame, gated) | 0.06 ms/veh | ~400 veh/s | 24 |
| ANPR (post-gate, ~8% pass) | 8 ms | ~30/s | 240 |
| **Total** | | | **~790 ms/s ≈ 79% of one T4** |

It fits, with the quality gate doing the heavy lifting. Without the gate, ANPR alone would need 4,000 ms/s — four GPUs. **The gate is worth three GPUs.**

Sample at 10 fps for AI, not the native 25/30. A vehicle at 60 km/h moves 1.7 m per 100 ms; 10 fps is ample for tracking and it cuts the whole budget by 3×.

---

## 6. Honest limitations to state in the submission

Naming these makes the work more credible, not less. Every one of them is real and every evaluator with domain experience knows it.

1. **Night ANPR on general cameras is weak** (35–55%). Mitigation: ReID + attributes carry the track; flag night sightings as lower confidence.
2. **Similar-vehicle confusion.** White hatchbacks in Ahmedabad are near-indistinguishable at 720p. Mitigation: gating plus operator confirmation on the `probable` band.
3. **Coverage gaps.** 50 cameras do not tile a city; tracks will have holes. Mitigation: the road-network graph lets you show *predicted* corridors between sightings as a distinct visual style — never draw an inferred segment as if it were observed.
4. **Plate obscuration / fake plates.** No CV system solves this. ReID is the only recourse, and it should be stated as a known limit.
5. **Domain shift.** Models trained on your 50 cameras degrade on camera 51. Mitigation: a continuous-labelling loop where operator confirmations become training data — worth describing as the Phase-2 plan.
6. **Clock skew on legacy DVRs** can invert apparent travel direction. Mitigation: NTP everywhere, `clock_confidence` on every sighting, widened gates when it is low.
