"""
Cross-camera vehicle association: spatio-temporal gating, score fusion and
assignment.

This is the component that turns per-camera tracklets into one trajectory
across the estate. Three ideas do the work:

  1. GATING FIRST. Given a sighting at camera A, only cameras reachable by
     road within a plausible travel-time window are candidates. This removes
     95-98% of comparisons before any model runs. A ReID model with 85% mAP
     is unusable against 50 candidates and trustworthy against 3 -- the gate
     matters more than the model.

  2. FUSION, NOT A SINGLE SIGNAL. Plate gives precision but is unavailable on
     ~85% of a general surveillance estate. ReID gives recall but confuses
     similar vehicles. Combined and gated, they cover each other.

  3. GLOBAL ASSIGNMENT, NOT GREEDY. At a junction with several plausible
     vehicles, greedy nearest-neighbour picks wrong and the error propagates
     through the rest of the trajectory. Hungarian assignment on the whole
     cost matrix does not.

Pure Python + numpy/scipy; no GPU. The DB provides the gate (see
`candidate_cameras()` in db/migrations/002_gating.sql).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

from . import plate_rules

try:
    import numpy as np
    from scipy.optimize import linear_sum_assignment
    _HAVE_SCIPY = True
except ImportError:                                     # pragma: no cover
    _HAVE_SCIPY = False


# ─────────────────────────────────────────────────────────────────────────
# Weights. Tune these on your own annotated cross-camera transitions --
# fifty hand-labelled ground-truth hops is enough to set them sensibly and
# gives you a measured precision/recall number instead of an adjective.
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Weights:
    plate: float = 0.45
    reid: float = 0.30
    colour: float = 0.08
    vclass: float = 0.07
    spatiotemporal: float = 0.10

    AUTO_CONFIRM: float = 0.80
    PROBABLE: float = 0.55

    # Ceiling applied when no plate signal is available on either side.
    # Appearance + attributes + reachability can raise a candidate for
    # operator review, but must never auto-confirm an identity: a white
    # hatchback resembles every other white hatchback, and an auto-confirm
    # the operator did not sanction is how a system loses their trust.
    NO_PLATE_CEILING: float = 0.79

    def total(self) -> float:
        return self.plate + self.reid + self.colour + self.vclass + self.spatiotemporal


DEFAULT_WEIGHTS = Weights()


# ─────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Tracklet:
    """One vehicle, one camera, entry -> exit."""
    tracklet_id: str
    camera_id: str
    ts_enter: datetime
    ts_exit: datetime
    vclass: str
    embedding: Sequence[float] | None = None
    embedding_model: str = ""
    plate_text: str | None = None
    plate_char_confs: list[float] | None = None
    colour: str | None = None
    colour_conf: float = 0.0
    quality: float = 1.0
    clock_confidence: float = 1.0

    @property
    def ts_mid(self) -> datetime:
        return self.ts_enter + (self.ts_exit - self.ts_enter) / 2


@dataclass
class Gate:
    """Travel-time window from `candidate_cameras()`."""
    from_camera: str
    to_camera: str
    window_start: datetime
    window_end: datetime
    expected_at: datetime
    travel_s: float
    source: str = "osrm_prior"

    def feasibility(self, actual: datetime) -> float:
        """1.0 at the expected arrival time, decaying linearly to 0 at the
        window edges, 0 outside. Mirrors st_feasibility() in SQL."""
        if actual < self.window_start or actual > self.window_end:
            return 0.0
        if actual <= self.expected_at:
            span = (self.expected_at - self.window_start).total_seconds()
            if span <= 0:
                return 1.0
            return max(0.0, 1.0 - (self.expected_at - actual).total_seconds() / span)
        span = (self.window_end - self.expected_at).total_seconds()
        if span <= 0:
            return 1.0
        return max(0.0, 1.0 - (actual - self.expected_at).total_seconds() / span)


@dataclass
class ScoreBreakdown:
    total: float = 0.0
    plate: float = 0.0
    reid: float = 0.0
    colour: float = 0.0
    vclass: float = 0.0
    spatiotemporal: float = 0.0
    decision: str = "REJECTED"          # AUTO | PROBABLE | REJECTED
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score_total": round(self.total, 4),
            "score_plate": round(self.plate, 4),
            "score_reid": round(self.reid, 4),
            "score_colour": round(self.colour, 4),
            "score_type": round(self.vclass, 4),
            "score_st": round(self.spatiotemporal, 4),
            "decision": self.decision,
            "reasons": self.reasons,
        }


# ─────────────────────────────────────────────────────────────────────────
# Component scores
# ─────────────────────────────────────────────────────────────────────────

# ReID similarity calibration.
#
# Raw cosine is NOT a probability and must not be fed to the fusion score
# directly. For OSNet/CLIP-ReID on vehicle crops the two distributions look
# roughly like:
#     same vehicle, different camera :  0.70 - 0.95
#     different vehicle, same class  :  0.15 - 0.50
# so a raw 0.5 means "probably NOT the same vehicle", while a linear or
# (cos+1)/2 mapping would score it a confident 0.5-0.75. That single mistake
# is enough to make every white hatchback match every other one.
#
# Calibrate with a logistic centred at the equal-error point. Re-fit
# REID_CENTRE and REID_SHARPNESS on your own annotated cross-camera pairs
# once you have them -- the values below are sane defaults for OSNet-AIN
# trained on VeRi-776, not universal constants.
REID_CENTRE = 0.62
REID_SHARPNESS = 12.0


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Raw cosine similarity, clamped to [0, 1].

    Embeddings are stored L2-normalised, but do not assume it -- an
    un-normalised vector would silently inflate every score.
    """
    if a is None or b is None or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def reid_score(a: Sequence[float], b: Sequence[float]) -> float:
    """Calibrated ReID similarity in [0, 1], suitable for score fusion."""
    c = cosine(a, b)
    return 1.0 / (1.0 + math.exp(-REID_SHARPNESS * (c - REID_CENTRE)))


