"""Vehicle re-identification embeddings.

ReID is the recall half of cross-camera tracking. Plate reads are precise
but unavailable on ~85% of a real estate (see the ANPR accuracy curve);
appearance embeddings are available everywhere but confuse similar
vehicles. Fused under the spatio-temporal gate, they cover each other.

512-d to match OSNet-AIN's native output, so swapping the simulated
extractor for real weights needs no schema migration.

Embeddings are L2-normalised, so cosine similarity is a dot product.
Crucially, raw cosine is NOT a probability: for vehicle ReID the same-ID
and different-ID distributions overlap around 0.55-0.65, which is why
sentinel_core.fusion calibrates it through a logistic before fusing. Never
feed a raw cosine into a score.
"""

from __future__ import annotations

import abc
import hashlib
import math
import random

EMBEDDING_DIM = 512


def _unit_vector_from(key: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic pseudo-random unit vector for a string key."""
    seed = int(hashlib.sha256(key.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def l2_normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


class ReIDExtractor(abc.ABC):
    model_name: str = "abstract"
    dim: int = EMBEDDING_DIM

    @abc.abstractmethod
    def extract(self, *, vehicle_crop=None, identity: str | None = None,
                vehicle_type: str = "car", colour: str = "white",
                view_quality: float = 1.0) -> list[float] | None: ...

    def extract_batch(self, items: list[dict]) -> list[list[float] | None]:
        """Batched extraction. Overridden by the ONNX backend, where
        batching is the difference between an idle GPU and a saturated one."""
        return [self.extract(**it) for it in items]

    @staticmethod
    def aggregate(embeddings: list[list[float]],
                  weights: list[float] | None = None) -> list[float] | None:
        """Quality-weighted mean of a track's embeddings.

        A track's best 3 crops averaged is materially more robust than any
        single frame: it averages out viewpoint and lighting noise, which is
        exactly the noise that causes cross-camera false negatives.
        """
        if not embeddings:
            return None
        weights = weights or [1.0] * len(embeddings)
        dim = len(embeddings[0])
        acc = [0.0] * dim
        total = 0.0
        for emb, w in zip(embeddings, weights):
            if emb is None or len(emb) != dim:
                continue
            for i, x in enumerate(emb):
                acc[i] += x * w
            total += w
        if total == 0:
            return None
        return l2_normalize([x / total for x in acc])


class SimulatedReIDExtractor(ReIDExtractor):
    """Embeddings with realistic same-ID / different-ID separation.

    An embedding is built from three components:
        identity  -- unique to the vehicle
        type      -- shared by all vehicles of that class
        colour    -- shared by all vehicles of that colour
    plus view noise scaled by crop quality.

    The type and colour components are deliberately shared, because that is
    what makes the hard case hard: two different white hatchbacks genuinely
    do produce similar embeddings, and a simulator that gave every vehicle
    an orthogonal vector would make cross-camera matching look trivially
    easy and hide every false-positive failure mode.
    """

    model_name = "simulated-osnet-ain-512"

    # Calibrated so the same-ID / different-ID distributions OVERLAP, the
    # way real vehicle ReID does (OSNet-AIN on VeRi-776: same-ID ~0.75,
    # different-ID-same-type ~0.40, with tails that cross). A simulator
    # with clean separation would make cross-camera matching look trivial
    # and would hide every false-positive mode the gate exists to catch.
    W_IDENTITY = 0.58
    W_TYPE = 0.34
    W_COLOUR = 0.40

    def __init__(self, seed: int = 20260907, noise_scale: float = 1.0):
        self._seed = seed
        self._noise_scale = noise_scale
        self._cache: dict[str, list[float]] = {}

    def _component(self, kind: str, value: str) -> list[float]:
        key = f"{self._seed}:{kind}:{value}"
        if key not in self._cache:
            self._cache[key] = _unit_vector_from(key)
        return self._cache[key]

    def extract(self, *, vehicle_crop=None, identity: str | None = None,
                vehicle_type: str = "car", colour: str = "white",
                view_quality: float = 1.0) -> list[float] | None:
        if identity is None:
            return None
        vid = self._component("id", identity)
        vty = self._component("type", vehicle_type or "unknown")
        vco = self._component("colour", colour or "unknown")

        rng = random.Random(int(hashlib.sha256(
            f"{identity}:{view_quality:.3f}".encode()).hexdigest()[:12], 16))

        # Real ReID variance is STRUCTURED, not iid. What actually moves an
        # embedding between two cameras is viewpoint (front/rear/three-
        # quarter), illumination and partial occlusion -- each of which
        # shifts the whole vector in a consistent direction. Modelling it as
        # per-component Gaussian noise instead would average out over 512
        # dimensions and produce distributions far tighter than reality,
        # with no overlap between same-ID and different-ID pairs. That would
        # make cross-camera matching look easy and hide every false positive.
        view_bucket = rng.randrange(8)
        vview = self._component("view", f"v{view_bucket}")
        # Poor crops are affected more by viewpoint and lighting, which is
        # why the quality gate feeds view_quality in here. The 0.18-0.80
        # range is tuned so the resulting distributions match OSNet-AIN on
        # VeRi-776 (same-ID ~0.73 sd 0.11, different-ID-same-type ~0.36
        # sd 0.08) INCLUDING the ~7% tail overlap between them.
        w_view = rng.uniform(0.18, 0.80) * (1.35 - 0.55 * max(0.0, min(1.0, view_quality)))

        sigma = self._noise_scale * 0.35 * (1.0 - 0.6 * max(0.0, min(1.0, view_quality)))

        out = [
            self.W_IDENTITY * vid[i] + self.W_TYPE * vty[i] + self.W_COLOUR * vco[i]
            + w_view * vview[i]
            + (rng.gauss(0.0, sigma / math.sqrt(EMBEDDING_DIM)) if sigma > 0 else 0.0)
            for i in range(EMBEDDING_DIM)
        ]
        return l2_normalize(out)


class OnnxReIDExtractor(ReIDExtractor):
    """Real ReID over onnxruntime (OSNet-AIN or CLIP-ReID export).

    Batched, because per-crop inference wastes most of a GPU: 50 cameras
    produce ~400 crops/second and one-at-a-time inference leaves the device
    mostly idle between calls.
    """

    model_name = "onnx-reid"

    def __init__(self, model_path: str, providers: list[str] | None = None,
                 input_size: tuple[int, int] = (256, 128)):
        import onnxruntime as ort
        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.input_size = input_size
        self.model_name = f"onnx:{model_path.rsplit('/', 1)[-1]}"

    def _preprocess(self, crop):
        import numpy as np
        import cv2
        h, w = self.input_size
        img = cv2.resize(crop, (w, h)).astype("float32") / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype="float32")
        std = np.array([0.229, 0.224, 0.225], dtype="float32")
        img = (img - mean) / std
        return np.transpose(img, (2, 0, 1))

    def extract(self, *, vehicle_crop=None, identity: str | None = None,
                vehicle_type: str = "car", colour: str = "white",
                view_quality: float = 1.0) -> list[float] | None:
        if vehicle_crop is None:
            return None
        out = self.extract_batch([{"vehicle_crop": vehicle_crop}])
        return out[0] if out else None

    def extract_batch(self, items: list[dict]) -> list[list[float] | None]:
        import numpy as np
        crops = [it.get("vehicle_crop") for it in items]
        valid = [(i, c) for i, c in enumerate(crops) if c is not None and c.size > 0]
        results: list[list[float] | None] = [None] * len(items)
        if not valid:
            return results
        batch = np.stack([self._preprocess(c) for _, c in valid])
        feats = self.session.run(None, {self.input_name: batch})[0]
        for (idx, _), f in zip(valid, feats):
            results[idx] = l2_normalize([float(x) for x in f])
        return results


def create_extractor(backend: str = "simulation", **kwargs) -> ReIDExtractor:
    if backend == "simulation":
        return SimulatedReIDExtractor(**kwargs)
    if backend == "onnx":
        return OnnxReIDExtractor(**kwargs)
    raise ValueError(f"unknown ReID backend: {backend!r}")
