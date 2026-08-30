"""Tests for the AI pipeline: tracker, quality gate, ANPR, ReID, end-to-end.

These run without a GPU, without model weights and without a database.
"""
from __future__ import annotations

import statistics as st
from datetime import datetime, timedelta, timezone

import pytest

from sentinel_core.domain import BoundingBox, Detection, VehicleType
from sentinel_ai.tracker import ByteTracker
from sentinel_ai.quality import assess, estimate_plate_width_px
from sentinel_ai.attributes import classify_rgb, colour_similarity
from sentinel_ai.anpr import create_recognizer
from sentinel_ai.reid import create_extractor, cosine
from sentinel_ai.detector import SceneObject
from sentinel_ai.backends.simulation import SimulationDetector
from sentinel_ai.pipeline import CameraPipeline, CameraConfig

T0 = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)


def det(x, y, conf=0.9, w=100, h=70, vt=VehicleType.CAR, ts=None):
    return Detection(camera_id="CAM-1", track_id="", timestamp=ts or T0,
                     vehicle_type=vt, confidence=conf,
                     bbox=BoundingBox(x=x, y=y, w=w, h=h))


# ── tracker ──────────────────────────────────────────────────────────

def test_tracker_maintains_one_identity_across_frames():
    tr = ByteTracker("CAM-1")
    for f in range(12):
        conf = tr.update([det(50 + f * 18, 100, ts=T0 + timedelta(seconds=f * 0.1))],
                         T0 + timedelta(seconds=f * 0.1))
    assert len(conf) == 1
    assert conf[0].hits == 12


def test_tracker_recovers_through_low_confidence_frames():
    """The ByteTrack move: associate low-confidence detections in a second
    pass rather than discarding them. This is what holds a track through a
    vehicle passing behind a bus instead of splitting it in two."""
    tr = ByteTracker("CAM-1")
    for f in range(6):
        tr.update([det(50 + f * 18, 100, 0.9, ts=T0 + timedelta(seconds=f * 0.1))],
                  T0 + timedelta(seconds=f * 0.1))
    # Four frames of heavy occlusion: detections survive, but weakly.
    for f in range(6, 10):
        tr.update([det(50 + f * 18, 100, 0.25, ts=T0 + timedelta(seconds=f * 0.1))],
                  T0 + timedelta(seconds=f * 0.1))
    active = tr.update([det(50 + 10 * 18, 100, 0.9, ts=T0 + timedelta(seconds=1.0))],
                       T0 + timedelta(seconds=1.0))
    assert len(active) == 1, "occlusion split the track"
    assert active[0].hits == 11, "low-confidence frames were discarded"


def test_tracker_computes_direction_of_travel():
    tr = ByteTracker("CAM-1")
    for f in range(8):
        conf = tr.update([det(50 + f * 25, 100, ts=T0 + timedelta(seconds=f * 0.1))],
                         T0 + timedelta(seconds=f * 0.1))
    assert conf[0].heading_deg == pytest.approx(90.0, abs=1.0)   # 90 = rightwards


def test_tracker_rejects_a_direction_reversing_association():
    """Two vehicles crossing at a junction produce overlapping boxes. IoU
    alone happily swaps their identities; the direction check stops it."""
    tr = ByteTracker("CAM-1", direction_tolerance_deg=90)
    for f in range(8):
        tr.update([det(50 + f * 30, 100, ts=T0 + timedelta(seconds=f * 0.1))],
                  T0 + timedelta(seconds=f * 0.1))
    before = tr.tracks[0].hits
    # A box in nearly the same place but implying reversed travel.
    tr.update([det(50 + 6 * 30, 100, ts=T0 + timedelta(seconds=0.9))],
              T0 + timedelta(seconds=0.9))
    assert tr.tracks[0].hits == before, "accepted a reversed-direction match"


