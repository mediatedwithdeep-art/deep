"""Domain models shared across services.

These are the wire contract between video-ingestion, the AI pipeline, the
event processor and the API. Changing one is a breaking change for every
service, so each carries an explicit schema version.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────

class Protocol(StrEnum):
    RTSP = "RTSP"
    ONVIF = "ONVIF"
    HLS = "HLS"
    DVR = "DVR"          # legacy analog behind a DVR channel
    FILE = "FILE"        # looped file, used by the demo harness
    SIMULATED = "SIMULATED"


class CameraStatus(StrEnum):
    PENDING = "PENDING"
    PROBING = "PROBING"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    # Distinct from OFFLINE on purpose. A stream that ended and is being
    # retried is not the same as a camera believed gone: showing a looping
    # or briefly-dropped camera as OFFLINE makes the estate flap red and
    # trains operators to ignore the colour.
    RECONNECTING = "RECONNECTING"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"


class CameraRole(StrEnum):
    SURVEILLANCE = "SURVEILLANCE"
    ANPR = "ANPR"
    PTZ = "PTZ"
    THERMAL = "THERMAL"


class VehicleType(StrEnum):
    CAR = "car"
    MOTORCYCLE = "motorcycle"
    AUTO_RICKSHAW = "auto_rickshaw"   # not a COCO class; large share of Indian traffic
    TRUCK = "truck"
    BUS = "bus"
    TRACTOR = "tractor"
    BICYCLE = "bicycle"
    UNKNOWN = "unknown"


class AlertType(StrEnum):
    WATCHLIST_HIT = "WATCHLIST_HIT"
    ANPR_MATCH = "ANPR_MATCH"
    MULTI_CAMERA = "MULTI_CAMERA"
    RESTRICTED_ZONE = "RESTRICTED_ZONE"
    SUSPICIOUS_PATTERN = "SUSPICIOUS_PATTERN"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    CAMERA_TAMPER = "CAMERA_TAMPER"
    TARGET_LOST = "TARGET_LOST"


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class MatchDecision(StrEnum):
    AUTO = "AUTO"
    PROBABLE = "PROBABLE"
    OPERATOR = "OPERATOR"
    REJECTED = "REJECTED"


# ─────────────────────────────────────────────────────────────────────────
# Camera abstraction
#
# This is the normalisation point for the whole heterogeneous estate. An
# analog camera on a 2011 DVR and a 2025 4K ONVIF camera produce the same
# object here, and nothing downstream knows the difference.
# ─────────────────────────────────────────────────────────────────────────

class CameraLocation(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: float | None = None
    address: str | None = None
    zone: str | None = None
    district: str | None = None


class CameraOptics(BaseModel):
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    fov_deg: float = 90.0
    range_m: float = 60.0


class Camera(BaseModel):
    camera_id: str
    name: str
    department: str
    zone: str | None = None
    location: CameraLocation
    optics: CameraOptics = Field(default_factory=CameraOptics)
    protocol: Protocol
    role: CameraRole = CameraRole.SURVEILLANCE
    status: CameraStatus = CameraStatus.PENDING
    # Never a credential-bearing URL. Credentials live in the secret store and
    # are resolved at connect time by `credential_ref`.
    stream_url: str | None = None
    substream_url: str | None = None
    credential_ref: str | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    vendor: str | None = None
    model: str | None = None
    last_seen: datetime | None = None
    trust_score: float = 0.5
    anpr_capable: bool = False
    tags: list[str] = Field(default_factory=list)

    @property
    def resolution(self) -> str | None:
        return f"{self.width}x{self.height}" if self.width and self.height else None


# ─────────────────────────────────────────────────────────────────────────
# Pipeline outputs
# ─────────────────────────────────────────────────────────────────────────

class BoundingBox(BaseModel):
    x: int
    y: int
    w: int
    h: int

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    def iou(self, other: "BoundingBox") -> float:
        ax2, ay2 = self.x + self.w, self.y + self.h
        bx2, by2 = other.x + other.w, other.y + other.h
        ix = max(0, min(ax2, bx2) - max(self.x, other.x))
        iy = max(0, min(ay2, by2) - max(self.y, other.y))
        inter = ix * iy
        union = self.area + other.area - inter
        return inter / union if union else 0.0


class PlateRead(BaseModel):
    raw_plate: str
    normalized_plate: str
    confidence: float = Field(ge=0, le=1)
    valid_format: bool = False
    corrected: bool = False
    plate_width_px: int | None = None
    char_confidences: list[float] | None = None
    crop_key: str | None = None


class Detection(BaseModel):
    """One vehicle in one frame."""
    schema_version: str = "1.0"
    detection_id: str = Field(default_factory=new_id)
    camera_id: str
    track_id: str                     # per-camera, e.g. "CAM-014:T-0007"
    timestamp: datetime
    vehicle_type: VehicleType
    confidence: float = Field(ge=0, le=1)
    bbox: BoundingBox
    vehicle_color: str | None = None
    color_confidence: float | None = None
    plate: PlateRead | None = None
    latitude: float | None = None
    longitude: float | None = None
    speed_kmph: float | None = None
    heading_deg: float | None = None
    quality_score: float = 1.0
    frame_seq: int | None = None


class Sighting(BaseModel):
    """A closed tracklet: one vehicle, one camera, entry to exit.

    This -- not the per-frame Detection -- is what crosses the network at
    scale. Publishing per-frame detections for 80,000 cameras is ~800k
    msg/s; publishing tracklets is ~32k msg/s and loses nothing the
    cross-camera matcher needs.
    """
    schema_version: str = "1.0"
    sighting_id: str = Field(default_factory=new_id)
    vehicle_track_id: str             # global, e.g. "V-000123"
    camera_id: str
    track_id: str
    first_seen: datetime
    last_seen: datetime
    timestamp: datetime               # representative (midpoint)
    vehicle_type: VehicleType
    vehicle_color: str | None = None
    # How sure the colour call is. Feeds the fusion score, where a
    # low-confidence colour must pull toward "no information" rather than
    # toward "different vehicle" -- sodium street lighting makes silver and
    # white genuinely indistinguishable, and treating that disagreement as
    # evidence loses real matches.
    color_confidence: float | None = None
    vehicle_make_model: str | None = None
    plate: PlateRead | None = None
    embedding: list[float] | None = None
    embedding_model: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    heading_deg: float | None = None
    speed_kmph: float | None = None
    bbox: BoundingBox | None = None
    detection_count: int = 1
    best_quality: float = 1.0
    clock_confidence: float = 1.0
    snapshot_key: str | None = None

    @field_validator("embedding")
    @classmethod
    def _check_dim(cls, v):
        if v is not None and len(v) == 0:
            raise ValueError("embedding must be non-empty when present")
        return v

    @property
    def dwell_seconds(self) -> float:
        return max(0.0, (self.last_seen - self.first_seen).total_seconds())


class CameraHealth(BaseModel):
    schema_version: str = "1.0"
    camera_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    reachable: bool
    fps_actual: float | None = None
    frames_decoded: int = 0
    decode_errors: int = 0
    # A live socket delivering an unchanging picture is the classic silent
    # failure. Without this, a frozen camera reports perfectly healthy.
    scene_change: float | None = None
    mean_luma: float | None = None
    blur_variance: float | None = None
    latency_ms: float | None = None
    clock_offset_ms: int | None = None
    inference_ms: float | None = None
    queue_depth: int = 0
    message: str | None = None


class Alert(BaseModel):
    schema_version: str = "1.0"
    alert_id: str = Field(default_factory=new_id)
    timestamp: datetime = Field(default_factory=utcnow)
    alert_type: AlertType
    severity: Severity
    title: str
    message: str
    camera_id: str | None = None
    camera_name: str | None = None
    vehicle_track_id: str | None = None
    sighting_id: str | None = None
    watchlist_id: str | None = None
    plate: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence: float = 1.0
    # Why the system believes this. An alert an operator cannot interrogate
    # is an alert they will learn to ignore.
    evidence: dict[str, Any] = Field(default_factory=dict)
    dedup_key: str | None = None


class TrackLink(BaseModel):
    """One cross-camera association decision, with its full score breakdown."""
    link_id: str = Field(default_factory=new_id)
    vehicle_track_id: str
    from_sighting_id: str | None = None
    to_sighting_id: str
    from_camera_id: str | None = None
    to_camera_id: str
    timestamp: datetime
    decision: MatchDecision
    score_total: float
    score_plate: float = 0.0
    score_reid: float = 0.0
    score_color: float = 0.0
    score_type: float = 0.0
    score_spatiotemporal: float = 0.0
    travel_expected_s: float | None = None
    travel_actual_s: float | None = None
    reasons: list[str] = Field(default_factory=list)
