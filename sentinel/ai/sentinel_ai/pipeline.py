"""The per-camera AI pipeline.

    frame/scene
      -> detect
      -> track            (ByteTrack, per camera)
      -> quality gate     (drops ~80-90% of downstream work)
      -> ANPR + ReID + attributes   (only on crops worth the compute)
      -> tracklet close-out
      -> Sighting

The design rule that keeps this affordable: **expensive stages run per
TRACK, not per FRAME**. A vehicle crossing a junction produces 40 frames;
it needs at most 3 ANPR reads and one aggregated embedding. Running ANPR on
all 40 would cost 13x more and produce a *worse* answer, because most of
those frames are motion-blurred.

One `CameraPipeline` per camera; they share no mutable state, so scaling
out is a matter of running more workers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sentinel_core.domain import (
    BoundingBox, Detection, PlateRead, Sighting, VehicleType,
)
from sentinel_core.log import get_logger

from .anpr import PlateRecognizer, create_recognizer
from .attributes import classify_rgb
from .detector import Detector, SceneObject
from .quality import DEFAULT as DEFAULT_QUALITY, QualityThresholds, assess
from .reid import ReIDExtractor, create_extractor
from .tracker import ByteTracker, Track

log = get_logger("sentinel.ai.pipeline")


@dataclass
class CameraConfig:
    """What the pipeline needs to know about the camera it is watching."""
    camera_id: str
    latitude: float
    longitude: float
    heading_deg: float | None = None
    anpr_capable: bool = False
    width: int = 1280
    height: int = 720
    fps: float = 12.0
    # AI runs at this rate regardless of the stream's native fps. A vehicle
    # at 60 km/h moves 1.7 m per 100 ms, so 6 fps is ample for tracking and
    # costs a third of what 15 fps would.
    target_fps: float = 6.0
    trust_score: float = 1.0


@dataclass
class PipelineStats:
    frames_in: int = 0
    frames_processed: int = 0
    frames_skipped: int = 0
    detections: int = 0
    tracks_started: int = 0
    sightings_emitted: int = 0
    anpr_attempts: int = 0
    anpr_reads: int = 0
    anpr_gated_out: int = 0
    reid_extractions: int = 0
    total_ms: float = 0.0

    @property
    def mean_latency_ms(self) -> float:
        return self.total_ms / max(self.frames_processed, 1)

    @property
    def anpr_gate_efficiency(self) -> float:
        """Fraction of candidate crops the gate refused. This is the number
        that determines whether 50 cameras fit on one GPU."""
        total = self.anpr_attempts + self.anpr_gated_out
        return self.anpr_gated_out / total if total else 0.0


@dataclass
class _TrackState:
    """Accumulated evidence for one open track."""
    plates: list[PlateRead] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    embedding_weights: list[float] = field(default_factory=list)
    colours: dict[str, float] = field(default_factory=dict)
    crops_read: int = 0
    best_quality: float = 0.0
    identity: str | None = None          # ground truth, simulation only
    lat: float | None = None
    lon: float | None = None
    speed: float | None = None
    heading: float | None = None


class CameraPipeline:
    def __init__(self, config: CameraConfig, detector: Detector,
                 recognizer: PlateRecognizer | None = None,
                 reid: ReIDExtractor | None = None,
                 quality: QualityThresholds = DEFAULT_QUALITY,
                 vehicle_id_prefix: str = "V"):
        self.config = config
        self.detector = detector
        self.recognizer = recognizer or create_recognizer("simulation")
        self.reid = reid or create_extractor("simulation")
        self.quality = quality
        self.tracker = ByteTracker(config.camera_id)
        self.stats = PipelineStats()

        self._track_state: dict[str, _TrackState] = {}
        self._last_processed_ts: float = 0.0
        self._frame_seq = 0
        self._scene_index: dict[str, SceneObject] = {}

    # ── frame admission ──────────────────────────────────────────────
    def _should_process(self, timestamp: datetime) -> bool:
        """Frame sampling. Decoding is cheap; inference is not, so we drop
        frames here rather than downstream."""
        if self.config.target_fps <= 0:
            return True
        now = timestamp.timestamp()
        interval = 1.0 / self.config.target_fps
        if now - self._last_processed_ts + 1e-9 >= interval:
            self._last_processed_ts = now
            return True
        return False

    # ── main entry point ─────────────────────────────────────────────
    def process(self, timestamp: datetime, *, frame=None,
                scene: list[SceneObject] | None = None,
                is_night: bool = False) -> tuple[list[Detection], list[Sighting]]:
        """Advance the pipeline by one frame.

        Returns (detections_this_frame, sightings_closed_this_frame).
        """
        self.stats.frames_in += 1
        if not self._should_process(timestamp):
            self.stats.frames_skipped += 1
            return [], []

        t0 = time.perf_counter()
        self.stats.frames_processed += 1
        self._frame_seq += 1

        if scene:
            self._scene_index = {o.identity: o for o in scene}

        detections = self.detector.detect(
            camera_id=self.config.camera_id, timestamp=timestamp,
            frame=frame, scene=scene, frame_seq=self._frame_seq)
        self.stats.detections += len(detections)

        active = self.tracker.update(detections, timestamp)

        for track in active:
            self._enrich(track, frame=frame, scene=scene, is_night=is_night)

        closed = self.tracker.collect_finished()
        sightings = [s for s in (self._close_track(t) for t in closed) if s]
        self.stats.sightings_emitted += len(sightings)

        self.stats.total_ms += (time.perf_counter() - t0) * 1000
        return detections, sightings

    # ── per-track enrichment ─────────────────────────────────────────
    def _enrich(self, track: Track, *, frame, scene, is_night: bool) -> None:
        state = self._track_state.setdefault(track.track_id, _TrackState())
        det = track.detections[-1]

        # Match this track back to its ground-truth object when running in
        # simulation, so ANPR and ReID have something to be noisy about.
        gt = self._match_ground_truth(det.bbox) if self._scene_index else None
        if gt is not None:
            state.identity = gt.identity
            state.lat, state.lon = gt.latitude, gt.longitude
            state.speed, state.heading = gt.speed_kmph, gt.heading_deg

        blur = 300.0 if not is_night else 180.0
        verdict = assess(
            det.bbox, det.confidence,
            blur_variance=blur,
            mean_luma=70.0 if is_night else 130.0,
            is_night=is_night,
            crops_taken=state.crops_read,
            camera_anpr_capable=self.config.anpr_capable,
            thresholds=self.quality)
        state.best_quality = max(state.best_quality, verdict.score)

        # ── ANPR, only on crops that can plausibly be read ──
        if verdict.run_anpr:
            self.stats.anpr_attempts += 1
            state.crops_read += 1
            plate_px = det.bbox.w / 4.5
            read = self.recognizer.read(
                vehicle_crop=self._crop(frame, det.bbox),
                ground_truth=gt.plate if gt else None,
                plate_width_px=plate_px, is_night=is_night, blur_variance=blur)
            if read:
                self.stats.anpr_reads += 1
                state.plates.append(read)
        else:
            self.stats.anpr_gated_out += 1

        # ── ReID: one embedding per few frames, aggregated at close-out ──
        if verdict.run_reid and len(state.embeddings) < 5:
            emb = self.reid.extract(
                vehicle_crop=self._crop(frame, det.bbox),
                identity=state.identity,
                vehicle_type=track.vehicle_type.value,
                colour=gt.colour if gt else "unknown",
                view_quality=verdict.score)
            if emb:
                self.stats.reid_extractions += 1
                state.embeddings.append(emb)
                state.embedding_weights.append(max(0.1, verdict.score))

        # ── colour, voted across the track ──
        colour = gt.colour if gt else self._colour_from_crop(frame, det.bbox, is_night)
        if colour:
            state.colours[colour] = state.colours.get(colour, 0.0) + max(0.1, verdict.score)

    def _match_ground_truth(self, bbox: BoundingBox) -> SceneObject | None:
        """Best-IoU match between a detection and the simulated scene.

        Only used in simulation. It exists because the detector deliberately
        jitters boxes, so identity cannot be assumed by index.
        """
        best, best_iou = None, 0.25
        for obj in self._scene_index.values():
            iou = bbox.iou(obj.bbox)
            if iou > best_iou:
                best, best_iou = obj, iou
        return best

    @staticmethod
    def _crop(frame, bbox: BoundingBox):
        if frame is None:
            return None
        h, w = frame.shape[:2]
        x1, y1 = max(0, bbox.x), max(0, bbox.y)
        x2, y2 = min(w, bbox.x + bbox.w), min(h, bbox.y + bbox.h)
        return frame[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else None

    def _colour_from_crop(self, frame, bbox: BoundingBox, is_night: bool) -> str | None:
        crop = self._crop(frame, bbox)
        if crop is None or getattr(crop, "size", 0) == 0:
            return None
        # Sample the middle band: the top is usually glass and the bottom
        # is shadow and road, both of which drag the mean toward grey.
        h = crop.shape[0]
        band = crop[int(h * 0.35):int(h * 0.75)]
        if band.size == 0:
            band = crop
        b, g, r = (float(band[:, :, i].mean()) for i in range(3))
        colour, _conf = classify_rgb(r, g, b, is_night=is_night)
        return colour

    # ── close-out: a finished track becomes one Sighting ─────────────
    def _close_track(self, track: Track) -> Sighting | None:
        state = self._track_state.pop(track.track_id, None)
        if state is None:
            return None

        # Best plate = highest confidence, with valid-format reads winning
        # ties. A grammatically valid read at 0.80 is worth more than an
        # invalid one at 0.85: the invalid one cannot be a real plate.
        best_plate: PlateRead | None = None
        if state.plates:
            best_plate = max(state.plates,
                             key=lambda p: (p.valid_format, p.confidence))

        embedding = self.reid.aggregate(state.embeddings, state.embedding_weights)
        colour = max(state.colours, key=state.colours.get) if state.colours else None

        return Sighting(
            vehicle_track_id="",          # assigned by the cross-camera matcher
            camera_id=self.config.camera_id,
            track_id=track.track_id,
            first_seen=track.first_seen,
            last_seen=track.last_seen,
            timestamp=track.first_seen + (track.last_seen - track.first_seen) / 2,
            vehicle_type=track.vehicle_type,
            vehicle_color=colour,
            plate=best_plate,
            embedding=embedding,
            embedding_model=self.reid.model_name if embedding else None,
            latitude=state.lat if state.lat is not None else self.config.latitude,
            longitude=state.lon if state.lon is not None else self.config.longitude,
            heading_deg=state.heading,
            speed_kmph=state.speed,
            bbox=track.bbox,
            detection_count=track.hits,
            best_quality=round(state.best_quality, 4),
            clock_confidence=self.config.trust_score,
        )

    def flush(self) -> list[Sighting]:
        """Close every open track. Called when a stream drops, so vehicles
        in flight still produce sightings instead of disappearing."""
        out = [s for s in (self._close_track(t) for t in self.tracker.flush()) if s]
        self.stats.sightings_emitted += len(out)
        return out

    def health_snapshot(self) -> dict:
        return {
            "camera_id": self.config.camera_id,
            "frames_in": self.stats.frames_in,
            "frames_processed": self.stats.frames_processed,
            "sampling_ratio": round(
                self.stats.frames_processed / max(self.stats.frames_in, 1), 3),
            "detections": self.stats.detections,
            "sightings": self.stats.sightings_emitted,
            "anpr_reads": self.stats.anpr_reads,
            "anpr_gate_efficiency": round(self.stats.anpr_gate_efficiency, 3),
            "inference_ms": round(self.stats.mean_latency_ms, 3),
            "open_tracks": len(self.tracker.tracks),
        }
