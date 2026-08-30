"""Object detection behind one interface.

Two implementations live in `backends/`:
  SimulationDetector -- converts simulator ground truth into realistically
                        noisy detections. No weights, no GPU.
  OnnxDetector       -- a real YOLO/RT-DETR export via onnxruntime.

Both return the same `Detection` objects, so the tracker, quality gate,
ANPR and ReID stages are identical in demo and production. That is the
whole point: the demo exercises the real code path, not a parallel one.

LICENCE NOTE. The ONNX backend deliberately does not depend on the
Ultralytics package, which is AGPL-3.0 and a procurement blocker for a
state-government deployment. It loads a plain ONNX graph, so YOLOX,
RT-DETRv2 or D-FINE (all Apache-2.0) drop straight in. See
docs/TECH_STACK.md for the licence table.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime

from sentinel_core.domain import BoundingBox, Detection, VehicleType


@dataclass
class SceneObject:
    """Ground truth for one vehicle visible to one camera at one instant.

    Produced by the world simulator, consumed by SimulationDetector. Because
    ground truth is known, the demo can *measure* detection recall, ANPR
    accuracy and cross-camera precision instead of asserting them -- which
    is where the benchmark numbers in docs/BENCHMARKS.md come from.
    """
    identity: str                 # stable ground-truth vehicle id
    vehicle_type: VehicleType
    colour: str
    plate: str
    bbox: BoundingBox
    distance_m: float = 30.0
    occlusion: float = 0.0        # 0 = clear, 1 = fully hidden
    speed_kmph: float = 40.0
    heading_deg: float = 0.0
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class DetectorStats:
    frames: int = 0
    objects_in: int = 0
    detections_out: int = 0
    missed: int = 0
    false_positives: int = 0
    inference_ms_total: float = 0.0

    @property
    def recall(self) -> float:
        return self.detections_out and (self.objects_in - self.missed) / max(self.objects_in, 1)

    @property
    def mean_inference_ms(self) -> float:
        return self.inference_ms_total / max(self.frames, 1)


class Detector(abc.ABC):
    name: str = "abstract"
    classes: list[str] = field(default_factory=list)

    def __init__(self) -> None:
        self.stats = DetectorStats()

    @abc.abstractmethod
    def detect(self, *, camera_id: str, timestamp: datetime,
               frame=None, scene: list[SceneObject] | None = None,
               frame_seq: int = 0) -> list[Detection]: ...

    def warmup(self) -> None:
        return None
