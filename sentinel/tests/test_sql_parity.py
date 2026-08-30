"""Guards against SQL and Python plate logic drifting apart.

`plate_canon()` / `plate_valid_in()` in db/migrations/002_gating.sql exist so
the database can prefilter candidate sightings before Python ever sees them.
If the two implementations diverge, SQL silently drops rows that the Python
matcher would have matched -- a failure that produces no error, just a
quietly worse recall. Hence this test.

Run: python3 sentinel/services/cv/test_sql_parity.py
"""
import os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from sentinel_core import plate_rules as pr

SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "database", "migrations", "0003_spatial_functions.sql")


def _sql() -> str:
    with open(SQL_PATH) as f:
        return f.read()


def test_state_codes_match():
    m = re.search(r"\^\(\(([A-Z|]+)\)", _sql())
    assert m, "could not locate the state-code alternation in plate_valid_in()"
    sql_codes = set(m.group(1).split("|"))
    assert sql_codes == set(pr.STATE_CODES), (
        f"only in SQL: {sorted(sql_codes - set(pr.STATE_CODES))}, "
        f"only in Python: {sorted(set(pr.STATE_CODES) - sql_codes)}")


def test_plate_canon_translation_tables_match():
    m = re.search(
        r"translate\(upper\(regexp_replace\(COALESCE\(p,''\), "
        r"'\[\^A-Za-z0-9\]', '', 'g'\)\),\s*'([A-Z]+)',\s*'([A-Z0-9]+)'", _sql())
    assert m, "could not locate the translate() call in plate_canon()"
    src, dst = m.group(1), m.group(2)
    # Mirror of sql_canonical() in plate_rules.py
    assert (src, dst) == ("OIBSZGDQU", "018526OO0"), \
        f"SQL plate_canon maps {src}->{dst}; plate_rules.sql_canonical disagrees"
    # Behavioural check on the Python side
    for a, b in [("GJ01AB1234", "GJ0IAB1234"), ("O0IL", "001L"), ("B8", "88")]:
        assert pr.sql_canonical(a) == pr.sql_canonical(b), f"{a} vs {b}"


def test_python_grammar_accepts_what_sql_accepts():
    accepted = ["GJ01AB1234", "MH12DE1433", "DL8CAF5030", "22BH1234AA"]
    rejected = ["XX01AB1234", "GJ01AB", "", "1234"]
    m = re.search(r"~\s*\n?\s*'(\^\(\(.+?\)\$)'", _sql(), re.S)
    assert m, "could not locate the plate grammar regex in plate_valid_in()"
    sql_re = re.compile(m.group(1))
    for p in accepted:
        n = pr.normalize(p)
        assert sql_re.match(n), f"SQL rejects {p} which Python accepts"
        assert pr.is_valid(p), f"Python rejects {p}"
    for p in rejected:
        n = pr.normalize(p)
        assert not sql_re.match(n), f"SQL accepts {p} which should be rejected"
        assert not pr.is_valid(p), f"Python accepts {p}"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)
