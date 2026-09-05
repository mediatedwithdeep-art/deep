"""Vehicle attributes: colour and type.

Attributes are cheap (~1 ms) and carry real discriminative power for
cross-camera matching. Knowing a vehicle is {white, sedan} cuts the
candidate pool by roughly 15x before any embedding comparison.

The colour palette is deliberately coarse. Distinguishing "pearl white"
from "off white" is not achievable from a 720p night crop under sodium
lighting, and pretending otherwise produces confident nonsense.
"""

from __future__ import annotations

COLOURS = ["white", "silver", "grey", "black", "red", "blue",
           "green", "yellow", "brown", "orange", "other"]

# Reference RGB centroids. Chosen for *vehicle* appearance under real
# street lighting, not for pure colour-space values -- a black car in
# daylight photographs around (45,45,48), not (0,0,0).
_CENTROIDS: dict[str, tuple[int, int, int]] = {
    "white":  (225, 225, 228),
    "silver": (176, 178, 182),
    "grey":   (120, 122, 126),
    "black":  (45, 45, 48),
    "red":    (150, 38, 38),
    "blue":   (42, 66, 140),
    "green":  (44, 104, 62),
    "yellow": (210, 180, 50),
    "brown":  (110, 76, 50),
    "orange": (205, 110, 40),
}

# Colours that street lighting genuinely confuses. Sodium vapour turns
# white vehicles yellow and makes silver essentially unidentifiable, so the
# matcher must treat these disagreements as weak evidence, not proof.
CONFUSABLE: dict[frozenset[str], float] = {
    frozenset({"white", "silver"}): 0.75,
    frozenset({"silver", "grey"}): 0.80,
    frozenset({"grey", "black"}): 0.55,
    frozenset({"white", "yellow"}): 0.50,
    frozenset({"red", "orange"}): 0.60,
    frozenset({"blue", "black"}): 0.45,
    frozenset({"brown", "orange"}): 0.55,
    frozenset({"grey", "blue"}): 0.40,
}


def classify_rgb(r: float, g: float, b: float,
                 is_night: bool = False) -> tuple[str, float]:
    """Nearest-centroid colour classification with a confidence.

    Confidence is the margin between the best and second-best match, not
    the raw distance: a crop that sits halfway between silver and grey
    should report low confidence even though it is close to both.
    """
    dists = []
    for name, (cr, cg, cb) in _CENTROIDS.items():
        d = ((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2) ** 0.5
        dists.append((d, name))
    dists.sort()
    best_d, best = dists[0]
    second_d = dists[1][0]

    margin = (second_d - best_d) / max(second_d, 1.0)
    conf = max(0.05, min(1.0, margin * 2.2))
    # Night crops are systematically less reliable. Reporting a confident
    # colour at 02:00 from an IR frame would be a lie -- IR is monochrome.
    if is_night:
        conf *= 0.55
    if best_d > 130:
        return "other", min(conf, 0.3)
    return best, round(conf, 3)


def colour_similarity(a: str | None, b: str | None,
                      conf_a: float = 1.0, conf_b: float = 1.0) -> float:
    """Similarity in [0,1] for the fusion score.

    An unknown or low-confidence colour pulls toward 0.5 ("no information"),
    never toward 0.0 ("different vehicle"). Absence of evidence is not
    evidence of absence, and treating it as such loses real matches.
    """
    if not a or not b:
        return 0.5
    base = 1.0 if a == b else CONFUSABLE.get(frozenset({a, b}), 0.0)
    conf = min(conf_a, conf_b)
    return base * conf + 0.5 * (1.0 - conf)