# Vehicle classes that a detector genuinely confuses on Indian roads. A
# car/auto_rickshaw disagreement is weak evidence of a different vehicle;
# a car/bus disagreement is strong evidence.
_CLASS_CONFUSION = {
    frozenset({"car", "auto_rickshaw"}): 0.55,
    frozenset({"truck", "bus"}): 0.60,
    frozenset({"car", "truck"}): 0.35,
    frozenset({"motorcycle", "cycle"}): 0.60,
    frozenset({"auto_rickshaw", "motorcycle"}): 0.30,
}

# Colours that street lighting genuinely confuses. Sodium vapour turns white
# vehicles yellow and silver vehicles into anything at all.
_COLOUR_CONFUSION = {
    frozenset({"white", "silver"}): 0.75,
    frozenset({"silver", "grey"}): 0.80,
    frozenset({"grey", "black"}): 0.55,
    frozenset({"white", "yellow"}): 0.50,
    frozenset({"red", "orange"}): 0.60,
    frozenset({"blue", "black"}): 0.45,
    frozenset({"brown", "orange"}): 0.55,
}


def class_score(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.5
    if a == b:
        return 1.0
    return _CLASS_CONFUSION.get(frozenset({a, b}), 0.0)


def colour_score(a: str | None, b: str | None, conf_a: float = 1.0, conf_b: float = 1.0) -> float:
    if not a or not b:
        return 0.5
    base = 1.0 if a == b else _COLOUR_CONFUSION.get(frozenset({a, b}), 0.0)
    # A low-confidence colour call should pull toward "no information" (0.5),
    # not toward "mismatch" (0.0) -- absence of evidence is not evidence.
    conf = min(conf_a, conf_b)
    return base * conf + 0.5 * (1.0 - conf)


def plate_score(target_plate: str | None, tracklet: Tracklet) -> tuple[float, str]:
    if not target_plate or not tracklet.plate_text:
        return 0.0, "no_plate"
    m = plate_rules.match(target_plate, tracklet.plate_text, tracklet.plate_char_confs)
    return m.score, f"plate_{m.band}(d={m.distance:.2f})"


# ─────────────────────────────────────────────────────────────────────────
# Fusion
# ─────────────────────────────────────────────────────────────────────────

def score_pair(
    prev: Tracklet,
    cand: Tracklet,
    gate: Gate | None,
    target_plate: str | None = None,
    w: Weights = DEFAULT_WEIGHTS,
) -> ScoreBreakdown:
    """Score a candidate tracklet against the previous confirmed one.

    Returns a REJECTED breakdown immediately when the hard gate fails --
    scoring outside the gate is how false positives get in.
    """
    sb = ScoreBreakdown()

    # --- Hard gate. Nothing outside the reachable travel window is scored.
    if gate is not None:
        st = gate.feasibility(cand.ts_enter)
        if st <= 0.0:
            sb.decision = "REJECTED"
            sb.reasons.append(
                f"outside_st_gate(arrived={cand.ts_enter.isoformat()}, "
                f"window={gate.window_start.isoformat()}..{gate.window_end.isoformat()})")
            return sb
        sb.spatiotemporal = st
        sb.reasons.append(f"st_ok({gate.source},travel={gate.travel_s:.0f}s,feas={st:.2f})")
    else:
        # No gate available (adjacency not built, or same camera). Neutral,
        # but flag it: an ungated match is much weaker evidence.
        sb.spatiotemporal = 0.5
        sb.reasons.append("no_gate_available")

    # --- Plate
    sb.plate, plate_reason = plate_score(target_plate, cand)
    have_plate = bool(target_plate) and bool(cand.plate_text)
    sb.reasons.append(plate_reason)

    # --- ReID. Refuse to compare embeddings from different models; the
    # numbers would be meaningless and silently wrong.
    have_reid = False
    if prev.embedding is not None and cand.embedding is not None:
        if prev.embedding_model and cand.embedding_model and \
                prev.embedding_model != cand.embedding_model:
            sb.reasons.append(
                f"reid_skipped(model_mismatch:{prev.embedding_model}!={cand.embedding_model})")
        else:
            raw = cosine(prev.embedding, cand.embedding)
            sb.reid = reid_score(prev.embedding, cand.embedding)
            have_reid = True
            sb.reasons.append(f"reid_cos={raw:.3f}->cal={sb.reid:.3f}")
    else:
        sb.reasons.append("reid_unavailable")

    # --- Attributes
    sb.colour = colour_score(prev.colour, cand.colour, prev.colour_conf or 1.0, cand.colour_conf or 1.0)
    sb.vclass = class_score(prev.vclass, cand.vclass)

    # --- Fuse, renormalising over the signals actually present.
    #
    # This matters more than it looks. Plate carries 45% of the weight, but
    # only ~10-15% of a general surveillance estate can read a plate at all.
    # Without renormalisation a plateless comparison loses 45% of the score
    # mass and can never cross the PROBABLE threshold no matter how good the
    # appearance match -- the system would go blind on 85% of its cameras.
    # So each unavailable signal surrenders its weight to the ones present.
    parts: list[tuple[float, float]] = [
        (w.spatiotemporal, sb.spatiotemporal),
        (w.colour, sb.colour),
        (w.vclass, sb.vclass),
    ]
    if have_plate:
        parts.append((w.plate, sb.plate))
    if have_reid:
        parts.append((w.reid, sb.reid))

    weight_sum = sum(wt for wt, _ in parts)
    sb.total = sum(wt * val for wt, val in parts) / weight_sum if weight_sum else 0.0

    if not have_plate:
        capped = min(sb.total, w.NO_PLATE_CEILING)
        if capped < sb.total:
            sb.reasons.append(f"capped_no_plate({sb.total:.3f}->{capped:.3f})")
        sb.total = capped

    # --- Hard override: a lexicon-validated exact plate read inside the gate
    # is a confirm regardless of ReID disagreement. Appearance models fail on
    # lighting and viewpoint change far more often than a grammar-checked
    # plate read is wrong.
    if sb.plate >= 0.99 and sb.spatiotemporal > 0:
        sb.decision = "AUTO"
        sb.total = max(sb.total, w.AUTO_CONFIRM)
        sb.reasons.append("override_exact_plate_in_gate")
        return sb

    # A class mismatch that is not a known confusion pair vetoes the match:
    # a bus is not a motorcycle no matter what the embedding says.
    if sb.vclass == 0.0 and prev.vclass and cand.vclass:
        sb.decision = "REJECTED"
        sb.reasons.append(f"class_veto({prev.vclass}!={cand.vclass})")
        return sb

    if sb.total >= w.AUTO_CONFIRM:
        sb.decision = "AUTO"
    elif sb.total >= w.PROBABLE:
        sb.decision = "PROBABLE"
    else:
        sb.decision = "REJECTED"
    return sb


# ─────────────────────────────────────────────────────────────────────────
# Assignment
# ─────────────────────────────────────────────────────────────────────────

def assign(
    open_tracks: list[tuple[str, Tracklet]],
    candidates: list[Tracklet],
    gates: dict[tuple[str, str], Gate],
    target_plates: dict[str, str | None] | None = None,
    w: Weights = DEFAULT_WEIGHTS,
) -> list[tuple[str, Tracklet, ScoreBreakdown]]:
    """Globally assign candidate tracklets to open global tracks.

    `open_tracks`  : (global_track_id, last confirmed tracklet)
    `candidates`   : new tracklets from this matcher tick
    `gates`        : (from_camera, to_camera) -> Gate
    `target_plates`: global_track_id -> target plate, when known

    Hungarian assignment over the full cost matrix. Greedy matching fails
    exactly where it matters most -- several plausible vehicles at one
    junction -- and that error then propagates through the trajectory.
    """
    if not open_tracks or not candidates:
        return []

    target_plates = target_plates or {}
    n, m = len(open_tracks), len(candidates)

    grid: list[list[ScoreBreakdown]] = []
    for gt_id, prev in open_tracks:
        row = []
        for cand in candidates:
            gate = gates.get((prev.camera_id, cand.camera_id))
            row.append(score_pair(prev, cand, gate, target_plates.get(gt_id), w))
        grid.append(row)

    if not _HAVE_SCIPY:                                  # pragma: no cover
        # Greedy fallback so the module still runs without scipy. Documented
        # as inferior; do not rely on it in production.
        out, used = [], set()
        order = sorted(((grid[i][j].total, i, j) for i in range(n) for j in range(m)),
                       reverse=True)
        taken_rows = set()
        for total, i, j in order:
            if i in taken_rows or j in used or grid[i][j].decision == "REJECTED":
                continue
            taken_rows.add(i)
            used.add(j)
            out.append((open_tracks[i][0], candidates[j], grid[i][j]))
        return out

    # scipy minimises, so cost = -score. Rejected pairs get a large finite
    # cost rather than inf: inf makes the problem infeasible when a row has
    # no legal column, and we want the solver to simply leave it unassigned.
    cost = np.full((n, m), 1e6, dtype=float)
    for i in range(n):
        for j in range(m):
            if grid[i][j].decision != "REJECTED":
                cost[i][j] = -grid[i][j].total

    rows, cols = linear_sum_assignment(cost)
    out = []
    for i, j in zip(rows, cols):
        if cost[i][j] >= 1e6:          # solver filled an unmatched slot
            continue
        out.append((open_tracks[i][0], candidates[j], grid[i][j]))
    return out


def next_camera_predictions(gates: Sequence[Gate], top_k: int = 5) -> list[dict]:
    """Cameras to watch next, soonest first.

    Feeds the dashboard's look-here-next highlight -- the map lights up
    ahead of the vehicle rather than behind it.
    """
    return [
        {
            "camera_id": g.to_camera,
            "expected_at": g.expected_at.isoformat(),
            "window_start": g.window_start.isoformat(),
            "window_end": g.window_end.isoformat(),
            "travel_s": g.travel_s,
            "source": g.source,
        }
        for g in sorted(gates, key=lambda g: g.expected_at)[:top_k]
    ]
