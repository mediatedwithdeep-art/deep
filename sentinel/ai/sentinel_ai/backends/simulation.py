"""Simulation detector: ground truth in, realistic detections out.

This is not a stub. It reproduces the failure modes that actually matter
downstream, because if the demo only ever sees clean detections then the
tracker's occlusion handling, the quality gate, the fuzzy plate matcher and
the fusion scorer are all untested by it:

  * misses, driven by apparent size and occlusion
  * box jitter, which is what makes IoU association non-trivial
  * class confusion between classes a real detector confuses
    (car/auto_rickshaw, truck/bus) and never between ones it does not
  * occasional false positives, so the tracker's min_hits filter earns
    its place
  * confidence that correlates with all of the above
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime

from sentinel_core.domain import BoundingBox, Detection, VehicleType

from ..detector import Detector, SceneObject

# Classes a real detector genuinely confuses on Indian roads, and the rate.
# A car is never mistaken for a bus; it is regularly mistaken for an
# auto-rickshaw, which is not even a COCO class.
_CLASS_CONFUSION: dict[VehicleType, list[tuple[VehicleType, float]]] = {
    VehicleType.CAR:           [(VehicleType.AUTO_RICKSHAW, 0.030), (VehicleType.TRUCK, 0.012)],
    VehicleType.AUTO_RICKSHAW: [(VehicleType.CAR, 0.070), (VehicleType.MOTORCYCLE, 0.025)],
    VehicleType.MOTORCYCLE:    [(VehicleType.BICYCLE, 0.045), (VehicleType.AUTO_RICKSHAW, 0.020)],
    VehicleType.TRUCK:         [(VehicleType.BUS, 0.055)],
    VehicleType.BUS:           [(VehicleType.TRUCK, 0.045)],
    VehicleType.BICYCLE:       [(VehicleType.MOTORCYCLE, 0.060)],
    VehicleType.TRACTOR:       [(VehicleType.TRUCK, 0.080)],
}


class SimulationDetector(Detector):
    name = "simulation-detector-v1"

    def __init__(self, seed: int = 20260907,
                 base_miss_rate: float = 0.04,
                 false_positive_rate: float = 0.01,
                 jitter_px: float = 4.0):
        super().__init__()
        self.classes = [t.value for t in VehicleType if t != VehicleType.UNKNOWN]
        self._seed = seed
        self.base_miss_rate = base_miss_rate
        self.false_positive_rate = false_positive_rate
        self.jitter_px = jitter_px

    def _rng(self, *parts) -> random.Random:
        key = ":".join(str(p) for p in (self._seed, *parts))
        return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))

    def detect(self, *, camera_id: str, timestamp: datetime,
               frame=None, scene: list[SceneObject] | None = None,
               frame_seq: int = 0) -> list[Detection]:
        t0 = time.perf_counter()
        scene = scene or []
        out: list[Detection] = []
        self.stats.frames += 1
        self.stats.objects_in += len(scene)

        for obj in scene:
            rng = self._rng(camera_id, frame_seq, obj.identity)

            # Small or occluded objects are missed more often. A 20x15 box
            # at the far end of a junction is genuinely hard.
            area = obj.bbox.area
            size_penalty = 1.0 if area >= 6000 else (0.35 if area >= 1500 else 0.10)
            miss_p = self.base_miss_rate + obj.occlusion * 0.65 + (1.0 - size_penalty) * 0.35
            if rng.random() < min(0.97, miss_p):
                self.stats.missed += 1
                continue

            # Box jitter, scaled by how small the object is. This is what
            # makes the tracker's association job real rather than trivial.
            j = self.jitter_px * (1.0 + (1.0 - size_penalty) * 2.0)
            bbox = BoundingBox(
                x=max(0, int(obj.bbox.x + rng.gauss(0, j))),
                y=max(0, int(obj.bbox.y + rng.gauss(0, j))),
                w=max(8, int(obj.bbox.w + rng.gauss(0, j * 0.6))),
                h=max(8, int(obj.bbox.h + rng.gauss(0, j * 0.6))))

            vtype = obj.vehicle_type
            for alt, p in _CLASS_CONFUSION.get(obj.vehicle_type, []):
                if rng.random() < p * (2.0 - size_penalty):
                    vtype = alt
                    break

            conf = min(0.99, max(0.20,
                       0.94 * size_penalty
                       + 0.06
                       - obj.occlusion * 0.45
                       + rng.gauss(0, 0.045)))

            out.append(Detection(
                camera_id=camera_id, track_id="", timestamp=timestamp,
                vehicle_type=vtype, confidence=round(conf, 4), bbox=bbox,
                latitude=obj.latitude, longitude=obj.longitude,
                speed_kmph=obj.speed_kmph, heading_deg=obj.heading_deg,
                frame_seq=frame_seq))

        # Spurious detections: shadows, reflections, signage. The tracker's
        # min_hits requirement is what removes them, so they need to exist
        # for that logic to be exercised.
        fp_rng = self._rng(camera_id, frame_seq, "fp")
        if fp_rng.random() < self.false_positive_rate:
            self.stats.false_positives += 1
            out.append(Detection(
                camera_id=camera_id, track_id="", timestamp=timestamp,
                vehicle_type=VehicleType.CAR,
                confidence=round(fp_rng.uniform(0.36, 0.55), 4),
                bbox=BoundingBox(x=fp_rng.randrange(0, 1000),
                                 y=fp_rng.randrange(0, 500),
                                 w=fp_rng.randrange(30, 90),
                                 h=fp_rng.randrange(25, 70)),
                frame_seq=frame_seq))

        self.stats.detections_out += len(out)
        self.stats.inference_ms_total += (time.perf_counter() - t0) * 1000
        return out
