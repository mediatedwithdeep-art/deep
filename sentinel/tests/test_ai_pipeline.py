"""Tests for the AI pipeline: tracker, quality gate, ANPR, ReID, end-to-end.

These run without a GPU, without model weights and without a database.
"""
from __future__ import annotations

import statistics as st
from datetime import datetime, timedelta, timezone

import pytest

from sentinel_core.domain import BoundingBox, Detection, VehicleType
from sentinel_ai.tracker import ByteTracker, Track
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
    tr = ByteTracker("CAM-1", min_hits=3, max_age_s=2.0)
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


# ── PART 6 · PTS time model in the tracker ───────────────────────────
#
# The brief's own example: frames at 0, 40, 120, 165 ms. Nothing in this
# section may depend on frame COUNT, because those four frames span three
# different intervals.

IRREGULAR_MS = [0, 40, 120, 165, 205, 330, 370, 410, 530, 570]


def _at(ms: float) -> datetime:
    return T0 + timedelta(milliseconds=ms)


def test_irregular_frame_intervals_are_preserved_end_to_end():
    """A vehicle moving at a constant real speed through irregular frame
    intervals must come out with a constant velocity estimate.

    If timing came from frame count, the 40 ms gap and the 125 ms gap would
    be treated as equal and the speed would swing by a factor of three.
    """
    tr = ByteTracker("CAM-1")
    px_per_s = 200.0
    for ms in IRREGULAR_MS:
        x = 50 + px_per_s * (ms / 1000.0)
        tr.update([det(int(x), 100, ts=_at(ms))], _at(ms))

    assert len(tr.tracks) == 1, "irregular intervals split the track"
    t = tr.tracks[0]
    vx, _ = t.velocity
    assert vx == pytest.approx(px_per_s, rel=0.15), (
        f"velocity {vx:.1f} px/s from a {px_per_s:.0f} px/s source -- "
        "this is what a per-frame velocity produces on a variable-rate stream")

    # And the track's own span is the real one, not a frame count.
    assert t.duration_s == pytest.approx(0.570, abs=1e-6)


def test_track_age_is_seconds_so_slow_and_fast_cameras_agree():
    """The defect this replaces: `max_age` in frames meant 1.0 s on a 25 fps
    camera and 4.2 s on a 6 fps one. Same estate, same vehicle, two
    different retirement policies decided by frame rate."""
    def run(fps: float) -> float:
        tr = ByteTracker(f"CAM-{fps}", min_hits=2, max_age_s=1.0)
        step = 1.0 / fps
        for f in range(6):                       # establish a track
            tr.update([det(50 + f * 10, 100, ts=_at(f * step * 1000))],
                      _at(f * step * 1000))
        # Now feed empty frames until it is retired, and report WHEN.
        t_last = 5 * step
        for f in range(1, 200):
            now = t_last + f * step
            tr.update([], _at(now * 1000))
            if tr.tracks and tr.tracks[0].state == "lost":
                return now - t_last
        raise AssertionError(f"never retired at {fps} fps")

    slow, fast = run(6.0), run(25.0)
    # Both must retire after ~1 s of real absence, within one frame interval
    # of the slower camera.
    assert slow == pytest.approx(1.0, abs=1.0 / 6.0), f"6 fps retired at {slow:.2f}s"
    assert fast == pytest.approx(1.0, abs=1.0 / 6.0), f"25 fps retired at {fast:.2f}s"
    assert abs(slow - fast) < 1.0 / 6.0 + 1e-9, (
        f"6 fps retired at {slow:.2f}s but 25 fps at {fast:.2f}s -- "
        "ageing is still frame-based")


def test_prediction_scales_with_the_real_gap():
    """A 165 ms gap must displace the predicted box four times as far as a
    40 ms gap. A per-frame velocity displaces it equally, which is how a
    fast vehicle gets lost after a stutter."""
    t = Track(track_id="T", bbox=BoundingBox(x=100, y=100, w=40, h=40),
              vehicle_type=VehicleType.CAR, confidence=0.9,
              first_seen=T0, last_seen=T0)
    t.velocity = (200.0, 0.0)                     # px/s
    near = t.predict(0.040).x - t.bbox.x
    far = t.predict(0.165).x - t.bbox.x
    assert near == pytest.approx(8, abs=1)
    assert far == pytest.approx(33, abs=1)
    assert far > near * 3.5, "prediction ignored the interval"


def test_a_long_stall_does_not_extrapolate_the_motion_model():
    """A stream that stalls for two minutes and resumes must not fling the
    predicted box across the frame; the track should age out instead."""
    tr = ByteTracker("CAM-1", max_age_s=2.5, max_gap_s=10.0)
    for f in range(6):
        tr.update([det(50 + f * 20, 100, ts=_at(f * 100))], _at(f * 100))
    t = tr.tracks[0]
    before = t.bbox.x
    tr.update([], _at(120_000))                   # two-minute stall
    assert t.age_s == pytest.approx(119.5, abs=0.1), "gap was not aged in real time"
    assert t.state == "lost", "a two-minute absence did not retire the track"
    assert t.bbox.x == before, "the box moved on a frame with no detection"


# ── PART 10 · scene discontinuity resets tracker state ───────────────

