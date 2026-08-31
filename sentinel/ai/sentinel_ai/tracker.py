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
    """A tracked object within one camera.

    Every time-dependent quantity here is in SECONDS, derived from the
    capture PTS carried on the frame -- never in frames. A 25 fps camera and
    a 6 fps camera must not disagree about how long a vehicle has been
    missing simply because they disagree about how many frames that took.
    """
    track_id: str
    bbox: BoundingBox
    vehicle_type: VehicleType
    confidence: float
    first_seen: datetime
    last_seen: datetime
    hits: int = 1
    age_s: float = 0.0           # SECONDS since last successful match
    state: str = "tentative"     # tentative -> confirmed -> lost
    history: list[tuple[float, float]] = field(default_factory=list)
    detections: list[Detection] = field(default_factory=list)
    # Pixels per SECOND, not per frame. On a stream with irregular frame
    # intervals -- which is every real RTSP camera -- a per-frame velocity
    # means a different physical speed on every frame.
    velocity: tuple[float, float] = (0.0, 0.0)

    @property
    def duration_s(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()

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

    def predict(self, dt_s: float) -> BoundingBox:
        """Constant-velocity prediction, extrapolated over a real interval.

        A full Kalman filter buys little here: traffic cameras run at 6-15
        fps with short occlusions, and the measurement noise is dominated by
        detector jitter rather than motion model error. Constant velocity is
        within a few pixels and is far easier to reason about when a track
        goes wrong.

        `dt_s` is the actual elapsed capture time since this track was last
        updated. Scaling by it is what makes the prediction correct when the
        interval between frames varies -- a 165 ms gap must displace the box
        four times as far as a 40 ms gap, and a per-frame velocity cannot
        express that.
        """
        vx, vy = self.velocity
        return BoundingBox(x=int(self.bbox.x + vx * dt_s),
                           y=int(self.bbox.y + vy * dt_s),
                           w=self.bbox.w, h=self.bbox.h)


def _iou_matrix(tracks: Sequence[Track], dets: Sequence[Detection],
                dt_s: float) -> list[list[float]]:
    return [[t.predict(dt_s).iou(d.bbox) for d in dets] for t in tracks]


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
                 max_age_s: float = 2.5,
                 min_hits: int = 3,
                 direction_tolerance_deg: float = 100.0,
                 max_gap_s: float = 10.0):
        """`max_age_s` is SECONDS a track may go unmatched before it is
        retired, not frames. The previous default of 25 frames meant 1.0 s on
        a 25 fps camera and 4.2 s on a 6 fps camera -- the same estate,
        two different retirement policies, decided by an accident of frame
        rate. 2.5 s is that figure at the middle of the tuned 6-15 fps range.

        `max_gap_s` bounds how much elapsed time one update may apply. A
        stream that stalls for two minutes and resumes must not extrapolate
        a track two minutes forward; beyond this bound the gap is aged but
        the motion model is not trusted.
        """
        self.camera_id = camera_id
        self.high_thresh = high_thresh
        self.low_thresh = low_thresh
        self.match_thresh = match_thresh
        self.second_match_thresh = second_match_thresh
        self.max_age_s = max_age_s
        self.min_hits = min_hits
        self.direction_tolerance_deg = direction_tolerance_deg
        self.max_gap_s = max_gap_s

        self.tracks: list[Track] = []
        self._ids = itertools.count(1)
        # Kept for logging and diagnostics only. Nothing time-dependent may
        # read it: that is the whole point of this class's time model.
        self.frame_index = 0
        self._last_ts: datetime | None = None

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
        """Advance to `timestamp`. Returns the currently confirmed tracks.

        `timestamp` MUST be the frame's PTS-derived capture time, which is
        what `LiveStreamReader` puts on `Frame.capture_time`. Every ageing
        and motion decision below is taken from the interval between
        successive values of it, so passing arrival time here reintroduces
        precisely the defect PART 6 exists to remove.
        """
        self.frame_index += 1

        # Elapsed capture time since the previous update. Clamped at zero:
        # a non-monotonic PTS is a discontinuity, and ageing a track by a
        # negative interval would make it younger.
        if self._last_ts is None:
            dt_s = 0.0
        else:
            dt_s = max(0.0, (timestamp - self._last_ts).total_seconds())
        self._last_ts = timestamp

        # The motion model is only trusted over a plausible interval.
        predict_dt = min(dt_s, self.max_gap_s)

        for t in self.tracks:
            t.age_s += dt_s

        high = [d for d in detections if d.confidence >= self.high_thresh]
        low = [d for d in detections if self.low_thresh <= d.confidence < self.high_thresh]

        active = [t for t in self.tracks if t.state != "lost"]
        unmatched_tracks = list(range(len(active)))
        matched: list[tuple[int, Detection]] = []

        # ── Pass 1: confident detections against all active tracks ──
        if active and high:
            cost = _iou_matrix(active, high, predict_dt)
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
            cost = _iou_matrix(remaining, low, predict_dt)
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
            # Velocity in pixels per SECOND, from the real elapsed capture
            # time rather than an assumed frame interval. Light smoothing;
            # raw deltas are noisy enough to make the prediction worse than
            # no prediction. A zero interval carries no velocity
            # information, so the previous estimate stands.
            gap_s = max(0.0, (timestamp - t.last_seen).total_seconds())
            if gap_s > 0:
                inst = ((cx - px) / gap_s, (cy - py) / gap_s)
                t.velocity = (0.6 * t.velocity[0] + 0.4 * inst[0],
                              0.6 * t.velocity[1] + 0.4 * inst[1])
            t.bbox = det.bbox
            t.confidence = det.confidence
            t.last_seen = timestamp
            t.hits += 1
            t.age_s = 0.0
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
        # In seconds of capture time, so a slow camera and a fast one retire
        # a vehicle after the same real absence.
        for t in self.tracks:
            if t.age_s > self.max_age_s:
                t.state = "lost"
        return [t for t in self.tracks
                if t.state == "confirmed" and t.age_s == 0.0]

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

    def reset_for_discontinuity(self) -> list[Track]:
        """Hard-reset across a scene discontinuity. Returns the tracks that
        had already earned a sighting, so they are published rather than lost.

        A discontinuity means the next frame shows a DIFFERENT PLACE OR
        TIME: a stream looped back to its start, a decoder resynchronised
        after a long stall, a camera was repointed. Every spatial assumption
        the tracker holds is void at that instant.

        Carrying a track across it is not a cosmetic error. The track's
        first_seen would sit before the cut and its last_seen after it, so
        the sighting it produces describes a vehicle that was in two places
        with an interval between them that never elapsed -- and the
        cross-camera matcher consumes exactly those two numbers to decide
        whether a journey is physically possible. One bridged track is
        enough to manufacture a journey that never happened and to attach a
        real number plate to it.

        The velocity estimate is discarded with the tracks: extrapolating
        motion measured before the cut into the scene after it is what
        produces a confident association between two unrelated vehicles.

        Distinguishing this from an ordinary dropped frame is the reader's
        job, not the tracker's -- `LiveStreamReader` raises it only on a
        non-monotonic or implausibly-jumped PTS, never on a gap alone. A
        gap is handled by ordinary ageing above; calling this on every
        stutter would shred long tracks for nothing.
        """
        out = [t for t in self.tracks if t.hits >= self.min_hits]
        self.tracks = []
        # The clock restarts too: the first frame after the cut must not be
        # aged against a timestamp from before it.
        self._last_ts = None
        return out