def test_short_blips_never_become_sightings():
    """A two-frame detection is detector noise, not a vehicle. Publishing it
    would pollute the map and the cross-camera matcher."""
    tr = ByteTracker("CAM-1", min_hits=3, max_age=2)
    tr.update([det(10, 10)], T0)
    tr.update([det(12, 10)], T0 + timedelta(seconds=0.1))
    for f in range(6):
        tr.update([], T0 + timedelta(seconds=1 + f))
    assert tr.collect_finished() == []


def test_flush_closes_open_tracks():
    """When a stream drops, vehicles in flight must still produce sightings
    rather than silently disappearing."""
    tr = ByteTracker("CAM-1")
    for f in range(6):
        tr.update([det(50 + f * 18, 100, ts=T0 + timedelta(seconds=f * 0.1))],
                  T0 + timedelta(seconds=f * 0.1))
    assert len(tr.flush()) == 1
    assert tr.tracks == []


# ── quality gate ─────────────────────────────────────────────────────

def test_gate_allows_anpr_on_a_large_sharp_daylight_crop():
    v = assess(BoundingBox(x=0, y=0, w=450, h=300), 0.9,
               blur_variance=300, mean_luma=130)
    assert v.run_anpr and v.run_reid


@pytest.mark.parametrize("kwargs,reason_fragment", [
    ({"blur_variance": 40, "mean_luma": 130}, "blur"),
    ({"blur_variance": 300, "mean_luma": 250}, "luma"),
    ({"blur_variance": 300, "mean_luma": 130, "skew_deg": 55}, "skew"),
    ({"blur_variance": 300, "mean_luma": 130, "crops_taken": 3}, "already_read"),
])
def test_gate_refuses_anpr_on_unreadable_crops(kwargs, reason_fragment):
    v = assess(BoundingBox(x=0, y=0, w=450, h=300), 0.9, **kwargs)
    assert not v.run_anpr
    assert any(reason_fragment in r for r in v.reasons)


def test_small_vehicle_keeps_reid_but_loses_anpr():
    """The central design point: ~85% of a real estate cannot read a plate
    but can still contribute through appearance. A gate that killed both
    would blind the system on most of its cameras."""
    v = assess(BoundingBox(x=0, y=0, w=120, h=90), 0.9,
               blur_variance=300, mean_luma=130)
    assert not v.run_anpr
    assert v.run_reid


def test_non_anpr_camera_never_attempts_anpr():
    v = assess(BoundingBox(x=0, y=0, w=600, h=400), 0.95,
               blur_variance=400, mean_luma=130, camera_anpr_capable=False)
    assert not v.run_anpr and v.run_reid


def test_plate_width_estimate_is_proportional_to_the_vehicle():
    assert estimate_plate_width_px(BoundingBox(x=0, y=0, w=450, h=300)) == pytest.approx(100, abs=1)


# ── attributes ───────────────────────────────────────────────────────

def test_colour_classification_and_night_confidence_penalty():
    day, day_conf = classify_rgb(228, 226, 230)
    night, night_conf = classify_rgb(228, 226, 230, is_night=True)
    assert day == night == "white"
    assert night_conf < day_conf, "night crops must not report day confidence"


def test_ambiguous_colour_reports_low_confidence():
    _, conf = classify_rgb(150, 152, 156)      # sits between silver and grey
    assert conf < 0.5


def test_unknown_colour_is_no_information_not_a_mismatch():
    assert colour_similarity(None, "red") == 0.5
    assert colour_similarity("white", "red") == 0.0
    assert colour_similarity("white", "silver") > 0.5    # a real lighting confusion


# ── ANPR ─────────────────────────────────────────────────────────────

PLATES = ["GJ01AB1234", "MH12DE1433", "GJ27XY0987", "DL8CAF5030", "GJ05KL7788",
          "RJ14QR2211", "GJ18MN4455", "KA03PQ9900", "GJ21ST3344", "TN09UV5566"]


