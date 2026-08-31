"""Cross-camera vehicle identity.

Turns a stream of per-camera tracklets into vehicles that persist across
the estate. Every incoming Sighting either extends an existing vehicle or
starts a new one.

Two association paths, deliberately different:

  PLATE   A canonical plate match is identity evidence and does NOT require
          the spatio-temporal gate. A vehicle can legitimately vanish for
          an hour and reappear across the city; refusing that match because
          it fell outside a travel-time window would lose exactly the
          long-range links an investigation cares about.

  APPEARANCE  ReID + colour + type, and here the gate is mandatory. A white
          hatchback resembles every other white hatchback, so appearance
          alone against 50 cameras produces false positives at a rate that
          destroys operator trust within minutes. Against the 2-5 cameras
          the gate leaves, the same model is trustworthy.

Assignment across a batch is Hungarian, not greedy. At a junction with
several plausible vehicles arriving together, greedy picks wrong and the
error then propagates through the rest of every affected trajectory.

NOTHING here is presented as certain. Every association records its full
score breakdown and a decision band (AUTO / PROBABLE), and PROBABLE links
are surfaced for operator confirmation rather than silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sentinel_core import fusion, plate_rules
from sentinel_core.domain import MatchDecision, Sighting, TrackLink
from sentinel_core.log import get_logger

from .store import GateWindow, OpenVehicle, Store

try:
    import numpy as _np
except ImportError:                                     # pragma: no cover
    _np = None

log = get_logger("sentinel.processor.matcher")


@dataclass
class MatchOutcome:
    sighting: Sighting
    vehicle_track_id: str
    is_new: bool
    link: TrackLink | None = None
    decision: MatchDecision = MatchDecision.AUTO


@dataclass
class MatcherStats:
    sightings: int = 0
    new_vehicles: int = 0
    matched_by_plate: int = 0
    matched_by_appearance: int = 0
    probable: int = 0
    rejected: int = 0
    gate_candidates_total: int = 0
    gate_lookups: int = 0
    scored_pairs: int = 0

    @property
    def mean_gate_candidates(self) -> float:
        """Average candidate cameras per lookup. The number that predicts
        cross-camera precision more than any model choice does."""
        return self.gate_candidates_total / max(self.gate_lookups, 1)


class CrossCameraMatcher:
    def __init__(self, store: Store, *,
                 weights: fusion.Weights = fusion.DEFAULT_WEIGHTS,
                 track_ttl_seconds: int = 900,
                 plate_lookback_seconds: int = 7200):
        self.store = store
        self.weights = weights
        self.track_ttl_seconds = track_ttl_seconds
        self.plate_lookback_seconds = plate_lookback_seconds
        self.stats = MatcherStats()
        self._gate_cache: dict[tuple[str, int], dict[str, GateWindow]] = {}

    # ── helpers ──────────────────────────────────────────────────────
    def _gate(self, from_camera_uuid: str, seen_at: datetime,
              clock_confidence: float) -> dict[str, GateWindow]:
        """Gate lookups, cached to a 10-second bucket.

        Consecutive sightings from one camera share almost identical
        windows, and the query is the most expensive thing the matcher
        does. Bucketing costs at most 10 s of window precision against a
        window that is already hundreds of seconds wide.
        """
        bucket = int(seen_at.timestamp() // 10)
        key = (from_camera_uuid, bucket)
        if key not in self._gate_cache:
            if len(self._gate_cache) > 2000:
                self._gate_cache.clear()
            self._gate_cache[key] = self.store.gate(
                from_camera_uuid, seen_at, clock_confidence)
            self.stats.gate_lookups += 1
            self.stats.gate_candidates_total += len(self._gate_cache[key])
        return self._gate_cache[key]

    @staticmethod
    def _to_tracklet(v: OpenVehicle) -> fusion.Tracklet:
        return fusion.Tracklet(
            tracklet_id=v.last_sighting_id, camera_id=v.last_camera_id,
            ts_enter=v.last_seen, ts_exit=v.last_seen,
            vclass=v.vehicle_type, embedding=v.embedding,
            embedding_model=v.embedding_model or "",
            plate_text=v.best_plate, colour=v.vehicle_color, colour_conf=0.85)

    @staticmethod
    def _from_sighting(s: Sighting, camera_uuid: str) -> fusion.Tracklet:
        # Convert the embedding to an ndarray ONCE per sighting rather than
        # letting fusion.cosine re-convert it on every comparison. A
        # sighting is scored against several candidates, and list->ndarray
        # conversion dominates the comparison itself (96 us vs 4.5 us).
        emb = s.embedding
        if emb is not None and _np is not None and not isinstance(emb, _np.ndarray):
            emb = _np.asarray(emb, dtype=_np.float32)
        return fusion.Tracklet(
            tracklet_id=s.sighting_id, camera_id=camera_uuid,
            ts_enter=s.first_seen, ts_exit=s.last_seen,
            vclass=s.vehicle_type.value, embedding=emb,
            embedding_model=s.embedding_model or "",
            plate_text=s.plate.normalized_plate if s.plate else None,
            plate_char_confs=s.plate.char_confidences if s.plate else None,
            colour=s.vehicle_color, colour_conf=s.color_confidence or 0.8,
            quality=s.best_quality, clock_confidence=s.clock_confidence)

    # ── plate path ───────────────────────────────────────────────────
    def _match_by_plate(self, s: Sighting) -> tuple[str, float] | None:
        """Find an existing vehicle whose plate canonicalises the same way.

        Canonical form collapses the systematic OCR confusions (O/0, I/1,
        8/B, 5/S, 2/Z, 6/G), so a misread still lands on the right vehicle.
        A second confusion-weighted distance check then rejects coincidences
        that merely canonicalise alike.
        """
        if not s.plate or not s.plate.valid_format:
            return None
        canonical = plate_rules.sql_canonical(s.plate.normalized_plate)
        if not canonical:
            return None
        since = datetime.now(timezone.utc) - timedelta(seconds=self.plate_lookback_seconds)
        for row in self.store.find_by_plate(canonical, since):
            m = plate_rules.match(row["best_plate"], s.plate.normalized_plate)
            if m.band in ("exact", "confident"):
                return row["vehicle_track_id"], m.score
        return None

    # ── main entry point ─────────────────────────────────────────────
    def process_batch(self, sightings: list[Sighting]) -> list[MatchOutcome]:
        """Associate a batch of sightings with vehicle identities."""
        if not sightings:
            return []

        self.stats.sightings += len(sightings)
        outcomes: list[MatchOutcome] = []
        batch_cameras = sorted({
            cam["id"] for cam in
            (self.store.camera(s.camera_id) for s in sightings) if cam})
        open_vehicles = self.store.open_vehicles(
            self.track_ttl_seconds, reachable_from=batch_cameras)
        by_track_id = {v.vehicle_track_id: v for v in open_vehicles}
        claimed: set[str] = set()
        unresolved: list[Sighting] = []

        # ── Pass 1: plate matches, which need no gate ──
        for s in sightings:
            hit = self._match_by_plate(s)
            if hit and hit[0] not in claimed:
                vtid, score = hit
                claimed.add(vtid)
                prev = by_track_id.get(vtid)
                cam = self.store.camera(s.camera_id)
                outcomes.append(MatchOutcome(
                    sighting=s, vehicle_track_id=vtid, is_new=False,
                    decision=MatchDecision.AUTO,
                    link=TrackLink(
                        vehicle_track_id=vtid,
                        from_sighting_id=prev.last_sighting_id if prev else None,
                        to_sighting_id=s.sighting_id,
                        from_camera_id=prev.last_camera_id if prev else None,
                        to_camera_id=cam["id"] if cam else None,
                        timestamp=s.timestamp,
                        decision=MatchDecision.AUTO,
                        score_total=max(score, self.weights.AUTO_CONFIRM),
                        score_plate=score,
                        travel_actual_s=((s.timestamp - prev.last_seen).total_seconds()
                                         if prev else None),
                        reasons=["plate_match_canonical"])))
                self.stats.matched_by_plate += 1
            else:
                unresolved.append(s)

        # ── Pass 2: appearance, gated, solved as one assignment ──
        outcomes.extend(self._match_by_appearance(unresolved, open_vehicles, claimed))
        return outcomes

    def _match_by_appearance(self, sightings: list[Sighting],
                             open_vehicles: list[OpenVehicle],
                             claimed: set[str]) -> list[MatchOutcome]:
        outcomes: list[MatchOutcome] = []
        if not sightings:
            return outcomes

        available = [v for v in open_vehicles if v.vehicle_track_id not in claimed]
        if not available:
            return [self._new_vehicle(s) for s in sightings]

        # ── Use the gate as an INDEX, not as a filter ──
        #
        # The naive shape is "for each sighting, for each open vehicle,
        # score the pair, discard the ones the gate rejects". That is
        # O(sightings x vehicles) 512-dimension cosines -- around 17,000
        # per batch once a few hundred vehicles are live -- and almost all
        # of that work is thrown away, because the gate admits only a
        # handful of pairs.
        #
        # Inverting it costs nothing: each open vehicle already knows which
        # cameras it could reach and when, so index vehicles BY reachable
        # camera and time window. A sighting then only ever meets the
        # vehicles that could physically have produced it. The gate stops
        # being a post-hoc veto and becomes the thing that makes the search
        # cheap -- which is what it is for.
        by_camera: dict[str, list[tuple[int, GateWindow]]] = {}
        for vi, v in enumerate(available):
            for cam_uuid, gw in self._gate(v.last_camera_id, v.last_seen, 1.0).items():
                by_camera.setdefault(cam_uuid, []).append((vi, gw))

        # Hydrate embeddings ONLY for vehicles that some sighting in this
        # batch could actually be compared against.
        reachable_cams = {self.store.camera(s.camera_id)["id"]
                          for s in sightings if self.store.camera(s.camera_id)}
        needed = {available[vi].vehicle_track_id
                  for cam_uuid in reachable_cams
                  for vi, _ in by_camera.get(cam_uuid, [])}
        embeddings = self.store.embeddings_for(sorted(needed))
        for v in available:
            if v.embedding is None:
                v.embedding = embeddings.get(v.vehicle_track_id)

        pairs: dict[tuple[int, int], tuple[fusion.ScoreBreakdown, GateWindow]] = {}
        for si, s in enumerate(sightings):
            cam = self.store.camera(s.camera_id)
            if cam is None:
                continue
            candidates = by_camera.get(cam["id"])
            if not candidates:
                continue
            cand_tracklet = self._from_sighting(s, cam["id"])
            for vi, gw in candidates:
                # Cheap time-window test before any embedding maths.
                if not (gw.window_start <= s.first_seen <= gw.window_end):
                    continue
                v = available[vi]
                gate = fusion.Gate(
                    from_camera=v.last_camera_id, to_camera=cam["id"],
                    window_start=gw.window_start, window_end=gw.window_end,
                    expected_at=gw.expected_at, travel_s=gw.travel_s,
                    source=gw.source)
                sb = fusion.score_pair(self._to_tracklet(v), cand_tracklet, gate,
                                       target_plate=v.best_plate, w=self.weights)
                if sb.decision != "REJECTED":
                    pairs[(si, vi)] = (sb, gw)
                    self.stats.scored_pairs += 1

        assigned_s: set[int] = set()
        for si, vi in _hungarian(pairs, len(sightings), len(available)):
            sb, gw = pairs[(si, vi)]
            s, v = sightings[si], available[vi]
            decision = (MatchDecision.AUTO if sb.decision == "AUTO"
                        else MatchDecision.PROBABLE)
            if decision is MatchDecision.PROBABLE:
                self.stats.probable += 1
            else:
                self.stats.matched_by_appearance += 1
            cam = self.store.camera(s.camera_id)
            outcomes.append(MatchOutcome(
                sighting=s, vehicle_track_id=v.vehicle_track_id, is_new=False,
                decision=decision,
                link=TrackLink(
                    vehicle_track_id=v.vehicle_track_id,
                    from_sighting_id=v.last_sighting_id,
                    to_sighting_id=s.sighting_id,
                    from_camera_id=v.last_camera_id,
                    to_camera_id=cam["id"] if cam else None,
                    timestamp=s.timestamp, decision=decision,
                    score_total=sb.total, score_plate=sb.plate,
                    score_reid=sb.reid, score_color=sb.colour,
                    score_type=sb.vclass, score_spatiotemporal=sb.spatiotemporal,
                    travel_expected_s=gw.travel_s,
                    travel_actual_s=(s.timestamp - v.last_seen).total_seconds(),
                    reasons=sb.reasons)))
            assigned_s.add(si)

        for si, s in enumerate(sightings):
            if si not in assigned_s:
                outcomes.append(self._new_vehicle(s))
        return outcomes

    def _new_vehicle(self, s: Sighting) -> MatchOutcome:
        self.stats.new_vehicles += 1
        return MatchOutcome(sighting=s,
                            vehicle_track_id=self.store.next_vehicle_track_id(),
                            is_new=True, decision=MatchDecision.AUTO)


def _hungarian(pairs: dict[tuple[int, int], tuple], n_rows: int,
               n_cols: int) -> list[tuple[int, int]]:
    """Optimal assignment over the admissible pairs.

    Falls back to greedy when scipy is unavailable so the service still
    runs; greedy is documented as inferior because it mis-assigns exactly
    where it matters most -- several plausible vehicles arriving at one
    junction together.
    """
    if not pairs:
        return []
    try:
        import numpy as np
        from scipy.optimize import linear_sum_assignment
    except ImportError:                                   # pragma: no cover
        out, used_r, used_c = [], set(), set()
        for (r, c), (sb, _) in sorted(pairs.items(), key=lambda kv: -kv[1][0].total):
            if r in used_r or c in used_c:
                continue
            used_r.add(r)
            used_c.add(c)
            out.append((r, c))
        return out

    BIG = 1e6
    cost = np.full((n_rows, n_cols), BIG, dtype=float)
    for (r, c), (sb, _) in pairs.items():
        cost[r][c] = -sb.total
    rows, cols = linear_sum_assignment(cost)
    return [(int(r), int(c)) for r, c in zip(rows, cols) if cost[r][c] < BIG]
