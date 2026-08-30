"""Real detection via onnxruntime. CPU or CUDA, no Ultralytics dependency.

Accepts either YOLO-style output [batch, 84, N] / [batch, N, 84] or
RT-DETR-style [batch, N, 6]. Both are common exports and guessing wrong
silently produces zero detections, so the layout is detected explicitly.
"""

from __future__ import annotations

import time
from datetime import datetime

from sentinel_core.domain import BoundingBox, Detection, VehicleType

from ..detector import Detector, SceneObject

# COCO indices we care about. COCO has no auto-rickshaw class, which is why
# a fine-tune on Indian footage is required for production -- see
# docs/CV_PIPELINE.md. Until then auto-rickshaws land in car/truck/motorcycle.
COCO_VEHICLES: dict[int, VehicleType] = {
    1: VehicleType.BICYCLE,
    2: VehicleType.CAR,
    3: VehicleType.MOTORCYCLE,
    5: VehicleType.BUS,
    7: VehicleType.TRUCK,
}


class OnnxDetector(Detector):
    name = "onnx-detector"

    def __init__(self, model_path: str, input_size: int = 640,
                 conf_threshold: float = 0.35, iou_threshold: float = 0.45,
                 providers: list[str] | None = None,
                 class_map: dict[int, VehicleType] | None = None):
        super().__init__()
        import onnxruntime as ort
        if providers is None:
            available = ort.get_available_providers()
            providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                         if "CUDAExecutionProvider" in available
                         else ["CPUExecutionProvider"])
        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.class_map = class_map or COCO_VEHICLES
        self.name = f"onnx:{model_path.rsplit('/', 1)[-1]}:{providers[0]}"
        self.classes = [t.value for t in set(self.class_map.values())]

    def warmup(self) -> None:
        """First inference includes graph optimisation and allocation, and
        can be 10-50x slower. Doing it at startup keeps that cost out of the
        latency measurements and off the first live frame."""
        import numpy as np
        blob = np.zeros((1, 3, self.input_size, self.input_size), dtype=np.float32)
        self.session.run(None, {self.input_name: blob})

    def _preprocess(self, frame):
        import numpy as np
        import cv2
        h, w = frame.shape[:2]
        scale = min(self.input_size / w, self.input_size / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (nw, nh))
        # Letterbox rather than stretch: a squashed vehicle is a different
        # shape from the one the model was trained on.
        canvas = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        canvas[:nh, :nw] = resized
        blob = canvas[:, :, ::-1].astype(np.float32) / 255.0
        return np.transpose(blob, (2, 0, 1))[None, ...], scale

    @staticmethod
    def _nms(boxes, scores, iou_threshold):
        import numpy as np
        idxs = scores.argsort()[::-1]
        keep = []
        while idxs.size:
            i = idxs[0]
            keep.append(i)
            if idxs.size == 1:
                break
            xx1 = np.maximum(boxes[i, 0], boxes[idxs[1:], 0])
            yy1 = np.maximum(boxes[i, 1], boxes[idxs[1:], 1])
            xx2 = np.minimum(boxes[i, 2], boxes[idxs[1:], 2])
            yy2 = np.minimum(boxes[i, 3], boxes[idxs[1:], 3])
            inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
            a_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
            a_r = ((boxes[idxs[1:], 2] - boxes[idxs[1:], 0]) *
                   (boxes[idxs[1:], 3] - boxes[idxs[1:], 1]))
            iou = inter / (a_i + a_r - inter + 1e-9)
            idxs = idxs[1:][iou <= iou_threshold]
        return keep

    def detect(self, *, camera_id: str, timestamp: datetime,
               frame=None, scene: list[SceneObject] | None = None,
               frame_seq: int = 0) -> list[Detection]:
        if frame is None:
            return []
        import numpy as np
        t0 = time.perf_counter()
        self.stats.frames += 1

        blob, scale = self._preprocess(frame)
        raw = self.session.run(None, {self.input_name: blob})[0]
        pred = np.asarray(raw)

        boxes, scores, classes = [], [], []
        if pred.ndim == 3 and pred.shape[1] in (84, 85) and pred.shape[2] > pred.shape[1]:
            pred = pred[0].T                                  # YOLOv8: (84, N) -> (N, 84)
        elif pred.ndim == 3:
            pred = pred[0]

        for row in pred:
            if row.shape[0] >= 84:                            # YOLOv8-style, no objectness
                cls_scores = row[4:84]
                cid = int(cls_scores.argmax())
                conf = float(cls_scores[cid])
                cx, cy, bw, bh = row[:4]
            elif row.shape[0] == 6:                           # RT-DETR-style
                x1, y1, x2, y2, conf, cid = row
                cx, cy, bw, bh = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
                cid, conf = int(cid), float(conf)
            else:
                continue
            if conf < self.conf_threshold or cid not in self.class_map:
                continue
            boxes.append([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2])
            scores.append(conf)
            classes.append(cid)

        out: list[Detection] = []
        if boxes:
            b = np.array(boxes, dtype=np.float32)
            s = np.array(scores, dtype=np.float32)
            for i in self._nms(b, s, self.iou_threshold):
                x1, y1, x2, y2 = b[i] / scale
                out.append(Detection(
                    camera_id=camera_id, track_id="", timestamp=timestamp,
                    vehicle_type=self.class_map[classes[i]],
                    confidence=round(float(s[i]), 4),
                    bbox=BoundingBox(x=max(0, int(x1)), y=max(0, int(y1)),
                                     w=int(x2 - x1), h=int(y2 - y1)),
                    frame_seq=frame_seq))

        self.stats.detections_out += len(out)
        self.stats.inference_ms_total += (time.perf_counter() - t0) * 1000
        return out