def _anpr_rate(px, night, blur, n=30):
    r = create_recognizer("simulation")
    reads = exact = 0
    for p in PLATES:
        for v in range(n):
            res = r.read(ground_truth=p, plate_width_px=px + v * 0.4,
                         is_night=night, blur_variance=blur + v * 2)
            if res:
                reads += 1
                exact += res.normalized_plate == p
    total = len(PLATES) * n
    return reads / total, exact / total


def test_anpr_accuracy_matches_the_documented_envelope():
    """The published accuracy table is a claim we have to be able to defend.

    These bounds are deliberately the ones in docs/CV_PIPELINE.md. If the
    simulator drifts outside them, either the docs or the model is wrong,
    and a hackathon submission that quotes numbers it cannot reproduce is
    worse than one that quotes none.
    """
    _, lane_day = _anpr_rate(165, False, 400)
    _, lane_night = _anpr_rate(155, True, 300)
    _, surv_day = _anpr_rate(108, False, 240)
    _, surv_night = _anpr_rate(102, True, 150)
    assert 0.85 <= lane_day <= 0.99,   f"ANPR lane day {lane_day:.2f}"
    assert 0.72 <= lane_night <= 0.95, f"ANPR lane night {lane_night:.2f}"
    assert 0.60 <= surv_day <= 0.85,   f"surveillance day {surv_day:.2f}"
    assert 0.20 <= surv_night <= 0.50, f"surveillance night {surv_night:.2f}"


def test_night_degrades_a_surveillance_camera_far_more_than_an_anpr_lane():
    """A dedicated ANPR lane has IR aimed at a retro-reflective plate and
    barely degrades. A wide-angle surveillance camera has no useful
    illumination at plate distance and falls off a cliff. Modelling night
    as a uniform penalty is the most common error in ANPR estimates."""
    _, lane_day = _anpr_rate(165, False, 400)
    _, lane_night = _anpr_rate(165, True, 400)
    _, surv_day = _anpr_rate(105, False, 240)
    _, surv_night = _anpr_rate(105, True, 240)
    lane_drop = (lane_day - lane_night) / lane_day
    surv_drop = (surv_day - surv_night) / surv_day
    assert surv_drop > lane_drop * 2, f"lane {lane_drop:.2f} vs surveillance {surv_drop:.2f}"


def test_plates_below_the_pixel_floor_are_never_read():
    r = create_recognizer("simulation")
    assert all(r.read(ground_truth="GJ01AB1234", plate_width_px=45,
                      is_night=False, blur_variance=400) is None for _ in range(50))


def test_reads_are_deterministic_so_demos_are_reproducible():
    a = create_recognizer("simulation").read(
        ground_truth="GJ01AB1234", plate_width_px=120, is_night=False, blur_variance=250)
    b = create_recognizer("simulation").read(
        ground_truth="GJ01AB1234", plate_width_px=120, is_night=False, blur_variance=250)
    assert (a is None) == (b is None)
    if a:
        assert a.raw_plate == b.raw_plate and a.confidence == b.confidence


def test_invalid_format_reads_are_penalised_in_confidence():
    from sentinel_ai.anpr.ocr import PlateRecognizer
    good = PlateRecognizer.finalize("GJ01AB1234", 0.85)
    bad = PlateRecognizer.finalize("QQ99ZZ0000", 0.85)
    assert good.valid_format and not bad.valid_format
    assert good.confidence > bad.confidence


# ── ReID ─────────────────────────────────────────────────────────────

def _emb(e, vid, t="car", c="white", q=1.0):
    return e.extract(identity=vid, vehicle_type=t, colour=c, view_quality=q)


def test_reid_separates_same_vehicle_from_different_vehicle():
    e = create_extractor("simulation")
    same = [cosine(_emb(e, f"V{i}", q=0.9), _emb(e, f"V{i}", q=0.6)) for i in range(200)]
    diff = [cosine(_emb(e, f"V{i}"), _emb(e, f"V{i+9000}")) for i in range(200)]
    assert st.mean(same) > 0.70
    assert st.mean(diff) < 0.50
    assert st.mean(same) - st.mean(diff) > 0.30


