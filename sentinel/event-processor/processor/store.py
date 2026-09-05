"""Persistence for the event processor.

Plain psycopg over the schema rather than an ORM. The hot paths here are
batch inserts of sightings and a gated candidate query that leans on
PostGIS and the adjacency table; both are hand-written SQL in the schema's
own terms, and an ORM would obscure exactly the parts worth reading.

Every write is batched. At 80,000 cameras this consumer sees ~32k
tracklets/second, and a per-row round trip would spend all its time on
network latency rather than work.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

try:
    import numpy as _np
except ImportError:                                     # pragma: no cover
    _np = None

from sentinel_core.domain import Sighting, TrackLink
from sentinel_core.log import get_logger

log = get_logger("sentinel.processor.store")


@dataclass
class OpenVehicle:
    """A vehicle currently being tracked, with its most recent sighting."""
    vehicle_id: str
    vehicle_track_id: str
    last_seen: datetime
    last_camera_id: str
    last_camera_ref: str
    last_sighting_id: str
    vehicle_type: str
    vehicle_color: str | None
    best_plate: str | None
    embedding: list[float] | None
    embedding_model: str | None
    camera_count: int
    sighting_count: int
    is_watchlisted: bool


@dataclass
class GateWindow:
    to_camera_id: str
    camera_ref: str
    travel_s: float
    window_start: datetime
    window_end: datetime
    expected_at: datetime
    source: str


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: psycopg.Connection | None = None
        self._seq = itertools.count(1)
        self._camera_cache: dict[str, dict] = {}
        self._embedding_cache: dict[str, Any] = {}

    # ── connection ───────────────────────────────────────────────────
    def connect(self) -> None:
        self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
        self._load_camera_cache()
        self._seq = itertools.count(self._next_vehicle_number())

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self.connect()
        return self._conn                        # type: ignore[return-value]

    def _load_camera_cache(self) -> None:
        """Camera metadata changes rarely and is read on every sighting.

        Caching it removes a join from the hottest path in the system. The
        cache is refreshed on demand when an unknown camera appears, so a
        newly onboarded camera does not need a restart.
        """
        rows = self.conn.execute(
            "SELECT id::text, camera_id, name, latitude, longitude, zone, "
            "       anpr_capable, trust_score "
            "FROM camera WHERE status <> 'DISABLED'").fetchall()
        self._camera_cache = {r["camera_id"]: r for r in rows}
        log.info("camera cache loaded", extra={"count": len(self._camera_cache)})

    def camera(self, camera_ref: str) -> dict | None:
        cam = self._camera_cache.get(camera_ref)
        if cam is None:
            self._load_camera_cache()
            cam = self._camera_cache.get(camera_ref)
        return cam

    def _next_vehicle_number(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(NULLIF(regexp_replace(vehicle_track_id, '\\D', '', 'g'), '')::bigint), 0) AS n "
            "FROM vehicle").fetchone()
        return int(row["n"]) + 1 if row else 1

    def next_vehicle_track_id(self) -> str:
        return f"V-{next(self._seq):06d}"

    # ── the spatio-temporal gate ─────────────────────────────────────
    def gate(self, from_camera_id: str, seen_at: datetime,
             clock_confidence: float = 1.0,
             max_travel_s: float = 900) -> dict[str, GateWindow]:
        """Cameras reachable from `from_camera_id`, keyed by camera UUID.

        This is the query that removes 95-98% of candidate comparisons
        before any embedding is compared. See candidate_cameras() in
        database/migrations/0005.
        """
        # Casts are load-bearing, not decoration. candidate_cameras() is
        # declared (UUID, TIMESTAMPTZ, REAL, REAL, ...); psycopg sends a
        # Python str as `unknown` and a float as `double precision`, and
        # PostgreSQL then cannot resolve the overload at all -- it fails
        # with "function does not exist", which reads like a missing
        # migration rather than a type problem.
        rows = self.conn.execute(
            "SELECT camera_id::text AS cid, camera_ref, travel_s, window_start, "
            "       window_end, expected_at, source "
            "FROM candidate_cameras(%s::uuid, %s::timestamptz, %s::real, %s::real)",
            (from_camera_id, seen_at, max_travel_s, clock_confidence)).fetchall()
        return {r["cid"]: GateWindow(
            to_camera_id=r["cid"], camera_ref=r["camera_ref"], travel_s=r["travel_s"],
            window_start=r["window_start"], window_end=r["window_end"],
            expected_at=r["expected_at"], source=r["source"]) for r in rows}

    # ── open vehicles ────────────────────────────────────────────────
    def open_vehicles(self, ttl_seconds: int = 900,
                      limit: int = 4000,
                      reachable_from: list[str] | None = None) -> list[OpenVehicle]:
        """Vehicles recent enough to still be trackable.

        `reachable_from` is the set of camera UUIDs in the current batch.
        When supplied, the adjacency graph is applied IN THE QUERY, so only
        vehicles that could physically have produced one of these sightings
        come back. This is the same spatio-temporal gate the scorer uses,
        pushed down to where it is cheapest: at 50 cameras it cuts the
        result set several-fold, and at district scale it is the difference
        between a bounded query and reading every live vehicle in the state
        on every batch.

        Bounded by time AND count regardless, because an unbounded
        candidate set turns every tick into a growing scan as the day wears
        on.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        params: list[Any] = [cutoff, cutoff]
        gate_clause = ""
        if reachable_from:
            gate_clause = """
              AND EXISTS (SELECT 1 FROM camera_adjacency a
                          WHERE a.from_camera = vs.camera_id
                            AND a.to_camera = ANY(%s::uuid[]))"""
            params.append(reachable_from)
        params.append(limit)

        rows = self.conn.execute(f"""
            SELECT v.id::text AS vid, v.vehicle_track_id, v.last_seen,
                   v.vehicle_type::text, v.vehicle_color, v.best_plate,
                   v.embedding_model, v.camera_count,
                   v.sighting_count, v.is_watchlisted,
                   vs.camera_id::text AS last_camera_id,
                   vs.camera_ref AS last_camera_ref,
                   vs.sighting_id AS last_sighting_id
            FROM vehicle v
            JOIN LATERAL (
                SELECT camera_id, camera_ref, sighting_id
                FROM vehicle_sighting s
                WHERE s.vehicle_track_id = v.vehicle_track_id
                  AND s.timestamp > %s
                ORDER BY s.timestamp DESC LIMIT 1
            ) vs ON TRUE
            WHERE v.last_seen > %s{gate_clause}
            ORDER BY v.last_seen DESC
            LIMIT %s""", params).fetchall()

        out: list[OpenVehicle] = []
        for r in rows:
            # Embeddings are fetched later, only for the handful of vehicles
            # the gate admits. Parsing 512 floats for every open vehicle
            # costs ~100 ms per batch once a few thousand are live, and
            # >90% of that work is thrown away unread.
            out.append(OpenVehicle(
                vehicle_id=r["vid"], vehicle_track_id=r["vehicle_track_id"],
                last_seen=r["last_seen"], last_camera_id=r["last_camera_id"],
                last_camera_ref=r["last_camera_ref"],
                last_sighting_id=r["last_sighting_id"],
                vehicle_type=r["vehicle_type"], vehicle_color=r["vehicle_color"],
                best_plate=r["best_plate"], embedding=None,
                embedding_model=r["embedding_model"],
                camera_count=r["camera_count"], sighting_count=r["sighting_count"],
                is_watchlisted=r["is_watchlisted"]))
        return out

    def count_open_vehicles(self, ttl_seconds: int = 900,
                            limit: int = 4000) -> int:
        """How many vehicles are live, with NO adjacency gate applied.

        This exists to make the gate's reduction claim falsifiable. The gate
        is applied inside `open_vehicles()` as a SQL pushdown, so by the
        time the matcher sees a candidate list the saving has already
        happened and cannot be measured from it. PART 17 asks for the
        before-and-after in comparisons, and the "before" is precisely this
        number: what an ungated implementation would have had to score every
        sighting against.

        It is a bounded count over the same time window and the same LIMIT
        the real query uses, so it prices the counterfactual honestly rather
        than against an unbounded table scan an ungated system would not
        have written either.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
        row = self.conn.execute("""
            SELECT count(*) AS n FROM (
                SELECT 1 FROM vehicle v
                WHERE v.last_seen > %s
                ORDER BY v.last_seen DESC
                LIMIT %s) t""", (cutoff, limit)).fetchone()
        return int(row["n"]) if row else 0

    def embeddings_for(self, vehicle_track_ids: list[str]) -> dict[str, Any]:
        """Fetch embeddings for a specific set of vehicles.

        Called after the gate has narrowed the candidate set, so this reads
        tens of rows rather than thousands.
        """
        if not vehicle_track_ids:
            return {}
        # Serve what we already have; fetch only the rest. A vehicle's
        # embedding changes only when it is re-seen, so re-parsing 512
        # floats for the same vehicle on every batch was measured at 28% of
        # matcher runtime.
        out: dict[str, Any] = {}
        missing = []
        for vid in vehicle_track_ids:
            cached = self._embedding_cache.get(vid)
            if cached is not None:
                out[vid] = cached
            else:
                missing.append(vid)
        if not missing:
            return out

        rows = self.conn.execute(
            "SELECT vehicle_track_id, embedding::text AS emb FROM vehicle "
            "WHERE vehicle_track_id = ANY(%s) AND embedding IS NOT NULL",
            (missing,)).fetchall()
        for r in rows:
            raw = r["emb"]
            if not raw:
                continue
            try:
                if _np is not None:
                    vec = _np.fromstring(raw.strip("[]"), sep=",", dtype=_np.float32)
                    if vec.size == 0:
                        continue
                else:                                   # pragma: no cover
                    vec = [float(x) for x in raw.strip("[]").split(",") if x.strip()]
            except ValueError:
                continue
            out[r["vehicle_track_id"]] = vec
            self._embedding_cache[r["vehicle_track_id"]] = vec

        if len(self._embedding_cache) > 20_000:
            self._embedding_cache.clear()
        return out

    def find_by_plate(self, canonical: str, since: datetime) -> list[dict]:
        """Vehicles whose best plate canonicalises to the same string.

        Uses the canonical (confusion-collapsed) form so an O/0 or 8/B
        misread still finds the vehicle. This is the one association path
        that does NOT require the spatio-temporal gate: a matching plate is
        identity evidence, and a vehicle can legitimately reappear after a
        gap far longer than any travel-time window.
        """
        return self.conn.execute("""
            SELECT v.id::text AS vid, v.vehicle_track_id, v.last_seen, v.best_plate
            FROM vehicle v
            WHERE v.best_plate IS NOT NULL
              AND plate_canon(v.best_plate) = %s
              AND v.last_seen > %s
            ORDER BY v.last_seen DESC LIMIT 10""", (canonical, since)).fetchall()

    # ── writes ───────────────────────────────────────────────────────
    def insert_sightings(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO vehicle_sighting
                  (sighting_id, timestamp, first_seen, last_seen, camera_id, camera_ref,
                   vehicle_track_id, track_id, vehicle_type, type_confidence,
                   vehicle_color, color_confidence, plate_raw, plate_normalized,
                   plate_confidence, plate_valid_fmt, embedding, embedding_model,
                   latitude, longitude, geom, heading_deg, speed_kmph,
                   detection_count, quality_score, clock_confidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""", rows)
        return len(rows)

    def insert_plate_reads(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO plate_read
                  (timestamp, camera_id, camera_ref, sighting_id, vehicle_track_id,
                   raw_plate, normalized_plate, canonical_plate, confidence,
                   valid_format, corrected, plate_width_px, state_code,
                   latitude, longitude)
                VALUES (%s,%s,%s,%s,%s,%s,%s,plate_canon(%s),%s,
                        plate_valid_in(%s),%s,%s,plate_state_code(%s),%s,%s)""", rows)
        return len(rows)

    def upsert_vehicle(self, *, vehicle_track_id: str, sighting: Sighting,
                       camera_uuid: str, is_new: bool) -> None:
        """Create or extend a vehicle identity.

        Named parameters with explicit casts throughout. Positional %s in a
        CASE expression leaves PostgreSQL unable to infer the type when the
        value is NULL, and it fails with "could not determine data type of
        parameter $3" -- which gives no hint at all about which column is
        at fault.

        `camera_count` is recomputed from distinct cameras rather than
        incremented, because a vehicle re-entering the same camera must not
        inflate it. camera_count >= 2 is the signal the entire cross-camera
        story rests on, so it has to mean exactly what it says.
        """
        plate = sighting.plate
        params = {
            "vtid": vehicle_track_id,
            "first_seen": sighting.first_seen,
            "last_seen": sighting.last_seen,
            "vtype": sighting.vehicle_type.value,
            "colour": sighting.vehicle_color,
            "plate": plate.normalized_plate if plate else None,
            "plate_conf": plate.confidence if plate else None,
            "has_plate": 1 if plate else 0,
            "embedding": str(sighting.embedding) if sighting.embedding else None,
            "emodel": sighting.embedding_model,
        }

        if is_new:
            self.conn.execute("""
                INSERT INTO vehicle (vehicle_track_id, first_seen, last_seen,
                    sighting_count, camera_count, vehicle_type, vehicle_color,
                    best_plate, best_plate_conf, plate_read_count, embedding,
                    embedding_model)
                VALUES (%(vtid)s::text, %(first_seen)s::timestamptz,
                        %(last_seen)s::timestamptz, 1, 1,
                        %(vtype)s::vehicle_type, %(colour)s::text,
                        %(plate)s::text, %(plate_conf)s::real,
                        %(has_plate)s::int, %(embedding)s::vector,
                        %(emodel)s::text)
                ON CONFLICT (vehicle_track_id) DO NOTHING""", params)
            return

        self.conn.execute("""
            UPDATE vehicle SET
                last_seen        = GREATEST(last_seen, %(last_seen)s::timestamptz),
                sighting_count   = sighting_count + 1,
                plate_read_count = plate_read_count + %(has_plate)s::int,
                -- Keep the strongest plate ever read for this vehicle. A
                -- clean daylight read must not be overwritten by a poor
                -- night one at the next camera.
                best_plate = CASE
                    WHEN %(plate)s::text IS NOT NULL
                     AND (best_plate_conf IS NULL
                          OR %(plate_conf)s::real > best_plate_conf)
                    THEN %(plate)s::text ELSE best_plate END,
                best_plate_conf = CASE
                    WHEN %(plate)s::text IS NOT NULL
                     AND (best_plate_conf IS NULL
                          OR %(plate_conf)s::real > best_plate_conf)
                    THEN %(plate_conf)s::real ELSE best_plate_conf END,
                vehicle_color   = COALESCE(vehicle_color, %(colour)s::text),
                embedding       = COALESCE(%(embedding)s::vector, embedding),
                embedding_model = COALESCE(%(emodel)s::text, embedding_model),
                camera_count    = (SELECT count(DISTINCT camera_id)
                                   FROM vehicle_sighting
                                   WHERE vehicle_track_id = %(vtid)s::text),
                updated_at = now()
            WHERE vehicle_track_id = %(vtid)s::text""", params)
        self._embedding_cache.pop(vehicle_track_id, None)

    def insert_track_links(self, links: list[TrackLink]) -> int:
        if not links:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO track_link
                  (vehicle_track_id, from_sighting_id, to_sighting_id,
                   from_camera_id, to_camera_id, timestamp, decision, score_total,
                   score_plate, score_reid, score_color, score_type,
                   score_spatiotemporal, travel_expected_s, travel_actual_s, reasons)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(l.vehicle_track_id, l.from_sighting_id, l.to_sighting_id,
                  l.from_camera_id, l.to_camera_id, l.timestamp, l.decision.value,
                  l.score_total, l.score_plate, l.score_reid, l.score_color,
                  l.score_type, l.score_spatiotemporal, l.travel_expected_s,
                  l.travel_actual_s, l.reasons) for l in links])
        return len(links)

    def refresh_vehicle_paths(self, vehicle_track_ids: Iterable[str]) -> None:
        """Rebuild the LINESTRING M trajectory for vehicles that moved.

        Recomputed rather than appended so a corrected or rejected
        association is reflected instead of leaving a phantom leg on the map.
        """
        ids = list(vehicle_track_ids)
        if not ids:
            return
        self.conn.execute("""
            UPDATE vehicle v SET
                path = sub.path,
                total_distance_m = sub.dist
            FROM (
                SELECT vs.vehicle_track_id,
                       ST_MakeLine(ST_SetSRID(ST_MakePointM(
                           vs.longitude, vs.latitude,
                           EXTRACT(EPOCH FROM vs.timestamp)), 4326)
                           ORDER BY vs.timestamp) AS path,
                       ST_Length(ST_MakeLine(ST_SetSRID(ST_MakePoint(
                           vs.longitude, vs.latitude), 4326)
                           ORDER BY vs.timestamp)::geography) AS dist
                FROM vehicle_sighting vs
                WHERE vs.vehicle_track_id = ANY(%s)
                  AND vs.latitude IS NOT NULL
                GROUP BY vs.vehicle_track_id
                HAVING count(*) >= 2
            ) sub
            WHERE v.vehicle_track_id = sub.vehicle_track_id""", (ids,))

    def insert_health(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO camera_health
                  (timestamp, camera_id, camera_ref, reachable, fps_actual,
                   frames_decoded, decode_errors, scene_change, mean_luma,
                   blur_variance, latency_ms, clock_offset_ms, inference_ms, queue_depth, message)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        return len(rows)

    def update_camera_status(self, camera_ref: str, *, online: bool,
                             message: str | None = None) -> None:
        self.conn.execute("""
            UPDATE camera SET
                status = CASE WHEN %s THEN 'ONLINE'::camera_status
                              ELSE 'OFFLINE'::camera_status END,
                last_seen = CASE WHEN %s THEN now() ELSE last_seen END,
                consecutive_failures = CASE WHEN %s THEN 0
                                            ELSE consecutive_failures + 1 END,
                last_error = %s,
                -- Trust decays fast on failure and recovers slowly. A camera
                -- that flaps should not be treated as reliable between flaps.
                trust_score = CASE WHEN %s THEN LEAST(1.0, trust_score + 0.02)
                                   ELSE GREATEST(0.0, trust_score - 0.15) END
            WHERE camera_id = %s""",
            (online, online, online, message, online, camera_ref))

    def insert_alerts(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO alert
                  (alert_id, timestamp, alert_type, severity, title, message,
                   camera_id, camera_ref, camera_name, vehicle_track_id,
                   sighting_id, plate, latitude, longitude, geom, confidence,
                   evidence, dedup_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s,%s,%s)
                ON CONFLICT DO NOTHING""", rows)
        return len(rows)

    def active_watchlist(self) -> list[dict]:
        return self.conn.execute("""
            SELECT id::text AS wid, label, plate_query, plate_canonical,
                   seed_embedding, vehicle_type::text, vehicle_color,
                   severity::text, case_ref
            FROM watchlist
            WHERE is_active AND (expires_at IS NULL OR expires_at > now())""").fetchall()

    def alert_rules(self) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT code, name, alert_type::text, severity::text, params, "
            "       dedup_seconds, is_enabled FROM alert_rule WHERE is_enabled").fetchall()
        return {r["code"]: r for r in rows}

    def restricted_zones(self) -> list[dict]:
        return self.conn.execute(
            "SELECT id::text AS lid, code, name FROM location "
            "WHERE restricted AND geom IS NOT NULL").fetchall()

    def zones_containing(self, lat: float, lon: float) -> list[dict]:
        return self.conn.execute("""
            SELECT code, name FROM location
            WHERE restricted AND geom IS NOT NULL
              AND ST_Covers(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography)""",
            (lon, lat)).fetchall()

    def bump_watchlist_hit(self, watchlist_id: str) -> None:
        self.conn.execute(
            "UPDATE watchlist SET hit_count = hit_count + 1, last_hit_at = now() "
            "WHERE id = %s::uuid", (watchlist_id,))

    def stale_cameras(self, seconds: int = 90, min_failures: int = 3) -> list[dict]:
        return self.conn.execute("""
            SELECT camera_id, name, latitude, longitude, consecutive_failures
            FROM camera
            WHERE status <> 'DISABLED'
              AND consecutive_failures >= %s
              AND (last_seen IS NULL OR last_seen < now() - make_interval(secs => %s))""",
            (min_failures, seconds)).fetchall()

    def ensure_partitions(self) -> int:
        rows = self.conn.execute("SELECT count(*) AS n FROM ensure_partitions()").fetchone()
        return int(rows["n"]) if rows else 0
