"""Alert engine.

Rules are rows in `alert_rule`, read at runtime. Adding "alert me when a
truck enters zone X" must never require a deployment, so nothing here is
hard-coded except the evaluation of each rule type.

Two principles run through this file:

1. EVERY ALERT CARRIES ITS EVIDENCE. An operator who cannot interrogate an
   alert learns to mute it, and a muted alert system is worse than none.
   Each alert records the scores, the plate read, the travel time and the
   camera that produced it.

2. NOTHING IS ASSERTED AS CERTAIN. A probable ANPR match says so, in the
   title, with its confidence and the actual characters read. The system
   is telling an officer where to point a vehicle; overstating certainty
   is how the wrong car gets stopped.

Deduplication is mandatory, not a nicety. A stationary vehicle at a
watchlisted junction generates one alert per sighting, which at 6 Hz is
several per second, and the operator mutes the whole system within a
minute.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sentinel_core import plate_rules
from sentinel_core.domain import Alert, AlertType, Severity, Sighting
from sentinel_core.log import get_logger

from .matcher import MatchOutcome
from .store import Store

log = get_logger("sentinel.processor.alerts")


@dataclass
class AlertStats:
    evaluated: int = 0
    raised: int = 0
    suppressed: int = 0
    by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class AlertEngine:
    def __init__(self, store: Store, default_dedup_seconds: int = 60):
        self.store = store
        self.default_dedup_seconds = default_dedup_seconds
        self.stats = AlertStats()
        self._dedup: dict[str, float] = {}
        # Per-vehicle recent sighting history, for the rules that need a
        # window (multi-camera, loitering, implausible speed). Bounded so a
        # long-running process cannot grow without limit.
        self._recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=24))
        self._rules: dict[str, dict] = {}
        self._watchlist: list[dict] = []
        self._rules_loaded_at = 0.0

    # ── rule/watchlist refresh ───────────────────────────────────────
    def refresh(self, force: bool = False) -> None:
        """Reload rules and watchlist periodically.

        30 seconds: fast enough that an officer adding a watchlist vehicle
        during an incident sees it take effect, cheap enough that it is not
        a query per sighting.
        """
        if not force and time.time() - self._rules_loaded_at < 30:
            return
        self._rules = self.store.alert_rules()
        self._watchlist = self.store.active_watchlist()
        self._rules_loaded_at = time.time()

    def _rule(self, code: str) -> dict | None:
        r = self._rules.get(code)
        return r if r and r.get("is_enabled", True) else None

    def _suppressed(self, key: str, seconds: int) -> bool:
        now = time.time()
        last = self._dedup.get(key)
        if last is not None and now - last < seconds:
            self.stats.suppressed += 1
            return True
        self._dedup[key] = now
        if len(self._dedup) > 50_000:
            cutoff = now - 3600
            self._dedup = {k: v for k, v in self._dedup.items() if v > cutoff}
        return False

    # ── evaluation ───────────────────────────────────────────────────
    def evaluate(self, outcomes: list[MatchOutcome]) -> list[Alert]:
        self.refresh()
        alerts: list[Alert] = []
        for o in outcomes:
            self.stats.evaluated += 1
            s = o.sighting
            cam = self.store.camera(s.camera_id) or {}
            self._recent[o.vehicle_track_id].append(
                (s.timestamp, s.camera_id, s.latitude, s.longitude))

            alerts.extend(self._watchlist_rules(o, cam))
            alerts.extend(self._anpr_rule(o, cam))
            alerts.extend(self._multi_camera_rule(o, cam))
            alerts.extend(self._restricted_zone_rule(o, cam))
            alerts.extend(self._loitering_rule(o, cam))
            alerts.extend(self._implausible_speed_rule(o, cam))

        for a in alerts:
            self.stats.raised += 1
            self.stats.by_type[a.alert_type.value] += 1
        return alerts

    # ── individual rules ─────────────────────────────────────────────
    def _watchlist_rules(self, o: MatchOutcome, cam: dict) -> list[Alert]:
        rule = self._rule("WATCHLIST_PLATE")
        if not rule or not self._watchlist:
            return []
        s = o.sighting
        if not s.plate:
            return []
        params = rule.get("params") or {}
        allowed_bands = set(params.get("match_bands", ["exact", "confident"]))
        out: list[Alert] = []

        for entry in self._watchlist:
            target = entry.get("plate_query")
            if not target:
                continue
            m = plate_rules.match(target, s.plate.normalized_plate,
                                  s.plate.char_confidences)
            if not m.matched or m.band not in allowed_bands:
                continue
            key = f"wl:{entry['wid']}:{s.camera_id}"
            if self._suppressed(key, rule.get("dedup_seconds") or self.default_dedup_seconds):
                continue

            # An exact match and a fuzzy one are NOT the same claim, and the
            # title says which. An officer acting on this needs to know
            # whether the characters were read or inferred.
            certain = m.band == "exact"
            title = (f"Watchlist vehicle detected: {target}" if certain
                     else f"Probable watchlist match: {target} (read as {s.plate.normalized_plate})")
            self.store.bump_watchlist_hit(entry["wid"])
            out.append(Alert(
                alert_type=AlertType.WATCHLIST_HIT,
                severity=Severity(entry.get("severity") or "CRITICAL"),
                title=title,
                message=(f"{entry['label']} seen at {cam.get('name', s.camera_id)}. "
                         f"Plate read {s.plate.normalized_plate} at "
                         f"{s.plate.confidence:.0%} confidence"
                         + ("." if certain else
                            f", matched to {target} allowing for OCR confusion "
                            f"(distance {m.distance:.2f}). Verify before acting.")),
                camera_id=s.camera_id, camera_name=cam.get("name"),
                vehicle_track_id=o.vehicle_track_id, sighting_id=s.sighting_id,
                watchlist_id=entry["wid"], plate=s.plate.normalized_plate,
                latitude=s.latitude, longitude=s.longitude,
                confidence=round(min(1.0, s.plate.confidence * m.score), 4),
                evidence={
                    "match_band": m.band,
                    "plate_distance": round(m.distance, 3),
                    "plate_read": s.plate.normalized_plate,
                    "plate_target": target,
                    "ocr_confidence": s.plate.confidence,
                    "valid_format": s.plate.valid_format,
                    "corrected_by_lexicon": s.plate.corrected,
                    "case_ref": entry.get("case_ref"),
                    "certain": certain,
                },
                dedup_key=key))
        return out

    def _anpr_rule(self, o: MatchOutcome, cam: dict) -> list[Alert]:
        rule = self._rule("ANPR_ANY")
        s = o.sighting
        if not rule or not s.plate:
            return []
        params = rule.get("params") or {}
        if s.plate.confidence < float(params.get("min_confidence", 0.75)):
            return []
        if params.get("require_valid_format", True) and not s.plate.valid_format:
            return []
        key = f"anpr:{s.plate.normalized_plate}:{s.camera_id}"
        if self._suppressed(key, rule.get("dedup_seconds") or 300):
            return []
        return [Alert(
            alert_type=AlertType.ANPR_MATCH, severity=Severity.INFO,
            title=f"ANPR read: {s.plate.normalized_plate}",
            message=f"{s.plate.normalized_plate} recorded at {cam.get('name', s.camera_id)}",
            camera_id=s.camera_id, camera_name=cam.get("name"),
            vehicle_track_id=o.vehicle_track_id, sighting_id=s.sighting_id,
            plate=s.plate.normalized_plate,
            latitude=s.latitude, longitude=s.longitude,
            confidence=s.plate.confidence,
            evidence={"raw": s.plate.raw_plate,
                      "corrected_by_lexicon": s.plate.corrected,
                      "plate_width_px": s.plate.plate_width_px},
            dedup_key=key)]

    def _multi_camera_rule(self, o: MatchOutcome, cam: dict) -> list[Alert]:
        rule = self._rule("MULTI_CAMERA_TRACK")
        if not rule or o.is_new:
            return []
        params = rule.get("params") or {}
        min_cameras = int(params.get("min_cameras", 3))
        window_s = float(params.get("window_minutes", 15)) * 60

        history = self._recent[o.vehicle_track_id]
        s = o.sighting
        recent = [h for h in history if (s.timestamp - h[0]).total_seconds() <= window_s]
        cameras = {h[1] for h in recent}
        if len(cameras) < min_cameras:
            return []
        key = f"multi:{o.vehicle_track_id}:{len(cameras)}"
        if self._suppressed(key, rule.get("dedup_seconds") or 120):
            return []

        span = (recent[-1][0] - recent[0][0]).total_seconds() if len(recent) > 1 else 0
        return [Alert(
            alert_type=AlertType.MULTI_CAMERA,
            severity=Severity(rule.get("severity") or "MEDIUM"),
            title=f"Vehicle tracked across {len(cameras)} cameras",
            message=(f"{o.vehicle_track_id} confirmed at {len(cameras)} cameras "
                     f"in {span/60:.1f} minutes, most recently {cam.get('name', s.camera_id)}."),
            camera_id=s.camera_id, camera_name=cam.get("name"),
            vehicle_track_id=o.vehicle_track_id, sighting_id=s.sighting_id,
            plate=s.plate.normalized_plate if s.plate else None,
            latitude=s.latitude, longitude=s.longitude,
            confidence=round(min(1.0, 0.55 + 0.12 * len(cameras)), 3),
            evidence={"camera_count": len(cameras),
                      "cameras": sorted(cameras),
                      "window_seconds": round(span, 1),
                      "association": o.decision.value},
            dedup_key=key)]

    def _restricted_zone_rule(self, o: MatchOutcome, cam: dict) -> list[Alert]:
        rule = self._rule("RESTRICTED_ZONE")
        s = o.sighting
        if not rule or s.latitude is None or s.longitude is None:
            return []
        params = rule.get("params") or {}
        types = params.get("vehicle_types") or []
        if types and s.vehicle_type.value not in types:
            return []
        zones = self.store.zones_containing(s.latitude, s.longitude)
        out: list[Alert] = []
        for z in zones:
            key = f"zone:{z['code']}:{o.vehicle_track_id}"
            if self._suppressed(key, rule.get("dedup_seconds") or 60):
                continue
            out.append(Alert(
                alert_type=AlertType.RESTRICTED_ZONE,
                severity=Severity(rule.get("severity") or "HIGH"),
                title=f"Vehicle in restricted zone: {z['name']}",
                message=(f"{s.vehicle_type.value} ({s.vehicle_color or 'unknown colour'}) "
                         f"detected inside {z['name']} at {cam.get('name', s.camera_id)}."),
                camera_id=s.camera_id, camera_name=cam.get("name"),
                vehicle_track_id=o.vehicle_track_id, sighting_id=s.sighting_id,
                plate=s.plate.normalized_plate if s.plate else None,
                latitude=s.latitude, longitude=s.longitude, confidence=0.95,
                evidence={"zone_code": z["code"], "zone_name": z["name"],
                          "vehicle_type": s.vehicle_type.value},
                dedup_key=key))
        return out

    def _loitering_rule(self, o: MatchOutcome, cam: dict) -> list[Alert]:
        rule = self._rule("LOITERING")
        if not rule:
            return []
        params = rule.get("params") or {}
        min_sightings = int(params.get("min_sightings", 4))
        window_s = float(params.get("window_minutes", 20)) * 60
        s = o.sighting
        history = self._recent[o.vehicle_track_id]
        same_cam = [h for h in history
                    if h[1] == s.camera_id
                    and (s.timestamp - h[0]).total_seconds() <= window_s]
        if len(same_cam) < min_sightings:
            return []
        key = f"loiter:{o.vehicle_track_id}:{s.camera_id}"
        if self._suppressed(key, rule.get("dedup_seconds") or 300):
            return []
        return [Alert(
            alert_type=AlertType.SUSPICIOUS_PATTERN,
            severity=Severity(rule.get("severity") or "MEDIUM"),
            title="Repeated passes at one camera",
            message=(f"{o.vehicle_track_id} passed {cam.get('name', s.camera_id)} "
                     f"{len(same_cam)} times within "
                     f"{params.get('window_minutes', 20)} minutes."),
            camera_id=s.camera_id, camera_name=cam.get("name"),
            vehicle_track_id=o.vehicle_track_id, sighting_id=s.sighting_id,
            plate=s.plate.normalized_plate if s.plate else None,
            latitude=s.latitude, longitude=s.longitude, confidence=0.7,
            evidence={"pass_count": len(same_cam),
                      "window_minutes": params.get("window_minutes", 20)},
            dedup_key=key)]

    def _implausible_speed_rule(self, o: MatchOutcome, cam: dict) -> list[Alert]:
        """Implied speed between two sightings exceeds what is possible.

        Deliberately LOW severity: in practice this fires far more often on
        a mis-association or a drifting DVR clock than on a speeding
        vehicle, so it is primarily a data-quality signal. Labelling it as
        a security finding would train operators to ignore it.
        """
        rule = self._rule("IMPOSSIBLE_SPEED")
        if not rule or o.link is None or o.link.travel_actual_s is None:
            return []
        max_kmph = float((rule.get("params") or {}).get("max_kmph", 160))
        dt = o.link.travel_actual_s
        expected = o.link.travel_expected_s
        if dt <= 1 or expected is None:
            return []
        # Reconstruct road distance from the routing prior's expected time.
        implied_kmph = (expected / dt) * 40.0
        if implied_kmph <= max_kmph:
            return []
        key = f"speed:{o.vehicle_track_id}:{o.sighting.camera_id}"
        if self._suppressed(key, rule.get("dedup_seconds") or 300):
            return []
        s = o.sighting
        return [Alert(
            alert_type=AlertType.SUSPICIOUS_PATTERN,
            severity=Severity(rule.get("severity") or "LOW"),
            title="Implausible travel time between cameras",
            message=(f"{o.vehicle_track_id} covered a route expected to take "
                     f"{expected:.0f}s in {dt:.0f}s. Likely a mis-association or "
                     f"camera clock drift rather than a real journey."),
            camera_id=s.camera_id, camera_name=cam.get("name"),
            vehicle_track_id=o.vehicle_track_id, sighting_id=s.sighting_id,
            latitude=s.latitude, longitude=s.longitude, confidence=0.45,
            evidence={"expected_s": expected, "actual_s": dt,
                      "implied_kmph": round(implied_kmph, 1),
                      "association_score": o.link.score_total,
                      "likely_cause": "mis-association or clock drift"},
            dedup_key=key)]

    # ── camera health rules ──────────────────────────────────────────
    def camera_health_alerts(self) -> list[Alert]:
        """Estate health. Roughly a fifth of a real government camera estate
        is dead, frozen or misaimed at any moment, and a VMS that does not
        say so is lying to its operators."""
        self.refresh()
        out: list[Alert] = []
        rule = self._rule("CAMERA_DOWN")
        if not rule:
            return out
        params = rule.get("params") or {}
        for cam in self.store.stale_cameras(
                seconds=int(params.get("stale_seconds", 90)),
                min_failures=int(params.get("min_consecutive_failures", 3))):
            key = f"camdown:{cam['camera_id']}"
            if self._suppressed(key, rule.get("dedup_seconds") or 600):
                continue
            out.append(Alert(
                alert_type=AlertType.CAMERA_OFFLINE,
                severity=Severity(rule.get("severity") or "MEDIUM"),
                title=f"Camera offline: {cam['name']}",
                message=(f"{cam['camera_id']} has missed "
                         f"{cam['consecutive_failures']} consecutive health beacons."),
                camera_id=cam["camera_id"], camera_name=cam["name"],
                latitude=cam["latitude"], longitude=cam["longitude"], confidence=1.0,
                evidence={"consecutive_failures": cam["consecutive_failures"]},
                dedup_key=key))
        for a in out:
            self.stats.raised += 1
            self.stats.by_type[a.alert_type.value] += 1
        return out

    def frozen_camera_alert(self, camera_ref: str, camera_name: str,
                            scene_change: float,
                            lat: float | None, lon: float | None) -> Alert | None:
        # refresh() first: this is the one alert path reachable before any
        # sighting has been processed, so without it the rule table is still
        # empty and the frozen-camera check silently never fires -- the
        # worst possible failure for a rule whose whole job is catching a
        # failure that looks healthy.
        self.refresh()
        rule = self._rule("CAMERA_FROZEN")
        if not rule:
            return None
        threshold = float((rule.get("params") or {}).get("scene_change_below", 0.002))
        if scene_change >= threshold:
            return None
        key = f"frozen:{camera_ref}"
        if self._suppressed(key, rule.get("dedup_seconds") or 600):
            return None
        self.stats.raised += 1
        self.stats.by_type[AlertType.CAMERA_TAMPER.value] += 1
        return Alert(
            alert_type=AlertType.CAMERA_TAMPER,
            severity=Severity(rule.get("severity") or "HIGH"),
            title=f"Camera picture frozen: {camera_name}",
            message=(f"{camera_ref} is delivering frames but the image is not "
                     f"changing (scene change {scene_change:.5f}). The stream looks "
                     f"healthy to every other check."),
            camera_id=camera_ref, camera_name=camera_name,
            latitude=lat, longitude=lon, confidence=0.85,
            evidence={"scene_change": scene_change, "threshold": threshold},
            dedup_key=key)