def test_reid_distributions_overlap_like_real_models_do():
    """Zero overlap would mean the simulator has made cross-camera matching
    trivially easy, hiding exactly the false-positive modes the
    spatio-temporal gate exists to suppress. Overlap is the point."""
    e = create_extractor("simulation")
    same = [cosine(_emb(e, f"V{i}", q=0.55), _emb(e, f"V{i}", q=0.95)) for i in range(400)]
    hard = [cosine(_emb(e, f"V{i}"), _emb(e, f"V{i+9000}")) for i in range(400)]
    assert st.pstdev(same) > 0.03, "same-ID distribution is unrealistically tight"
    assert max(hard) > min(same), "no overlap: the simulator is too easy"


def test_different_type_and_colour_are_far_more_separable():
    e = create_extractor("simulation")
    hard = st.mean([cosine(_emb(e, f"V{i}", "car", "white"),
                           _emb(e, f"V{i+9000}", "car", "white")) for i in range(150)])
    easy = st.mean([cosine(_emb(e, f"V{i}", "car", "white"),
                           _emb(e, f"V{i+9000}", "truck", "red")) for i in range(150)])
    assert easy < hard, "type and colour must carry discriminative signal"


def test_embeddings_are_normalised_and_correctly_sized():
    e = create_extractor("simulation")
    v = _emb(e, "V1")
    assert len(v) == 512
    assert sum(x * x for x in v) == pytest.approx(1.0, abs=1e-6)


def test_aggregation_is_more_stable_than_any_single_crop():
    """A track's best crops averaged should sit closer to the vehicle's
    canonical embedding than a single poor crop does."""
    e = create_extractor("simulation")
    poor = _emb(e, "V1", q=0.3)
    crops = [_emb(e, "V1", q=q) for q in (0.9, 0.85, 0.8)]
    agg = e.aggregate(crops, [0.9, 0.85, 0.8])
    ref = _emb(e, "V1", q=1.0)
    assert cosine(agg, ref) > cosine(poor, ref)


# ── detector ─────────────────────────────────────────────────────────

def _recall(obj, frames=300):
    d = SimulationDetector()
    for f in range(frames):
        d.detect(camera_id="C", timestamp=T0, scene=[obj], frame_seq=f)
    return (d.stats.objects_in - d.stats.missed) / d.stats.objects_in


def test_detector_recall_degrades_with_size_and_occlusion():
    clear = _recall(SceneObject(identity="A", vehicle_type=VehicleType.CAR, colour="white",
                                plate="GJ01AB1234", bbox=BoundingBox(x=10, y=10, w=140, h=95)))
    tiny = _recall(SceneObject(identity="B", vehicle_type=VehicleType.CAR, colour="white",
                               plate="GJ01AB1234", bbox=BoundingBox(x=10, y=10, w=30, h=22)))
    occluded = _recall(SceneObject(identity="C", vehicle_type=VehicleType.CAR, colour="white",
                                   plate="GJ01AB1234", bbox=BoundingBox(x=10, y=10, w=140, h=95),
                                   occlusion=0.7))
    assert clear > 0.90
    assert tiny < clear and occluded < clear


def test_detector_only_confuses_classes_a_real_model_confuses():
    d = SimulationDetector()
    seen = set()
    obj = SceneObject(identity="A", vehicle_type=VehicleType.CAR, colour="white",
                      plate="GJ01AB1234", bbox=BoundingBox(x=10, y=10, w=140, h=95))
    for f in range(2000):
        for det_ in d.detect(camera_id="C", timestamp=T0, scene=[obj], frame_seq=f):
            seen.add(det_.vehicle_type)
    # A car may be called an auto-rickshaw or a truck. Never a bus.
    assert VehicleType.BUS not in seen
    assert seen <= {VehicleType.CAR, VehicleType.AUTO_RICKSHAW, VehicleType.TRUCK}


