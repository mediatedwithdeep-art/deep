"""Plate recognition.

Two stages, always:
  1. plate detection on the VEHICLE crop (not the full frame -- searching a
     1280x720 frame for a 90 px plate wastes most of the compute)
  2. character recognition on the plate crop

Then, and this is where most of the real-world accuracy comes from,
lexicon-constrained correction against the Indian plate grammar and a
confidence that reflects how readable the crop actually was. See
shared/sentinel_core/plate_rules.py.

The simulated recogniser is not a stub that returns fixed strings. It
models the *actual failure modes* of plate OCR -- systematic character
confusions (O/0, 8/B, 5/S), degradation with plate size and light, and
outright read failures -- so everything downstream (fuzzy matching,
watchlist alerting, the confidence shown to the operator) is exercised by
realistic input rather than by clean data that would never occur.
"""

from __future__ import annotations

import abc
import hashlib
import random
from dataclasses import dataclass

from sentinel_core import plate_rules
from sentinel_core.domain import PlateRead

# Characters an OCR model genuinely confuses, with the direction it tends
# to fail in. Not symmetric: a dirty '8' reads as 'B' far more often than a
# clean 'B' reads as '8'.
_CONFUSION_TABLE: dict[str, list[str]] = {
    "0": ["O", "D", "Q"], "O": ["0", "D"],
    "1": ["I", "L", "7"], "I": ["1"],
    "8": ["B", "6", "3"], "B": ["8"],
    "5": ["S", "6"],      "S": ["5"],
    "2": ["Z", "7"],      "Z": ["2"],
    "6": ["G", "5", "8"], "G": ["6", "C"],
    "4": ["A", "9"],      "A": ["4"],
    "7": ["1", "T"],      "T": ["7"],
    "9": ["4", "0"],      "D": ["0", "O"],
}


@dataclass
class PlateCandidate:
    """A detected plate region within a vehicle crop."""
    x: int
    y: int
    w: int
    h: int
    confidence: float


