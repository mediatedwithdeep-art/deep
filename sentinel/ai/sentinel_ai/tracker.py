"""Multi-object tracking: a ByteTrack implementation.

ByteTrack's insight, and the reason it is right for traffic: it associates
*low-confidence* detections in a second pass instead of discarding them.
That second pass is what recovers a vehicle passing behind a bus, through
IR glare, or under a shadow -- exactly the cases where a naive tracker
breaks the track and the cross-camera matcher then has to re-acquire from
scratch.

Pure Python + a small Hungarian solver, no torch. It runs at thousands of
frames/second on CPU for the object counts a traffic camera produces
(typically < 30), so it is never the bottleneck.

MIT-licensed algorithm, reimplemented here rather than vendored so the
project carries no GPL/AGPL surface.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from sentinel_core.domain import BoundingBox, Detection, VehicleType


@dataclass
class Track:
    """A tracked object within one camera."""
    track_id: str
    bbox: BoundingBox
    vehicle_type: VehicleType
    confidence: float
    first_seen: datetime
    last_seen: datetime
    hits: int = 1
    age: int = 0                 # frames since last successful match
    state: str = "tentative"     # tentative -> confirmed -> lost
    history: list[tuple[float, float]] = field(default_factory=list)
    detections: list[Detection] = field(default_factory=list)
    velocity: tuple[float, float] = (0.0, 0.0)

    @property
    def heading_deg(self) -> float | None:
        """Direction of travel in image space, degrees clockwise from up.

        Used for the direction-consistency check below and, once a camera is
        calibrated, as the seed for real-world heading.
        """
        if len(self.history) < 2:
            return None
        import math
        (x0, y0), (x1, y1) = self.history[0], self.history[-1]
        dx, dy = x1 - x0, y1 - y0
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return None
        return (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0

    def predict(self) -> BoundingBox:
        """Constant-velocity prediction.

        A full Kalman filter buys little here: traffic cameras run at 6-15
        fps with short occlusions, and the measurement noise is dominated by
        detector jitter rather than motion model error. Constant velocity is
        within a few pixels and is far easier to reason about when a track
        goes wrong.
        """
        vx, vy = self.velocity
        return BoundingBox(x=int(self.bbox.x + vx), y=int(self.bbox.y + vy),
                           w=self.bbox.w, h=self.bbox.h)


def _iou_matrix(tracks: Sequence[Track], dets: Sequence[Detection]) -> list[list[float]]:
    return [[t.predict().iou(d.bbox) for d in dets] for t in tracks]


def _greedy_assign(cost: list[list[float]], threshold: float) -> list[tuple[int, int]]:
    """Greedy max-IoU assignment.

    Greedy rather than Hungarian is a deliberate choice *here* and only
    here: within a single camera, at 6-15 fps, boxes barely move between
    frames and the IoU matrix is close to diagonal, so greedy and optimal
    agree almost always and greedy is far cheaper. The cross-camera matcher,
    where candidates genuinely compete, uses Hungarian assignment instead.
    """
    pairs: list[tuple[int, int]] = []
    used_r: set[int] = set()
    used_c: set[int] = set()
    flat = sorted(
        ((cost[i][j], i, j) for i in range(len(cost)) for j in range(len(cost[i]))),
        reverse=True)
    for score, i, j in flat:
        if score < threshold or i in used_r or j in used_c:
            continue
        used_r.add(i)
        used_c.add(j)
        pairs.append((i, j))
    return pairs


class ByteTracker:
    """Per-camera multi-object tracker.

    Thresholds are tuned for traffic at 6-15 fps, not for the MOT17
    benchmark defaults, which assume 30 fps pedestrians.
    """

    def __init__(self,
                 camera_id: str,
                 high_thresh: float = 0.5,
                 low_thresh: float = 0.15,
                 match_thresh: float = 0.30,
                 second_match_thresh: float = 0.20,
                 max_age: int = 25,
                 min_hits: int = 3,
                 direction_tolerance_deg: float = 100.0):
        self.camera_id = camera_id
        self.high_thresh = high_thresh
        self.low_thresh = low_thresh
        self.match_thresh = match_thresh
        self.second_match_thresh = second_match_thresh
        self.max_age = max_age
        self.min_hits = min_hits
        self.direction_tolerance_deg = direction_tolerance_deg

        self.tracks: list[Track] = []
        self._ids = itertools.count(1)
        self.frame_index = 0

    def _new_id(self) -> str:
        return f"{self.camera_id}:T-{next(self._ids):05d}"

    def _direction_consistent(self, track: Track, det: Detection) -> bool:
        """Reject an association that reverses the track's heading.

        Cheap, and it removes the ID-swap-at-a-junction failure that
        dominates real traffic footage: two vehicles crossing produce
        overlapping boxes, and IoU alone happily swaps their identities.
        """
        h = track.heading_deg
        if h is None or len(track.history) < 3:
            return True
        import math
        cx, cy = det.bbox.centre
        px, py = track.history[-1]
        dx, dy = cx - px, cy - py
        if abs(dx) < 2 and abs(dy) < 2:
            return True                       # stationary: no direction to violate
        new_h = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
        diff = abs((new_h - h + 180) % 360 - 180)
        return diff <= self.direction_tolerance_deg

    def update(self, detections: Sequence[Detection],
               timestamp: datetime) -> list[Track]:
        """Advance one frame. Returns the currently confirmed tracks."""
        self.frame_index += 1
        for t in self.tracks:
            t.age += 1

        high = [d for d in detections if d.confidence >= self.high_thresh]
        low = [d for d in detections if self.low_thresh <= d.confidence < self.high_thresh]

        active = [t for t in self.tracks if t.state != "lost"]
        unmatched_tracks = list(range(len(active)))
        matched: list[tuple[int, Detection]] = []

        # ── Pass 1: confident detections against all active tracks ──
        if active and high:
            cost = _iou_matrix(active, high)
            for i, j in _greedy_assign(cost, self.match_thresh):
                if self._direction_consistent(active[i], high[j]):
                    matched.append((i, high[j]))
                    if i in unmatched_tracks:
                        unmatched_tracks.remove(i)
            matched_dets = {id(d) for _, d in matched}
            high = [d for d in high if id(d) not in matched_dets]

        # ── Pass 2: the ByteTrack move. Low-confidence detections against
        # tracks that found nothing, at a looser threshold. This is what
        # holds a track through partial occlusion instead of breaking it.
        if unmatched_tracks and low:
            remaining = [active[i] for i in unmatched_tracks]
            cost = _iou_matrix(remaining, low)
            for i, j in _greedy_assign(cost, self.second_match_thresh):
                tr_idx = unmatched_tracks[i]
                if self._direction_consistent(active[tr_idx], low[j]):
                    matched.append((tr_idx, low[j]))
            done = {tr for tr, _ in matched}
            unmatched_tracks = [i for i in unmatched_tracks if i not in done]

        # ── Apply matches ──
        for i, det in matched:
            t = active[i]
            px, py = t.bbox.centre
            cx, cy = det.bbox.centre
            # Light velocity smoothing; raw frame-to-frame deltas are noisy
            # enough to make the prediction worse than no prediction.
            t.velocity = (0.6 * t.velocity[0] + 0.4 * (cx - px),
                          0.6 * t.velocity[1] + 0.4 * (cy - py))
            t.bbox = det.bbox
            t.confidence = det.confidence
            t.last_seen = timestamp
            t.hits += 1
            t.age = 0
            t.history.append((cx, cy))
            t.detections.append(det)
            if t.hits >= self.min_hits:
                t.state = "confirmed"
            # Vehicle type by majority vote across the track: a single-frame
            # misclassification should not rename the whole vehicle.
            if det.confidence > 0.7:
                counts: dict[VehicleType, int] = {}
                for d in t.detections[-10:]:
                    counts[d.vehicle_type] = counts.get(d.vehicle_type, 0) + 1
                t.vehicle_type = max(counts, key=counts.get)

        # ── Spawn tracks for confident detections that matched nothing ──
        for det in high:
            cx, cy = det.bbox.centre
            self.tracks.append(Track(
                track_id=self._new_id(), bbox=det.bbox,
                vehicle_type=det.vehicle_type, confidence=det.confidence,
                first_seen=timestamp, last_seen=timestamp,
                history=[(cx, cy)], detections=[det]))

        # ── Retire stale tracks ──
        for t in self.tracks:
            if t.age > self.max_age:
                t.state = "lost"
        return [t for t in self.tracks if t.state == "confirmed" and t.age == 0]

    def collect_finished(self, min_hits: int | None = None) -> list[Track]:
        """Remove and return tracks that have ended.

        A finished track becomes one Sighting -- the unit that crosses the
        network. Tracks that never reached `min_hits` are dropped entirely:
        a two-frame blip is detector noise, not a vehicle, and publishing it
        would pollute the map and the matcher.
        """
        min_hits = self.min_hits if min_hits is None else min_hits
        finished = [t for t in self.tracks if t.state == "lost"]
        self.tracks = [t for t in self.tracks if t.state != "lost"]
        return [t for t in finished if t.hits >= min_hits]

    def flush(self) -> list[Track]:
        """End every open track. Called when a stream disconnects, so
        in-flight vehicles still produce sightings instead of vanishing."""
        out = [t for t in self.tracks if t.hits >= self.min_hits]
        self.tracks = []
        return out
