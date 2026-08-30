"""Tests for cross-camera association. Runs with or without scipy.

Run: python3 sentinel/services/matcher/test_fusion.py
"""
import sys, os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fusion as F

T0 = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)


def mk(tid, cam, t_offset_s, vclass="car", colour="white", emb=None,
       plate=None, model="osnet_v3", dwell=6):
    return F.Tracklet(
        tracklet_id=tid, camera_id=cam,
        ts_enter=T0 + timedelta(seconds=t_offset_s),
        ts_exit=T0 + timedelta(seconds=t_offset_s + dwell),
        vclass=vclass, colour=colour, colour_conf=0.9,
        embedding=emb, embedding_model=model, plate_text=plate,
    )


def mk_gate(a, b, travel_s, at=0, lo=None, hi=None):
    base = T0 + timedelta(seconds=at)
    return F.Gate(
        from_camera=a, to_camera=b,
        window_start=base + timedelta(seconds=lo if lo is not None else travel_s / 1.6),
        window_end=base + timedelta(seconds=hi if hi is not None else travel_s / 0.35 + 120),
        expected_at=base + timedelta(seconds=travel_s),
        travel_s=travel_s,
    )


E_A = [1.0, 0.0, 0.0, 0.0]
E_A2 = [0.96, 0.28, 0.0, 0.0]      # same vehicle, different viewpoint
E_B = [0.0, 1.0, 0.0, 0.0]         # different vehicle


def test_gate_rejects_arrival_that_is_too_early():
    # 300s of road between the cameras; a sighting 20s later cannot be the
    # same vehicle unless it teleported.
    prev, cand = mk("t1", "camA", 0, emb=E_A), mk("t2", "camB", 20, emb=E_A)
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300))
    assert sb.decision == "REJECTED"
    assert any("outside_st_gate" in r for r in sb.reasons)


def test_gate_rejects_arrival_that_is_too_late():
    prev, cand = mk("t1", "camA", 0, emb=E_A), mk("t2", "camB", 5000, emb=E_A)
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300))
    assert sb.decision == "REJECTED"


def test_feasibility_peaks_at_expected_arrival():
    g = mk_gate("camA", "camB", 300)
    at_expected = g.feasibility(T0 + timedelta(seconds=300))
    off_peak = g.feasibility(T0 + timedelta(seconds=700))
    assert at_expected > off_peak > 0.0
    assert abs(at_expected - 1.0) < 1e-6


def test_exact_plate_in_gate_auto_confirms_despite_reid_disagreement():
    # Appearance models fail on lighting/viewpoint far more often than a
    # grammar-checked plate read is wrong, so the plate must win.
    prev = mk("t1", "camA", 0, emb=E_A, plate="GJ01AB1234")
    cand = mk("t2", "camB", 300, emb=E_B, plate="GJ01AB1234")
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300), target_plate="GJ01AB1234")
    assert sb.decision == "AUTO"
    assert "override_exact_plate_in_gate" in sb.reasons


def test_ocr_garbled_plate_still_confirms_via_lexicon_correction():
    prev = mk("t1", "camA", 0, emb=E_A, plate="GJ01AB1234")
    cand = mk("t2", "camB", 300, emb=E_A2, plate="GJO1AB1Z34")   # O/0 and Z/2
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300), target_plate="GJ01AB1234")
    assert sb.decision == "AUTO"


def test_reid_alone_inside_gate_reaches_probable_not_auto():
    # No plate anywhere: appearance + attributes + gate should surface the
    # candidate for operator review, but must not auto-confirm.
    prev = mk("t1", "camA", 0, emb=E_A)
    cand = mk("t2", "camB", 300, emb=E_A2)
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300))
    assert sb.decision == "PROBABLE", sb.as_dict()


def test_different_vehicle_inside_gate_is_rejected():
    prev = mk("t1", "camA", 0, emb=E_A, colour="white")
    cand = mk("t2", "camB", 300, emb=E_B, colour="red")
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300))
    assert sb.decision == "REJECTED", sb.as_dict()


def test_class_mismatch_vetoes():
    prev = mk("t1", "camA", 0, vclass="bus", emb=E_A, plate="GJ01AB1234")
    cand = mk("t2", "camB", 300, vclass="motorcycle", emb=E_A, plate="GJ01AB9999")
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300))
    assert sb.decision == "REJECTED"
    assert any("class_veto" in r for r in sb.reasons)


def test_class_confusion_pair_does_not_veto():
    # car/auto_rickshaw is a real detector confusion on Indian roads.
    prev = mk("t1", "camA", 0, vclass="car", emb=E_A)
    cand = mk("t2", "camB", 300, vclass="auto_rickshaw", emb=E_A2)
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300))
    assert not any("class_veto" in r for r in sb.reasons)


def test_embeddings_from_different_models_are_not_compared():
    prev = mk("t1", "camA", 0, emb=E_A, model="osnet_v3")
    cand = mk("t2", "camB", 300, emb=E_A, model="clipreid_v1")
    sb = F.score_pair(prev, cand, mk_gate("camA", "camB", 300))
    assert sb.reid == 0.0
    assert any("model_mismatch" in r for r in sb.reasons)


def test_low_confidence_colour_pulls_toward_no_information():
    confident = F.colour_score("white", "red", 1.0, 1.0)
    unsure = F.colour_score("white", "red", 0.2, 0.2)
    assert confident == 0.0
    assert 0.3 < unsure < 0.5      # ignorance, not evidence of mismatch


def test_assignment_picks_the_right_vehicle_at_a_junction():
    # Two open tracks, two candidates arriving together at camB. Greedy
    # matching gets this wrong; global assignment must not.
    prevA = mk("a1", "camA", 0, emb=E_A, colour="white")
    prevB = mk("b1", "camA", 0, emb=E_B, colour="red")
    candA = mk("a2", "camB", 300, emb=E_A2, colour="white")
    candB = mk("b2", "camB", 305, emb=E_B, colour="red")

    gates = {("camA", "camB"): mk_gate("camA", "camB", 300)}
    res = F.assign([("gtA", prevA), ("gtB", prevB)], [candA, candB], gates)

    mapping = {gt: tr.tracklet_id for gt, tr, _ in res}
    assert mapping.get("gtA") == "a2", mapping
    assert mapping.get("gtB") == "b2", mapping


def test_assignment_leaves_ungateable_candidates_unassigned():
    prev = mk("a1", "camA", 0, emb=E_A)
    cand = mk("z9", "camZ", 30, emb=E_A)          # no gate for camA->camZ
    res = F.assign([("gtA", prev)], [cand], gates={})
    # No gate means neutral ST and no plate; must not auto-link.
    assert all(sb.decision != "AUTO" for _, _, sb in res)


def test_next_camera_predictions_are_ordered_by_arrival():
    gates = [mk_gate("camA", "camC", 900), mk_gate("camA", "camB", 300)]
    preds = F.next_camera_predictions(gates)
    assert [p["camera_id"] for p in preds] == ["camB", "camC"]


if __name__ == "__main__":
    print(f"(scipy available: {F._HAVE_SCIPY})\n")
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)