def test_a_discontinuity_ends_tracks_instead_of_bridging_them():
    """The failure this prevents: one track whose first_seen is before a
    loop point and whose last_seen is after it describes a vehicle in two
    places with no time between them. The matcher consumes exactly those
    two numbers."""
    tr = ByteTracker("CAM-1", min_hits=3)
    for f in range(6):
        tr.update([det(50 + f * 18, 100, ts=_at(f * 100))], _at(f * 100))
    assert len(tr.tracks) == 1
    open_track = tr.tracks[0]

    published = tr.reset_for_discontinuity()
    assert published == [open_track], "the in-flight vehicle was silently dropped"
    assert tr.tracks == [], "a track survived the discontinuity"

    # After the cut, the same-looking vehicle must be a NEW identity.
    tr.update([det(50, 100, ts=_at(0))], _at(0))
    assert len(tr.tracks) == 1
    assert tr.tracks[0].track_id != open_track.track_id, (
        "the tracker bridged across the discontinuity and fabricated a journey")


def test_a_discontinuity_restarts_the_clock_without_a_negative_age():
    """PTS goes backwards at a loop point. Ageing a track by a negative
    interval would make it younger and it would never retire."""
    tr = ByteTracker("CAM-1", min_hits=2, max_age_s=1.0)
    for f in range(4):
        tr.update([det(50 + f * 18, 100, ts=_at(5000 + f * 100))], _at(5000 + f * 100))
    tr.reset_for_discontinuity()
    # PTS restarts at zero -- far in the past relative to the last frame.
    tr.update([det(50, 100, ts=_at(0))], _at(0))
    tr.update([det(68, 100, ts=_at(100))], _at(100))
    assert all(t.age_s >= 0.0 for t in tr.tracks), "a track aged backwards"
    assert tr.tracks[0].hits == 2, "the post-cut track did not associate"


def test_an_ordinary_gap_does_not_end_a_track():
    """A dropped frame is not a discontinuity. Resetting on every stutter
    would shred long tracks and destroy the cross-camera signal."""
    tr = ByteTracker("CAM-1", min_hits=3, max_age_s=2.5)
    for f in range(6):
        tr.update([det(50 + f * 18, 100, ts=_at(f * 100))], _at(f * 100))
    tid = tr.tracks[0].track_id
    # One frame missing: 200 ms instead of 100 ms, then it comes back.
    tr.update([], _at(600))
    tr.update([det(50 + 7 * 18, 100, ts=_at(700))], _at(700))
    assert len(tr.tracks) == 1
    assert tr.tracks[0].track_id == tid, "a single dropped frame broke the track"
    assert tr.tracks[0].hits == 7


def test_a_discontinuity_does_not_swallow_the_next_vehicle(tmp_path):
    """The bridging this prevents, shown against the same frames twice.

    The cut lands while a vehicle is MID-FRAME with its track open, and the
    scene that follows puts a different vehicle in nearly the same place --
    a camera repointed, or a decoder resynchronising after a stall. The IoU
    association continues the open track straight onto the new vehicle.

    Unsignalled, the second vehicle does not merely get the wrong id: it
    produces NO SIGHTING AT ALL. Its detections are absorbed into the first
    vehicle's track, so a red car with a different plate is recorded as
    four seconds of the white car that preceded it, and the plate read from
    it is attributed to that car. Nothing downstream can recover from this,
    because nothing downstream ever learns the second vehicle existed.

    Time runs FORWARD across this cut. A loop point that rewinds PTS is
    caught by a second mechanism -- the frame sampler stalls on a backwards
    clock -- and using one here would let this test pass with the tracker
    reset removed.
    """
    def run(*, signal_the_cut: bool):
        cfg = CameraConfig(camera_id="AHM-SAT-001", latitude=23.027,
                           longitude=72.512, anpr_capable=True, target_fps=6.0)
        p = CameraPipeline(cfg, SimulationDetector())
        out = []

        def obj(identity, plate, colour, x):
            return SceneObject(identity=identity, vehicle_type=VehicleType.CAR,
                               colour=colour, plate=plate,
                               bbox=BoundingBox(x=x, y=300, w=520, h=340),
                               latitude=23.027, longitude=72.512,
                               speed_kmph=44, heading_deg=47)

        n = 24
        for f in range(n):
            _, s = p.process(T0 + timedelta(seconds=f / 15.0),
                             scene=[obj("GT-A", "GJ01AB1234", "white", 60 + f * 14)])
            out += s
        assert p.tracker.tracks, "no open track at the cut -- test proves nothing"

        x_at_cut = 60 + (n - 1) * 14
        for f in range(90):
            x = x_at_cut + f * 14
            scene = [obj("GT-B", "GJ05XY9999", "red", x)] if x < 900 else []
            _, s = p.process(T0 + timedelta(seconds=(n + f) / 15.0), scene=scene,
                             is_discontinuity=(signal_the_cut and f == 0))
            out += s
        return p, out

    p_ok, ok = run(signal_the_cut=True)
    assert p_ok.stats.discontinuities == 1
    assert len(ok) == 2, (
        f"expected one sighting per vehicle, got {len(ok)}")
    for s in ok:
        assert s.dwell_seconds < 3.0, (
            f"sighting spans {s.dwell_seconds:.1f}s -- it was bridged across the cut")

    _, fused = run(signal_the_cut=False)
    assert len(fused) == 1, (
        "the unsignalled run was expected to fuse both vehicles into one "
        f"track, but produced {len(fused)} sightings -- this test is no "
        "longer exercising the reset")
    assert fused[0].dwell_seconds > max(s.dwell_seconds for s in ok), (
        "the fused sighting should span both vehicles' time in frame")


def test_a_discontinuity_with_nothing_open_is_harmless():
    """Most discontinuities happen with no vehicle in frame. That must be a
    no-op, not an error and not a spurious sighting."""
    cfg = CameraConfig(camera_id="AHM-SAT-001", latitude=23.027, longitude=72.512)
    p = CameraPipeline(cfg, SimulationDetector())
    _, s = p.process(T0, scene=[], is_discontinuity=True)
    assert s == []
    assert p.stats.discontinuities == 1
