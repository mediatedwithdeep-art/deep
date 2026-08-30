"""Ahmedabad road topology for the demo estate.

ONE definition drives three things:
  * where the 50 seed cameras sit,
  * the camera adjacency graph (the spatio-temporal gate),
  * the routes the demo vehicle simulator drives.

Keeping them in one file is what makes the demo coherent: a vehicle that
leaves the Satellite junction heading east genuinely arrives at the next
camera east of it, after a travel time consistent with the road distance.
If these three were defined separately they would drift, and the demo would
show vehicles teleporting -- which is exactly the failure the gate exists
to prevent.

Coordinates are real Ahmedabad junctions (WGS84). Approximate to a few tens
of metres, which is well within what a demo needs and honest about not
being a survey.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Junction:
    code: str
    name: str
    lat: float
    lon: float
    zone: str


@dataclass(frozen=True)
class Road:
    a: str
    b: str
    name: str
    # Typical speed in km/h. Arterials move faster than inner-city roads,
    # and the gate's travel-time windows are only as good as this number.
    speed_kmph: float = 30.0
    lanes: int = 2


# ── Junctions (west Ahmedabad, Sabarmati crossings, and the old city) ──
JUNCTIONS: list[Junction] = [
    Junction("J01", "Jodhpur Cross Roads",        23.02705, 72.51192, "Satellite"),
    Junction("J02", "Shivranjani Cross Roads",    23.02243, 72.52915, "Satellite"),
    Junction("J03", "Nehrunagar Circle",          23.02060, 72.54180, "Ambawadi"),
    Junction("J04", "Panjrapole Cross Roads",     23.02730, 72.54690, "Ambawadi"),
    Junction("J05", "IIM Cross Roads",            23.03270, 72.54990, "Vastrapur"),
    Junction("J06", "Vastrapur Lake",             23.03760, 72.52640, "Vastrapur"),
    Junction("J07", "Judges Bungalow Road",       23.03310, 72.51890, "Bodakdev"),
    Junction("J08", "Sindhu Bhavan Road",         23.04350, 72.50450, "Bodakdev"),
    Junction("J09", "Thaltej Cross Roads",        23.05230, 72.50700, "Thaltej"),
    Junction("J10", "Gurukul Cross Roads",        23.04620, 72.53150, "Memnagar"),
    Junction("J11", "Drive-In Cross Roads",       23.05040, 72.54610, "Memnagar"),
    Junction("J12", "Helmet Circle",              23.05930, 72.53480, "Memnagar"),
    Junction("J13", "Gujarat University Circle",  23.03610, 72.54680, "Navrangpura"),
    Junction("J14", "Commerce Six Roads",         23.03210, 72.55870, "Navrangpura"),
    Junction("J15", "Stadium Cross Roads",        23.04350, 72.56480, "Navrangpura"),
    Junction("J16", "Naranpura Cross Roads",      23.05390, 72.55620, "Naranpura"),
    Junction("J17", "Income Tax Circle",          23.04180, 72.57430, "Ashram Road"),
    Junction("J18", "Ashram Road / Gandhi Bridge",23.03270, 72.57330, "Ashram Road"),
    Junction("J19", "Nehru Bridge West",          23.02460, 72.57330, "Ashram Road"),
    Junction("J20", "Ellis Bridge",               23.02180, 72.57180, "Ellisbridge"),
    Junction("J21", "Paldi Cross Roads",          23.01180, 72.56660, "Paldi"),
    Junction("J22", "Vasna Circle",               23.00190, 72.55440, "Vasna"),
    Junction("J23", "Anjali Cross Roads",         23.00600, 72.54250, "Vasna"),
    Junction("J24", "Shyamal Cross Roads",        23.00300, 72.52760, "Satellite"),
    Junction("J25", "Prahladnagar Garden",        23.00760, 72.50840, "Prahladnagar"),
    Junction("J26", "Iskcon Cross Roads",         23.02760, 72.50700, "Satellite"),
    Junction("J27", "Sarkhej Circle",             22.99000, 72.49770, "Sarkhej"),
    Junction("J28", "Bopal Circle",               23.02030, 72.46610, "Bopal"),
    Junction("J29", "Gota Cross Roads",           23.10190, 72.54470, "Gota"),
    Junction("J30", "Chandkheda Circle",          23.10650, 72.58880, "Chandkheda"),
    Junction("J31", "Sabarmati Ashram",           23.06070, 72.58020, "Sabarmati"),
    Junction("J32", "Delhi Darwaja",              23.03730, 72.58940, "Old City"),
    Junction("J33", "Lal Darwaja",                23.02490, 72.58060, "Old City"),
    Junction("J34", "Kalupur Railway Station",    23.02690, 72.60040, "Kalupur"),
    Junction("J35", "Sarangpur Bridge",           23.02150, 72.59450, "Old City"),
    Junction("J36", "Astodia Darwaja",            23.01750, 72.58890, "Old City"),
    Junction("J37", "Maninagar Cross Roads",      22.99820, 72.60130, "Maninagar"),
    Junction("J38", "Kankaria Lake",              23.00450, 72.60130, "Maninagar"),
    Junction("J39", "Isanpur Cross Roads",        22.97730, 72.59440, "Isanpur"),
    Junction("J40", "Narol Circle",               22.96700, 72.57810, "Narol"),
    Junction("J41", "Vatva GIDC",                 22.97000, 72.62200, "Vatva"),
    Junction("J42", "Odhav Circle",               23.02350, 72.65180, "Odhav"),
    Junction("J43", "Naroda Circle",              23.06900, 72.65090, "Naroda"),
    Junction("J44", "Nikol Cross Roads",          23.04600, 72.66200, "Nikol"),
    Junction("J45", "Airport Circle",             23.07480, 72.62660, "Hansol"),
    Junction("J46", "Airport Terminal Approach",  23.07720, 72.63430, "Hansol"),
    Junction("J47", "Indira Bridge",              23.08500, 72.60340, "Airport Road"),
    Junction("J48", "Motera Stadium",             23.09220, 72.59720, "Motera"),
    Junction("J49", "Vaishnodevi Circle",         23.12160, 72.51660, "SG Highway N"),
    Junction("J50", "Science City Road",          23.07840, 72.50170, "Sola"),
]

JUNCTION_BY_CODE = {j.code: j for j in JUNCTIONS}

# ── Roads. Speeds reflect what the road actually does at typical hours. ──
ROADS: list[Road] = [
    # SG Highway spine (fast arterial, north-south on the west side)
    Road("J27", "J25", "SG Highway", 55), Road("J25", "J26", "SG Highway", 55),
    Road("J26", "J01", "SG Highway", 50), Road("J01", "J07", "SG Highway", 50),
    Road("J07", "J08", "SG Highway", 55), Road("J08", "J09", "SG Highway", 55),
    Road("J09", "J50", "SG Highway", 60), Road("J50", "J49", "SG Highway", 60),
    Road("J49", "J29", "SG Highway", 55),

    # 132ft Ring Road
    Road("J01", "J02", "132ft Ring Road", 40), Road("J02", "J03", "132ft Ring Road", 40),
    Road("J03", "J04", "132ft Ring Road", 35), Road("J04", "J05", "132ft Ring Road", 35),
    Road("J05", "J13", "132ft Ring Road", 35), Road("J13", "J10", "132ft Ring Road", 40),
    Road("J10", "J11", "132ft Ring Road", 40), Road("J11", "J16", "132ft Ring Road", 40),
    Road("J16", "J12", "132ft Ring Road", 40),

    # Vastrapur / Bodakdev connectors
    Road("J06", "J07", "Vastrapur Link", 35), Road("J06", "J05", "Vastrapur Link", 35),
    Road("J06", "J10", "Vastrapur Link", 35), Road("J02", "J06", "Vastrapur Link", 35),
    Road("J09", "J12", "Thaltej Link", 40), Road("J12", "J29", "Gota Link", 45),

    # Navrangpura / Ashram Road (dense, slower)
    Road("J13", "J14", "University Road", 30), Road("J14", "J15", "Stadium Road", 30),
    Road("J15", "J17", "Stadium Road", 30), Road("J14", "J18", "Ashram Road", 28),
    Road("J17", "J18", "Ashram Road", 30), Road("J18", "J19", "Ashram Road", 28),
    Road("J19", "J20", "Ashram Road", 25), Road("J20", "J21", "Paldi Road", 28),
    Road("J16", "J15", "Naranpura Link", 35), Road("J12", "J16", "Naranpura Link", 35),

    # River crossings -- chokepoints, and the most valuable camera sites
    Road("J18", "J32", "Gandhi Bridge", 35), Road("J19", "J33", "Nehru Bridge", 32),
    Road("J31", "J48", "Sabarmati Riverfront", 40), Road("J17", "J31", "Riverfront Road", 40),
    Road("J47", "J48", "Indira Bridge", 50),

    # Old city (slowest)
    Road("J32", "J34", "Old City Road", 22), Road("J33", "J35", "Old City Road", 22),
    Road("J33", "J36", "Old City Road", 22), Road("J35", "J34", "Old City Road", 22),
    Road("J36", "J37", "Maninagar Road", 25), Road("J34", "J38", "Kankaria Road", 25),
    Road("J37", "J38", "Kankaria Road", 25),

    # South / east industrial
    Road("J21", "J22", "Vasna Road", 30), Road("J22", "J23", "Vasna Road", 30),
    Road("J23", "J24", "Shyamal Road", 32), Road("J24", "J02", "Shyamal Road", 32),
    Road("J24", "J25", "Prahladnagar Road", 35),
    Road("J37", "J39", "Isanpur Road", 35), Road("J39", "J40", "Narol Road", 40),
    Road("J40", "J27", "Narol-Sarkhej Highway", 50),
    Road("J39", "J41", "Vatva Road", 38), Road("J41", "J42", "Odhav Road", 40),
    Road("J42", "J44", "Nikol Road", 38), Road("J44", "J43", "Naroda Road", 40),
    Road("J42", "J43", "Odhav-Naroda Road", 40),
    Road("J43", "J45", "Airport Road", 45), Road("J45", "J46", "Airport Approach", 35),
    Road("J45", "J47", "Airport Road", 45), Road("J47", "J30", "Chandkheda Road", 45),
    Road("J30", "J29", "Chandkheda Road", 45), Road("J48", "J30", "Motera Road", 40),
    Road("J31", "J16", "Sabarmati Link", 38),
    Road("J34", "J42", "Odhav Link", 35),
    Road("J28", "J27", "Bopal Road", 45), Road("J28", "J26", "Bopal-Iskcon Road", 45),
]


@dataclass
class RoadGraph:
    junctions: dict[str, Junction] = field(default_factory=dict)
    neighbours: dict[str, list[tuple[str, Road]]] = field(default_factory=dict)

    @classmethod
    def build(cls) -> "RoadGraph":
        g = cls(junctions=dict(JUNCTION_BY_CODE))
        for j in JUNCTIONS:
            g.neighbours[j.code] = []
        for r in ROADS:
            # Undirected: real roads carry traffic both ways, and a
            # one-way-only graph would make the gate reject legitimate
            # reverse transitions.
            g.neighbours[r.a].append((r.b, r))
            g.neighbours[r.b].append((r.a, r))
        return g

    def edge_length_m(self, a: str, b: str) -> float:
        from sentinel_core.geo import haversine_m
        ja, jb = self.junctions[a], self.junctions[b]
        # Straight line between junctions understates the driven distance;
        # 1.15 is a mild correction for a graph whose nodes are already the
        # junctions themselves.
        return haversine_m(ja.lat, ja.lon, jb.lat, jb.lon) * 1.15

    def travel_time_s(self, a: str, b: str, road: Road) -> float:
        return self.edge_length_m(a, b) / (road.speed_kmph * 1000 / 3600)

    def shortest_paths(self, source: str, max_seconds: float = 900.0
                       ) -> dict[str, tuple[float, float]]:
        """Dijkstra by travel time. Returns {code: (seconds, metres)}.

        This is what stands in for OSRM when no routing server is available:
        the demo estate has its own road graph, so travel times are real
        rather than crow-flight guesses.
        """
        import heapq
        dist: dict[str, tuple[float, float]] = {source: (0.0, 0.0)}
        pq: list[tuple[float, float, str]] = [(0.0, 0.0, source)]
        seen: set[str] = set()
        while pq:
            t, d, node = heapq.heappop(pq)
            if node in seen:
                continue
            seen.add(node)
            for nxt, road in self.neighbours.get(node, []):
                if nxt in seen:
                    continue
                nt = t + self.travel_time_s(node, nxt, road)
                nd = d + self.edge_length_m(node, nxt)
                if nt > max_seconds:
                    continue
                if nxt not in dist or nt < dist[nxt][0]:
                    dist[nxt] = (nt, nd)
                    heapq.heappush(pq, (nt, nd, nxt))
        dist.pop(source, None)
        return dist


def bearing_between(a: str, b: str) -> float:
    from sentinel_core.geo import bearing_deg
    ja, jb = JUNCTION_BY_CODE[a], JUNCTION_BY_CODE[b]
    return bearing_deg(ja.lat, ja.lon, jb.lat, jb.lon)