# ── end-to-end ───────────────────────────────────────────────────────

def _drive_past(anpr_capable=True, target_fps=6.0, plate="GJ01AB1234",
                identity="GT-001", colour="white", box=(520, 340)):
    cfg = CameraConfig(camera_id="AHM-SAT-001", latitude=23.027, longitude=72.512,
                       anpr_capable=anpr_capable, target_fps=target_fps)
    p = CameraPipeline(cfg, SimulationDetector())
    out = []
    w, h = box
    for f in range(75):
        x = 60 + f * 14
        scene = [SceneObject(identity=identity, vehicle_type=VehicleType.CAR, colour=colour,
                             plate=plate, bbox=BoundingBox(x=x, y=300, w=w, h=h),
                             latitude=23.027, longitude=72.512,
                             speed_kmph=44, heading_deg=47)] if x < 900 else []
        _, s = p.process(T0 + timedelta(seconds=f / 15.0), scene=scene)
        out += s
    for f in range(75, 140):
        _, s = p.process(T0 + timedelta(seconds=f / 15.0), scene=[])
        out += s
    return p, out


def test_pipeline_produces_one_sighting_per_vehicle_pass():
    p, sightings = _drive_past()
    assert len(sightings) == 1
    s = sightings[0]
    assert s.camera_id == "AHM-SAT-001"
    assert s.vehicle_type == VehicleType.CAR
    assert s.plate is not None and s.plate.normalized_plate == "GJ01AB1234"
    assert s.embedding is not None and len(s.embedding) == 512
    assert s.detection_count > 5
    assert s.dwell_seconds > 0


def test_frame_sampling_reduces_inference_load():
    """AI runs at 6 fps regardless of the stream's 15 fps. A vehicle at
    60 km/h moves 1.7 m per 100 ms, so this loses nothing and costs a third."""
    p, _ = _drive_past(target_fps=6.0)
    ratio = p.stats.frames_processed / p.stats.frames_in
    assert 0.30 <= ratio <= 0.45
    assert p.stats.frames_skipped > 0


def test_quality_gate_removes_most_anpr_work():
    """The gate is what makes 50 cameras fit on one GPU. If its efficiency
    collapses, the compute budget in docs/BENCHMARKS.md stops holding."""
    p, _ = _drive_past()
    assert p.stats.anpr_gate_efficiency > 0.60
    assert p.stats.anpr_attempts <= 3, "OCR ran on more than the best 3 crops"


def test_non_anpr_camera_still_yields_a_usable_sighting():
    """The 85% case: no plate, but colour, type and an embedding, which is
    what the cross-camera matcher actually needs to keep a track alive."""
    p, sightings = _drive_past(anpr_capable=False)
    assert len(sightings) == 1
    s = sightings[0]
    assert s.plate is None
    assert s.embedding is not None
    assert s.vehicle_color is not None
    assert p.stats.anpr_attempts == 0


def test_two_vehicles_produce_two_distinguishable_sightings():
    a, sa = _drive_past(identity="GT-A", plate="GJ01AB1234", colour="white")
    b, sb = _drive_past(identity="GT-B", plate="MH12DE1433", colour="red")
    assert sa[0].plate.normalized_plate != sb[0].plate.normalized_plate
    assert cosine(sa[0].embedding, sb[0].embedding) < 0.6


def test_health_snapshot_reports_operational_metrics(): 
    p, _ = _drive_past()
    snap = p.health_snapshot()
    for key in ("frames_in", "frames_processed", "sampling_ratio", "detections",
                "sightings", "anpr_reads", "anpr_gate_efficiency",
                "inference_ms", "open_tracks"):
        assert key in snap
    assert snap["open_tracks"] == 0, "tracks left open after the vehicle departed"
