# Security Review — Adversarial Test of Department Isolation

**Date:** 2026-09-01 · **Scope:** authorisation boundary of the Sentinel API
**Method:** authorised self-attack against a live two-department estate on
PostgreSQL 16 + PostGIS + pgvector. Every finding below was proved by an
exploit that ran, not by reading code.

---

## Why this review happened at all

The system claimed, in `README.md` and in the demo script, that department
isolation was *"enforced in every query — 25 tests hold it."*

That claim was false, and the 25 tests were the reason nobody noticed. All
25 attacked `/cameras`. The camera registry was scoped correctly. Nothing
downstream of it was — and everything downstream of a camera inherits that
camera's department: who was seen, where they went, what was alerted on,
which vehicles are being watched, and under which case.

Scoping the registry is not scoping the estate.

---

## Findings

Severity uses the operational consequence for a police deployment, not a
generic CVSS band.

| # | Surface | Class | Severity | Status |
|---|---|---|---|---|
| 1 | `GET /vehicles/search` | Cross-tenant read | **CRITICAL** | Fixed |
| 2 | `GET /vehicles/{id}` | Cross-tenant read | **CRITICAL** | Fixed |
| 3 | `GET /vehicles/{id}/timeline` | Cross-tenant read (movement history) | **CRITICAL** | Fixed |
| 4 | `GET /vehicles/{id}/track.geojson` | Cross-tenant read | **CRITICAL** | Fixed |
| 5 | `GET /vehicles/{id}/next-cameras` | Cross-tenant read | HIGH | Fixed |
| 6 | `POST /vehicles/similar` | Cross-tenant read | HIGH | Fixed |
| 7 | `GET /vehicles/links/pending` | Cross-tenant read | HIGH | Fixed |
| 8 | `POST /vehicles/links/{id}/verdict` | Cross-tenant **write** | **CRITICAL** | Fixed |
| 9 | `GET /sightings` | Cross-tenant read | **CRITICAL** | Fixed |
| 10 | `GET /sightings/live.geojson` | Cross-tenant read | **CRITICAL** | Fixed |
| 11 | `GET /alerts` | Cross-tenant read | HIGH | Fixed |
| 12 | `POST /alerts/{id}/ack` | Cross-tenant **write** | **CRITICAL** | Fixed |
| 13 | `GET /alerts/summary` | Cross-tenant aggregate | MEDIUM | Fixed |
| 14 | `GET /watchlist` | Cross-tenant read (case metadata) | **CRITICAL** | Fixed |
| 15 | `GET /system/audit` | Cross-tenant read (audit trail) | **CRITICAL** | Fixed |
| 16 | `GET /analytics/cameras` | Cross-tenant inventory disclosure | HIGH | Fixed |
| 17 | `GET /analytics/vehicle-mix` | Cross-tenant aggregate | MEDIUM | Fixed |
| 18 | `WSS /ws` broadcast | Cross-tenant live feed | **CRITICAL** | Fixed |

### The two that matter most

**#12 — an operator could acknowledge another department's CRITICAL alert.**
This was not a read leak. A Surat operator could mark an Ahmedabad
stolen-vehicle hit as `FALSE_POSITIVE`, and the owning department would
see a handled alert nobody in it had handled. Proved:

```
LEAK: dept A acknowledged dept B's CRITICAL alert -> 200
{"alert_id":"VICTIM-ALERT-B","state":"ACKNOWLEDGED"}
```

**#15 — the audit log was estate-wide.** The control that exists to detect
misuse was itself the disclosure: any investigator could read which
vehicles every other department's officers had looked up, and the stated
purpose they gave for doing it. This one passed a first, isolated test run
and only failed once the full suite had populated the log with other
departments' activity — a reminder that an isolation test on an estate with
nothing to isolate proves nothing.

### What was already sound

These were attacked and held, and are recorded so the review is not read as
a list of only what broke:

- JWT verification pins the algorithm; `alg: none` and algorithm confusion
  are rejected, and a forged token fails.
- `SECRET_KEY` has no usable default and the service refuses to start in
  production without one.
- Passwords: PBKDF2-HMAC-SHA256 at 260,000 iterations, constant-time
  comparison, per-user salt, transparent rehash on iteration bump.
- Login: account lockout, timing equalisation against username
  enumeration, one error message for every failure mode, denials audited.
- WebSocket handshake authenticates and closes 4401 on an invalid token.
- No SQL injection. Every dynamic statement builds column names from
  hard-coded literals and passes values as parameters; no `ORDER BY` is
  caller-controlled.
- Unknown roles fail closed to `VIEWER`; `AUDITOR` is deliberately off the
  privilege ladder.

---

## The fix

Three composable scope helpers in `backend/app/deps.py`, applied at the
query rather than in the handler:

- `dept_scope_sighting(user, alias)` — a sighting belongs to the department
  that owns the camera that produced it.
- `dept_scope_vehicle(user, alias)` — a vehicle is visible if one of the
  caller's own cameras saw it.
- `dept_scope_alert(user, alias)` — as sightings, and an alert with no
  camera is unattributable and therefore state-admin only.

Three properties were deliberate:

**Writes are scoped in the `UPDATE`, not checked first.** A check-then-write
leaves a window in which the row changes department between the two
statements. `/alerts/{id}/ack` and `/links/{id}/verdict` both carry the
scope in their `WHERE`.

**Absence is reported as 404, never 403.** A 403 confirms the record exists
in another department, which is a disclosure an operator can harvest by
enumeration.

**A boundary-crossing vehicle is visible to both departments, but each sees
only its own hops.** Both genuinely observed it, so both may know it
exists; neither learns the other's camera positions or timings. The
GeoJSON track drops the connecting path along with the withheld points,
because a line drawn through them redraws exactly what was withheld.

The WebSocket now carries the token's department onto the connection and
filters every broadcast. An event whose camera cannot be attributed to a
department reaches only the state admin — the live channel is not permitted
to be the hole that the REST layer closed.

---

## Verification

```
tests/test_isolation_regression.py     13 attacks, all defended
tests/test_security_regression.py      26 tests
full suite                            359 passed, 0 failed
scripts/verify_p0.py                   10 PASS · 0 FAIL · 0 NOT RUN
```

Each of the 13 new tests performs the access a department A user should not
be able to perform and asserts refusal, so a regression reads as a breach
rather than as a diff. `verify_p0.py` question 9 now runs both isolation
suites rather than the camera suite alone.

---

## Honest limits of this review

- **Scope was the authorisation boundary.** Not reviewed here: dependency
  CVEs, container escape, the media server, TLS termination, or the
  reverse-proxy configuration the WebSocket token workaround depends on.
- **The rate limiter remains in-process.** With N API replicas the
  effective limit is N times the configured one. This is stated in the
  code and unchanged by this review; it needs Redis before multi-replica
  deployment.
- **No penetration test against a deployed instance.** Everything here ran
  against the application in-process over ASGI, on a local database. A
  deployed system has network surface this review did not touch.
- **The event processor and ingestion paths were not attacked.** They do
  not serve user requests, but they do write the data these queries scope.
