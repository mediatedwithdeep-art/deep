"""Tests for Indian plate grammar, correction and fuzzy matching.

Run: python3 -m pytest sentinel/services/cv/test_plate_rules.py -q
     (or plain `python3 test_plate_rules.py` — it self-runs without pytest)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plate_rules as pr


def test_normalize():
    assert pr.normalize("GJ 01 AB 1234") == "GJ01AB1234"
    assert pr.normalize("gj-01-ab-1234") == "GJ01AB1234"
    assert pr.normalize("IND GJ01AB1234") == "INDGJ01AB1234"
    assert pr.normalize(None) == ""


def test_valid_formats():
    assert pr.is_valid("GJ01AB1234")
    assert pr.is_valid("MH12DE1433")
    assert pr.is_valid("DL8CAF5030")
    assert pr.is_valid("GJ1AB1234")        # single-digit RTO
    assert pr.is_valid("22BH1234AA")       # Bharat series
    assert not pr.is_valid("XX01AB1234")   # XX is not a state code
    assert not pr.is_valid("GJ01AB")       # no serial
    assert not pr.is_valid("")


def test_parse_components():
    p = pr.parse("GJ27XY0987")
    assert (p.state, p.rto, p.series, p.serial) == ("GJ", "27", "XY", "0987")
    assert p.kind == "standard"


def test_lexicon_correction_recovers_ocr_confusions():
    # O->0 in the RTO block, Z->2 in the serial: both grammatically impossible
    # where they landed, so the grammar tells us how to fix them.
    assert pr.correct("GJO1AB1Z34").normalized == "GJ01AB1234"
    assert pr.correct("GJO1AB1Z34").corrected is True
    # 0->O in the state code
    assert pr.correct("6J01AB1234").normalized == "GJ01AB1234"
    # already valid -> untouched
    c = pr.correct("GJ01AB1234")
    assert c.normalized == "GJ01AB1234" and c.corrected is False


def test_correction_does_not_invent_plates():
    # Nothing grammatical is reachable; must not fabricate a match.
    assert pr.correct("XXXXXXXXXX").valid is False
    assert pr.correct("!!!").valid is False


def test_confusable_substitutions_cost_less_than_real_ones():
    d_confusable = pr.weighted_distance("GJ01AB1234", "GJ01AB1284")   # 3<->8
    d_real       = pr.weighted_distance("GJ01AB1234", "GJ01AB1274")   # 3<->7
    assert d_confusable < d_real


def test_match_bands():
    # Exact
    assert pr.match("GJ01AB1234", "GJ01AB1234").band == "exact"
    # Correctable OCR noise resolves to exact via the lexicon
    assert pr.match("GJ01AB1234", "GJO1AB1Z34").band == "exact"
    # A genuinely different plate must not match
    m = pr.match("GJ01AB1234", "MH12CD9876")
    assert m.matched is False and m.band == "none"
    # One uncorrectable real error -> probable, not confident
    m = pr.match("GJ01AB1234", "GJ01AB1734")
    assert m.band in ("probable", "confident") and m.matched


def test_match_scores_are_monotone():
    exact = pr.match("GJ01AB1234", "GJ01AB1234").score
    near  = pr.match("GJ01AB1234", "GJ01AB1734").score
    far   = pr.match("GJ01AB1234", "MH12CD9876").score
    assert exact > near > far


def test_low_ocr_confidence_is_forgiven_but_not_unboundedly():
    hi = pr.match("GJ01AB1234", "GJ01AB1734", char_confs=[0.99] * 10).distance
    lo = pr.match("GJ01AB1234", "GJ01AB1734", char_confs=[0.30] * 10).distance
    assert lo < hi                      # low confidence -> more forgiving
    # ...but a garbage read still cannot match an unrelated plate
    assert pr.match("GJ01AB1234", "MH12CD9876", char_confs=[0.01] * 10).matched is False


def test_sql_canonical_matches_sql_translate():
    # Must stay in step with plate_canon() in db/migrations/002_gating.sql.
    # Divergence would make SQL prefilters silently drop matchable rows.
    assert pr.sql_canonical("GJ01AB1234") == pr.sql_canonical("GJ0IAB1234")
    assert pr.sql_canonical("O0IL") == pr.sql_canonical("001L")


def test_empty_and_none_are_safe():
    assert pr.match("", "GJ01AB1234").matched is False
    assert pr.match("GJ01AB1234", "").matched is False
    assert pr.correct(None).valid is False


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)
