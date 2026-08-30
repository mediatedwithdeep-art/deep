"""The quality gate.

The single most important optimisation in the pipeline. Running ANPR on
every vehicle crop costs roughly 8 ms; running it only on crops a model
could actually read costs the same 8 ms on ~10% of them. Measured on the
50-camera budget that is the difference between one GPU and four.

It also *raises* accuracy, because most wrong plate reads come from crops a
human could not read either. A confident wrong plate is worse than no
plate: it sends officers to the wrong vehicle.
"""

from __future__ import annotations

from dataclasses import dataclass

from sentinel_core.domain import BoundingBox


@dataclass(frozen=True)
class QualityVerdict:
    score: float                 # 0-1 composite
    run_anpr: bool
    run_reid: bool
    reasons: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.run_anpr or self.run_reid


@dataclass(frozen=True)
class QualityThresholds:
    # An Indian plate is ~500 mm wide. Below ~90 px of plate width, OCR is
    # noise generation regardless of what the model reports. At night, IR
    # noise pushes the practical floor higher.
    min_plate_px_day: int = 90
    min_plate_px_night: int = 110
    # Variance of the Laplacian. Motion blur is the leading cause of wrong
    # reads, and it is cheap to measure.
    min_blur_variance: float = 100.0
    max_skew_deg: float = 35.0
    min_luma: float = 40.0
    max_luma: float = 230.0
    # ReID needs far less: a 40x40 crop still carries usable colour and
    # coarse shape, which is why ReID covers the ~85% of the estate that
    # cannot read a plate.
    min_reid_area_px: int = 1600
    min_reid_confidence: float = 0.35
    max_crops_per_track: int = 3


DEFAULT = QualityThresholds()


def estimate_plate_width_px(vehicle_bbox: BoundingBox) -> float:
    """Expected plate width from the vehicle box.

    An Indian number plate is roughly 1/4.5 the width of the vehicle's rear
    face. Used as a pre-check so we can skip plate *detection* entirely on
    vehicles that are obviously too small, not just skip OCR afterwards.
    """
    return vehicle_bbox.w / 4.5


def assess(vehicle_bbox: BoundingBox,
           confidence: float,
           *,
           blur_variance: float | None = None,
           mean_luma: float | None = None,
           skew_deg: float | None = None,
           is_night: bool = False,
           crops_taken: int = 0,
           camera_anpr_capable: bool = True,
           thresholds: QualityThresholds = DEFAULT) -> QualityVerdict:
    """Decide what, if anything, is worth running on this crop."""
    reasons: list[str] = []
    score_parts: list[float] = []

    area = vehicle_bbox.area
    run_reid = (area >= thresholds.min_reid_area_px
                and confidence >= thresholds.min_reid_confidence)
    if not run_reid:
        reasons.append(f"reid_skip(area={area},conf={confidence:.2f})")
    score_parts.append(min(1.0, area / (thresholds.min_reid_area_px * 6)))

    run_anpr = True
    if not camera_anpr_capable:
        # A physical property of the installation, not a tuning knob. A
        # wide-angle junction camera cannot resolve a plate at any settings.
        run_anpr = False
        reasons.append("anpr_skip(camera_not_anpr_capable)")

    plate_px = estimate_plate_width_px(vehicle_bbox)
    floor = thresholds.min_plate_px_night if is_night else thresholds.min_plate_px_day
    if run_anpr and plate_px < floor:
        run_anpr = False
        reasons.append(f"anpr_skip(plate~{plate_px:.0f}px < {floor}px)")
    score_parts.append(min(1.0, plate_px / max(floor, 1)))

    if run_anpr and blur_variance is not None and blur_variance < thresholds.min_blur_variance:
        run_anpr = False
        reasons.append(f"anpr_skip(blur={blur_variance:.0f})")
    if blur_variance is not None:
        score_parts.append(min(1.0, blur_variance / (thresholds.min_blur_variance * 3)))

    if run_anpr and mean_luma is not None and not (
            thresholds.min_luma <= mean_luma <= thresholds.max_luma):
        run_anpr = False
        reasons.append(f"anpr_skip(luma={mean_luma:.0f})")

    if run_anpr and skew_deg is not None and abs(skew_deg) > thresholds.max_skew_deg:
        run_anpr = False
        reasons.append(f"anpr_skip(skew={skew_deg:.0f}deg)")

    if run_anpr and crops_taken >= thresholds.max_crops_per_track:
        # Do not OCR forty frames of the same car. Three good crops from a
        # track give almost all the accuracy of forty, at 7% of the cost.
        run_anpr = False
        reasons.append(f"anpr_skip(already_read_{crops_taken}_crops)")

    score = sum(score_parts) / len(score_parts) if score_parts else 0.0
    score = max(0.0, min(1.0, score * (0.5 + 0.5 * confidence)))
    return QualityVerdict(score=score, run_anpr=run_anpr, run_reid=run_reid,
                          reasons=tuple(reasons))
