"""The account recovery tool must stay compatible with login.

scripts/admin_unlock.py exists because the seeded password is printed once,
into a container log, and five wrong guesses then lock the account for
fifteen minutes with no supported way back in.

The failure mode worth guarding is quiet: if the tool ever hashed a
password differently from backend/app/security.py, it would report success,
write a well-formed row, and the account still would not authenticate. The
only symptom is "invalid credentials" on a password you just set. These
tests assert the tool and the login path agree.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys

import pytest

from app.security import hash_password, verify_password

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "admin_unlock.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("admin_unlock", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["admin_unlock"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


@pytest.fixture
def locked_user(db):
    """An account locked exactly the way five bad logins lock one."""
    db.execute("INSERT INTO department (code,name) VALUES ('UNLOCK_T','Test') "
               "ON CONFLICT (code) DO NOTHING")
    db.execute("""INSERT INTO app_user (username, full_name, password_hash,
                      role, department_id, failed_logins, locked_until)
                  SELECT 'locktest','Lock Test',%s,'ADMIN'::user_role,d.id,5,
                         now() + interval '15 minutes'
                  FROM department d WHERE d.code='UNLOCK_T'
                  ON CONFLICT (username) DO UPDATE
                    SET failed_logins = 5,
                        locked_until  = now() + interval '15 minutes',
                        password_hash = EXCLUDED.password_hash,
                        is_active     = TRUE""",
               (hash_password("OriginalPass123"),))
    return "locktest"


def _state(db, username):
    cur = db.execute("SELECT failed_logins, locked_until, password_hash "
                     "FROM app_user WHERE username=%s", (username,))
    return cur.fetchone()


def test_unlock_clears_both_the_counter_and_the_deadline(db, locked_user):
    """Clearing locked_until but leaving failed_logins at 5 would re-lock the
    account on the very next typo, which is not what an operator asked for."""
    failed, until, _ = _state(db, locked_user)
    assert failed == 5 and until is not None, "fixture did not lock the account"

    with db.cursor() as cur:
        assert tool.unlock(cur, locked_user, everyone=False) == 0
    db.commit()

    failed, until, _ = _state(db, locked_user)
    assert failed == 0
    assert until is None


def test_a_reset_password_actually_authenticates(db, locked_user):
    """The drift guard: what the tool writes, verify_password must accept."""
    with db.cursor() as cur:
        assert tool.set_password(cur, locked_user, "BrandNewPass456") == 0
    db.commit()

    _, _, stored = _state(db, locked_user)
    assert verify_password("BrandNewPass456", stored)
    assert not verify_password("OriginalPass123", stored)


def test_a_reset_also_lifts_the_lock(db, locked_user):
    """Setting a password on a locked account and leaving it locked would
    send the operator round the loop a second time."""
    with db.cursor() as cur:
        tool.set_password(cur, locked_user, "BrandNewPass456")
    db.commit()

    failed, until, _ = _state(db, locked_user)
    assert failed == 0 and until is None


def test_a_password_the_api_would_reject_is_refused(db, locked_user):
    """The tool enforces the same policy as the change-password endpoint, so
    it cannot set a credential the application itself would not accept."""
    _, _, before = _state(db, locked_user)

    with db.cursor() as cur:
        assert tool.set_password(cur, locked_user, "short") != 0
    db.rollback()

    _, _, after = _state(db, locked_user)
    assert after == before, "a rejected password must change nothing"


def test_an_unknown_account_is_reported_not_invented(db):
    with db.cursor() as cur:
        assert tool.unlock(cur, "no-such-officer", everyone=False) != 0
        assert tool.set_password(cur, "no-such-officer", "BrandNewPass456") != 0
    db.rollback()

    cur = db.execute("SELECT count(*) FROM app_user WHERE username=%s",
                     ("no-such-officer",))
    assert cur.fetchone()[0] == 0


def test_the_reset_is_audited_without_recording_the_password(db, locked_user):
    """An out-of-band credential change on an evidence system that leaves no
    trace is precisely what an auditor comes looking for -- and the password
    itself must never be what gets left behind."""
    with db.cursor() as cur:
        tool.set_password(cur, locked_user, "BrandNewPass456")
    db.commit()

    cur = db.execute(
        "SELECT action, resource_id, detail::text FROM audit_log "
        "WHERE username='admin_unlock.py' AND action='PASSWORD_RESET' "
        "ORDER BY timestamp DESC LIMIT 1")
    row = cur.fetchone()
    assert row is not None, "the reset wrote no audit row"
    assert row[1] == locked_user
    assert "BrandNewPass456" not in row[2]

    cur = db.execute("SELECT count(*) FROM audit_log WHERE detail::text "
                     "LIKE %s", ("%BrandNewPass456%",))
    assert cur.fetchone()[0] == 0