class PlateRecognizer(abc.ABC):
    """Plate detection + OCR behind one interface.

    Swapping the simulated recogniser for PaddleOCR or a fine-tuned PARSeq
    means implementing this and changing one setting. Nothing else in the
    pipeline changes.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def read(self, *, vehicle_crop=None, ground_truth: str | None = None,
             plate_width_px: float = 100.0, is_night: bool = False,
             blur_variance: float = 300.0) -> PlateRead | None:
        """Return a plate read, or None when nothing legible was found."""

    @staticmethod
    def finalize(raw: str, confidence: float,
                 char_confidences: list[float] | None = None,
                 plate_width_px: int | None = None) -> PlateRead | None:
        """Normalise, lexicon-correct, and package a raw OCR string.

        Shared by every backend so that a real model and the simulator
        produce identically shaped output -- including the correction step,
        which is worth 8-15 points of exact-match accuracy and must not be
        something only one backend does.
        """
        if not raw:
            return None
        parsed = plate_rules.correct(raw)
        normalized = parsed.normalized or plate_rules.normalize(raw)
        if not normalized:
            return None
        # A grammatically valid plate is more trustworthy than a raw string
        # of the same OCR confidence, and an invalid one is less. Reflect
        # that in the number the operator sees.
        adjusted = confidence * (1.08 if parsed.valid else 0.80)
        return PlateRead(
            raw_plate=raw,
            normalized_plate=normalized,
            confidence=round(max(0.0, min(1.0, adjusted)), 4),
            valid_format=parsed.valid,
            corrected=parsed.corrected,
            plate_width_px=plate_width_px,
            char_confidences=char_confidences,
        )


class SimulatedPlateRecognizer(PlateRecognizer):
    """Models real ANPR failure modes against known ground truth.

    Read probability and per-character error rate are driven by plate pixel
    width, light and blur -- the three factors that actually dominate in the
    field. The resulting accuracy curve is deliberately calibrated to the
    published envelope in docs: ~95% on a dedicated ANPR lane in daylight,
    ~40% on a general surveillance camera at night.
    """

    name = "simulated-anpr-v1"

    def __init__(self, seed: int = 20260907):
        self._seed = seed

    def _rng(self, key: str) -> random.Random:
        # Deterministic per (plate, conditions): the same vehicle at the same
        # camera reads the same way on every run, so demos are reproducible
        # and test failures are debuggable.
        h = hashlib.sha256(f"{self._seed}:{key}".encode()).hexdigest()
        return random.Random(int(h[:16], 16))

    def read(self, *, vehicle_crop=None, ground_truth: str | None = None,
             plate_width_px: float = 100.0, is_night: bool = False,
             blur_variance: float = 300.0) -> PlateRead | None:
        if not ground_truth:
            return None

        rng = self._rng(f"{ground_truth}:{int(plate_width_px)}:{is_night}:{int(blur_variance)}")

        # ── Probability the plate is read at all ──
        # Ramps from 0 at ~55 px of plate width to saturation at ~125 px.
        size_factor = max(0.0, min(1.0, (plate_width_px - 55) / 70.0))
        blur_factor = max(0.35, min(1.0, blur_variance / 220.0))

        # Night degradation is NOT uniform, and getting this wrong is the
        # most common modelling error in ANPR estimates. A dedicated ANPR
        # lane has IR illuminators aimed at the plate, whose retro-
        # reflective surface lights up beautifully -- such a camera barely
        # degrades after dark. A wide-angle surveillance camera has no
        # useful illumination at plate distance and falls off a cliff.
        # Proxy for "is this a dedicated ANPR install": plate pixel width.
        if is_night:
            night_factor = 0.93 if plate_width_px >= 130 else (
                           0.72 if plate_width_px >= 100 else 0.45)
        else:
            night_factor = 1.0

        p_read = size_factor * blur_factor * night_factor * 0.99
        if rng.random() > p_read:
            return None

        # ── Per-character error rate under the same conditions ──
        base_err = 0.018
        err = base_err * (2.4 - 1.4 * size_factor) * (1.9 if is_night else 1.0) \
                       * (2.1 - 1.1 * blur_factor)

        chars = list(plate_rules.normalize(ground_truth))
        confs: list[float] = []
        for i, ch in enumerate(chars):
            if rng.random() < err and ch in _CONFUSION_TABLE:
                chars[i] = rng.choice(_CONFUSION_TABLE[ch])
                confs.append(round(rng.uniform(0.35, 0.68), 3))
            else:
                confs.append(round(rng.uniform(0.82, 0.995), 3))

        # Occasionally lose a character entirely -- a bolt, a bracket, mud.
        if rng.random() < err * 0.5 and len(chars) > 6:
            drop = rng.randrange(len(chars))
            chars.pop(drop)
            confs.pop(drop)

        raw = "".join(chars)
        overall = sum(confs) / len(confs) if confs else 0.0
        return self.finalize(raw, overall, confs, int(plate_width_px))


class OnnxPlateRecognizer(PlateRecognizer):
    """Real two-stage ANPR over onnxruntime (CPU or CUDA).

    Not exercised by the demo -- it needs model files the repository does
    not ship -- but the interface and the post-processing are identical, so
    dropping in weights is a configuration change. See
    ai/models/README.md for where to get and how to export them.
    """

    name = "onnx-anpr"

    def __init__(self, plate_detector_path: str, recognizer_path: str,
                 providers: list[str] | None = None):
        import onnxruntime as ort
        self.det = ort.InferenceSession(plate_detector_path, providers=providers)
        self.rec = ort.InferenceSession(recognizer_path, providers=providers)
        self.charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def read(self, *, vehicle_crop=None, ground_truth: str | None = None,
             plate_width_px: float = 100.0, is_night: bool = False,
             blur_variance: float = 300.0) -> PlateRead | None:
        if vehicle_crop is None:
            return None
        import numpy as np

        plate = self._detect_plate(vehicle_crop)
        if plate is None:
            return None
        crop = vehicle_crop[plate.y:plate.y + plate.h, plate.x:plate.x + plate.w]
        if crop.size == 0:
            return None

        inp = self._preprocess_plate(crop)
        logits = self.rec.run(None, {self.rec.get_inputs()[0].name: inp})[0][0]
        # Greedy CTC decode with per-character confidence retained: the
        # matcher weights a mismatch by how sure the recogniser was about
        # that specific character.
        text, confs, prev = [], [], -1
        for step in logits:
            e = np.exp(step - step.max())
            probs = e / e.sum()
            idx = int(probs.argmax())
            if idx != prev and idx != 0:
                text.append(self.charset[idx - 1])
                confs.append(float(probs[idx]))
            prev = idx
        if not text:
            return None
        return self.finalize("".join(text), sum(confs) / len(confs), confs, plate.w)

    def _detect_plate(self, vehicle_crop) -> PlateCandidate | None:
        import numpy as np
        h, w = vehicle_crop.shape[:2]
        blob = self._preprocess_det(vehicle_crop)
        out = self.det.run(None, {self.det.get_inputs()[0].name: blob})[0]
        best, best_conf = None, 0.0
        for row in np.asarray(out).reshape(-1, out.shape[-1]):
            conf = float(row[4])
            if conf > best_conf and conf > 0.35:
                cx, cy, bw, bh = row[:4]
                best_conf = conf
                best = PlateCandidate(
                    x=max(0, int((cx - bw / 2) * w / 320)),
                    y=max(0, int((cy - bh / 2) * h / 320)),
                    w=int(bw * w / 320), h=int(bh * h / 320), confidence=conf)
        return best

    @staticmethod
    def _preprocess_det(img):
        import numpy as np
        import cv2
        r = cv2.resize(img, (320, 320)).astype(np.float32) / 255.0
        return np.transpose(r, (2, 0, 1))[None, ...]

    @staticmethod
    def _preprocess_plate(crop):
        import numpy as np
        import cv2
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        r = cv2.resize(g, (128, 32)).astype(np.float32) / 255.0
        return r[None, None, ...]


def create_recognizer(backend: str = "simulation", **kwargs) -> PlateRecognizer:
    if backend == "simulation":
        return SimulatedPlateRecognizer(**kwargs)
    if backend == "onnx":
        return OnnxPlateRecognizer(**kwargs)
    raise ValueError(f"unknown ANPR backend: {backend!r}")
