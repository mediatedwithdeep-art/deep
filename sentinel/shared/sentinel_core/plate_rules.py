"""
Indian number-plate grammar, lexicon-constrained correction, and
confusion-weighted fuzzy matching.

Why this module exists
----------------------
Plate OCR on a 720p surveillance sub-stream is not clean. Exact string
comparison throws away most true positives: a single O/0 confusion turns a
correct read into a miss. Two cheap techniques recover most of that loss:

  1. Lexicon-constrained correction. Indian plates follow a closed grammar
     with a closed set of ~37 state codes. Snapping OCR output to the
     nearest grammatical string is worth roughly +8-15 points of exact-match
     accuracy for a few dozen lines of code -- more than any model upgrade
     of comparable effort.

  2. Confusion-weighted edit distance. OCR errors are systematic, not
     random. O<->0 and 8<->B are common; O<->W is not. Weighting the
     substitution cost by whether the pair is a known confusion separates
     "same plate, misread" from "different plate" far better than plain
     Levenshtein.

No dependencies beyond the standard library, so this runs identically in the
edge pipeline and in the core matcher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# ─────────────────────────────────────────────────────────────────────────
# Grammar
# ─────────────────────────────────────────────────────────────────────────

STATE_CODES: frozenset[str] = frozenset({
    "AN", "AP", "AR", "AS", "BR", "CG", "CH", "DD", "DL", "DN", "GA", "GJ",
    "HP", "HR", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP",
    "MZ", "NL", "OD", "OR", "PB", "PY", "RJ", "SK", "TN", "TR", "TS", "UK",
    "UP", "WB",
})

# Standard:  GJ 01 AB 1234  -- state, RTO district, series, serial
_RE_STANDARD = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{0,3})(\d{1,4})$")
# Bharat series: 22 BH 1234 AA
_RE_BHARAT = re.compile(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$")
# Military: 09B 123456 A  (leading digits, service char, serial, check char)
_RE_MILITARY = re.compile(r"^(\d{2})([A-Z])(\d{6})([A-Z])$")

# Directional confusion maps. The grammar tells us whether a slot *should*
# be alphabetic or numeric, so correction is directional: a "0" read in the
# state-code slot is almost certainly an "O", and never the reverse.
_DIGIT_TO_ALPHA_MAP = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A"}
_ALPHA_TO_DIGIT_MAP = {"O": "0", "I": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "A": "4",
                       "D": "0", "Q": "0", "U": "0", "T": "7"}

# Symmetric confusion classes, with cost. Lower cost = more likely an OCR
# error rather than a genuinely different character.
_CONFUSION_COST: dict[frozenset[str], float] = {
    frozenset({"O", "0"}): 0.20,
    frozenset({"I", "1"}): 0.20,
    frozenset({"D", "0"}): 0.35,
    frozenset({"B", "8"}): 0.30,
    frozenset({"S", "5"}): 0.30,
    frozenset({"Z", "2"}): 0.30,
    frozenset({"G", "6"}): 0.35,
    frozenset({"Q", "O"}): 0.30,
    frozenset({"U", "V"}): 0.40,
    frozenset({"M", "N"}): 0.45,
    frozenset({"C", "G"}): 0.45,
    frozenset({"E", "F"}): 0.45,
    frozenset({"K", "X"}): 0.50,
    frozenset({"P", "R"}): 0.50,
    frozenset({"T", "7"}): 0.35,
    frozenset({"A", "4"}): 0.40,
    frozenset({"J", "1"}): 0.45,
    frozenset({"9", "4"}): 0.50,
    frozenset({"6", "8"}): 0.50,
    frozenset({"3", "8"}): 0.45,
}

SUB_COST_DEFAULT = 1.0
INDEL_COST = 1.0


def normalize(plate: str | None) -> str:
    """Uppercase, strip everything that is not A-Z0-9. IND, state emblems and
    hyphens all vanish, which is what we want before any comparison."""
    if not plate:
        return ""
    return re.sub(r"[^A-Z0-9]", "", plate.upper())


def sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return _CONFUSION_COST.get(frozenset({a, b}), SUB_COST_DEFAULT)


# ─────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlateParse:
    raw: str
    normalized: str
    valid: bool
    kind: str          # standard | bharat | military | invalid
    state: str = ""
    rto: str = ""
    series: str = ""
    serial: str = ""
    corrected: bool = False

    @property
    def canonical(self) -> str:
        return self.normalized


def parse(plate: str | None) -> PlateParse:
    """Parse without correcting. Use `correct()` for the OCR path."""
    raw = plate or ""
    n = normalize(raw)

    m = _RE_STANDARD.match(n)
    if m and m.group(1) in STATE_CODES:
        return PlateParse(raw, n, True, "standard", m.group(1), m.group(2), m.group(3), m.group(4))

    m = _RE_BHARAT.match(n)
    if m:
        return PlateParse(raw, n, True, "bharat", "BH", m.group(1), m.group(4), m.group(3))

    m = _RE_MILITARY.match(n)
    if m:
        return PlateParse(raw, n, True, "military", "IND", m.group(1), m.group(2), m.group(3))

    return PlateParse(raw, n, False, "invalid")


def is_valid(plate: str | None) -> bool:
    return parse(plate).valid


# ─────────────────────────────────────────────────────────────────────────
# Lexicon-constrained correction
# ─────────────────────────────────────────────────────────────────────────

def _coerce_alpha(s: str) -> str:
    return "".join(_DIGIT_TO_ALPHA_MAP.get(c, c) for c in s)


def _coerce_digit(s: str) -> str:
    return "".join(_ALPHA_TO_DIGIT_MAP.get(c, c) for c in s)


@lru_cache(maxsize=8192)
def correct(plate: str | None) -> PlateParse:
    """Snap raw OCR output to the nearest grammatically valid Indian plate.

    Strategy: the grammar tells us which slots are alphabetic and which are
    numeric, so we coerce per-slot using the directional confusion maps
    rather than guessing globally. Only accept the correction if the result
    is grammatical AND carries a real state code -- otherwise return the
    uncorrected parse and let the fuzzy matcher deal with it.
    """
    n = normalize(plate)
    if not n:
        return PlateParse(plate or "", "", False, "invalid")

    direct = parse(n)
    if direct.valid:
        return direct

    # --- Try the standard layout: AA DD [AAA] DDDD
    # Work out the split by scanning from both ends, since the series block
    # is variable-length (0-3 chars) and sometimes absent entirely.
    if 8 <= len(n) <= 10:
        state = _coerce_alpha(n[:2])
        if state in STATE_CODES:
            # trailing 4 (or fewer) must be the numeric serial
            for serial_len in (4, 3, 2):
                if len(n) < 4 + serial_len:
                    continue
                serial = _coerce_digit(n[-serial_len:])
                middle = n[2:-serial_len]
                # middle = RTO digits then series letters
                for rto_len in (2, 1):
                    if len(middle) < rto_len:
                        continue
                    rto = _coerce_digit(middle[:rto_len])
                    series = _coerce_alpha(middle[rto_len:])
                    cand = f"{state}{rto}{series}{serial}"
                    p = parse(cand)
                    if p.valid:
                        return PlateParse(plate or "", cand, True, "standard",
                                          state, rto, series, serial,
                                          corrected=(cand != n))

    # --- Try the Bharat layout: DD BH DDDD AA
    if len(n) == 10:
        cand = _coerce_digit(n[:2]) + _coerce_alpha(n[2:4]) + _coerce_digit(n[4:8]) + _coerce_alpha(n[8:])
        p = parse(cand)
        if p.valid:
            return PlateParse(plate or "", cand, True, "bharat", "BH",
                              cand[:2], cand[8:], cand[4:8], corrected=(cand != n))

    return PlateParse(plate or "", n, False, "invalid")


# ─────────────────────────────────────────────────────────────────────────
# Confusion-weighted distance and matching
# ─────────────────────────────────────────────────────────────────────────

def weighted_distance(a: str, b: str, positional: bool = True) -> float:
    """Levenshtein where substitution cost reflects OCR confusability.

    `positional` additionally down-weights errors in the state-code prefix,
    which is grammar-constrained and therefore more reliable than the
    free-form serial. A mismatch in the last four digits is much stronger
    evidence of a different vehicle than a mismatch in the first two chars.
    """
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return float(max(len(a), len(b)))

    la, lb = len(a), len(b)
    prev = [j * INDEL_COST for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [i * INDEL_COST]
        for j in range(1, lb + 1):
            c = sub_cost(a[i - 1], b[j - 1])
            if positional:
                # Serial block (last 4) weighted up; prefix weighted down.
                if i > la - 4 and j > lb - 4:
                    c *= 1.25
                elif i <= 2 and j <= 2:
                    c *= 0.75
            cur.append(min(
                prev[j] + INDEL_COST,       # deletion
                cur[j - 1] + INDEL_COST,    # insertion
                prev[j - 1] + c,            # substitution
            ))
        prev = cur
    return prev[lb]


@dataclass(frozen=True)
class PlateMatch:
    matched: bool
    distance: float
    band: str        # exact | confident | probable | none
    score: float     # 0-1, feeds the fusion score as `plate(a,b)`
    query: str
    candidate: str


# Thresholds tuned against the confusion costs above: a single high-confusion
# substitution (O/0) costs 0.20; two cost 0.40; one full substitution costs
# 1.0. So `confident` admits up to ~3 confusable errors, `probable` admits
# one genuine error or several confusable ones.
BAND_CONFIDENT = 0.60
BAND_PROBABLE = 1.50


def match(query: str, candidate: str,
          char_confs: list[float] | None = None) -> PlateMatch:
    """Compare a target plate against an OCR read.

    `char_confs` (per-character OCR confidence) lets us discount mismatches
    at characters the recogniser was already unsure about -- a mismatch at a
    0.4-confidence character is much weaker evidence than one at 0.99.
    """
    q = normalize(query)
    c = correct(candidate).normalized or normalize(candidate)

    if not q or not c:
        return PlateMatch(False, 99.0, "none", 0.0, q, c)

    if q == c:
        return PlateMatch(True, 0.0, "exact", 1.0, q, c)

    d = weighted_distance(q, c)

    if char_confs and len(char_confs) == len(normalize(candidate)):
        mean_conf = sum(char_confs) / len(char_confs)
        # Low-confidence reads get distance forgiveness, capped so a garbage
        # read cannot match everything.
        d *= max(0.55, mean_conf)

    if d <= BAND_CONFIDENT:
        return PlateMatch(True, d, "confident", 0.75 + 0.25 * (1 - d / BAND_CONFIDENT), q, c)
    if d <= BAND_PROBABLE:
        return PlateMatch(True, d, "probable",
                          0.50 * (1 - (d - BAND_CONFIDENT) / (BAND_PROBABLE - BAND_CONFIDENT)), q, c)
    return PlateMatch(False, d, "none", 0.0, q, c)


def sql_canonical(plate: str | None) -> str:
    """Mirror of the SQL `plate_canon()` function in 002_gating.sql.

    Collapses each confusion class to one representative so a plain equality
    or trigram index can find near-misses. Keep the two implementations in
    step -- if they diverge, SQL prefilters will silently drop candidates
    that this module would have matched.
    """
    n = normalize(plate)
    return n.translate(str.maketrans("OIBSZGDQU", "018526OO0"))
