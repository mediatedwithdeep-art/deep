# Security & Compliance

This system watches public roads and stores video of identifiable people.
The security model is part of the design, not a hardening pass afterwards.

Things we chose **not** to do, and why, are in
[LIMITATIONS.md](LIMITATIONS.md#security-caveats-we-chose-to-accept).

---

## 1 · Camera credentials never reach the database

**No table in this schema can hold a camera password.** A test asserts it:

```python
def test_no_column_can_hold_a_camera_password(db):
    for forbidden in ("password", "passwd", "secret", "credentials", "auth_token"):
        assert forbidden not in camera_columns
    assert "credential_ref" in camera_columns
```

`camera.credential_ref` names a secret (`env:`, `file:`, `vault://`) that is
resolved at connect time and held in memory only. The API never returns it —
a second test greps the serialised camera response for anything replayable.

A database dump must not become a working set of credentials for the entire
state's camera estate.

Inline credentials in `config/cameras.yaml` are tolerated for a laptop demo
and **refused at startup** when `ENVIRONMENT=production`.

---

## 2 · Camera networks are unreachable from anywhere but their gateway

Legacy DVRs are the worst-maintained devices on a government network:
default credentials, end-of-life firmware with published unauthenticated
RCEs, and many already port-forwarded to the public internet.

```
VLAN 10 — cameras: no default gateway, no internet route
    │  only the edge gateway may cross
    ▼
EDGE GATEWAY: ip_forward = 0 · nftables default-deny both ways
    │  outbound only — WireGuard + mTLS
    ▼
STATE CORE: no inbound rules at any site
```

Outbound-only is the load-bearing control. It means no port forwarding, no
public DVR exposure, no inbound firewall rules to get wrong at 2,000 sites,
and no path from a compromised core back into a camera VLAN.

The shipped Kubernetes NetworkPolicy enforces the cluster half: only
ingestion pods may reach camera subnets, and only on 554/80/443. A
compromised web pod cannot scan a DVR.

---

## 3 · Authentication

| | |
|---|---|
| Password hashing | PBKDF2-HMAC-SHA256, 260,000 iterations, per-user salt |
| Iteration upgrades | recorded in the hash; re-hashed transparently on next login |
| Access tokens | JWT HS256, 60 min, unique `jti` per token |
| Refresh tokens | **rotate on use**, stored as SHA-256 digest only |
| Lockout | 5 failures → 15 minutes |
| Password change | revokes every other session for that user |

**Login failures are indistinguishable.** A wrong password and an unknown
username return the same status and the same message, and the unknown-user
path still spends the hashing time so response latency does not leak account
existence. Both paths are tested.

**Refresh rotation** makes a stolen token detectable: it is single-use, so
the legitimate holder's next refresh fails loudly rather than the attacker
riding along silently.

---

## 4 · Authorisation

Roles are an ordered `IntEnum`, so "at least OPERATOR" is expressible
without enumerating every role above it — which is where permission bugs
come from. An unrecognised role parses to `VIEWER`, never to privileged.

| Role | Can |
|---|---|
| VIEWER | read cameras, sightings, vehicles, alerts, analytics |
| OPERATOR | + acknowledge alerts, confirm probable matches |
| INVESTIGATOR | + watchlist, evidence export, audit read |
| ADMIN | + camera and rule management, users |

**Denied requests are written to the audit log, not merely refused.** An
attempt to reach data outside one's authority is exactly the event an audit
trail exists to capture.

---

## 5 · DPDP Act 2023 — purpose limitation

Video of identifiable people is personal data. Movement history is the most
sensitive thing this system produces, so it is gated at the edge rather than
trusted to callers:

```
GET /api/v1/vehicles/V-000123/timeline
X-Reason: FIR 0142/2026 vehicle movement enquiry     ← required
```

Without a reason the request is refused with 400. With one, the reason is
written to `audit_log` alongside the query, the user, the IP and the result.
**Reading the audit log is itself audited** — the first thing an insider does
is check what was recorded about them.

Retention is set per data class, and audit outlives everything:

| Data | Retention | Why |
|---|---|---|
| Per-frame detections | 3 days | Debug only; nothing in the UI reads them |
| Sightings | 90 days | A typical investigation window |
| Plate reads | 1 year | Evidentiary value |
| Alerts | 2 years | Case linkage |
| **Audit log** | **7 years** | Accountability outlives the data it describes |

Retention is enforced by `DROP PARTITION`, which is instant and leaves no
recoverable remnants — a `DELETE` would leave the rows on disk until vacuum.

---

## 6 · Evidentiary integrity — BSA 2023 §63

The Bharatiya Sakshya Adhiniyam 2023 §63 (which replaced Indian Evidence Act
§65B) requires a certificate accompanying electronic evidence.

`evidence_export` carries a **hash chain**:

```
chain_hash(n) = SHA256( chain_hash(n-1) ‖ content_sha256 ‖ metadata ‖ timestamp )
```

Append-only, so altering or removing any export breaks every subsequent
hash. Each row records the source camera, the exact window, the requesting
officer, the stated purpose, and the case reference.

The schema and the chain exist; **the certificate renderer does not yet** —
see [LIMITATIONS.md](LIMITATIONS.md#deployment-gaps).

---

## 7 · Application hardening

| Control | Where |
|---|---|
| Input validation | Pydantic models on every request body |
| SQL injection | parameterised queries throughout; no string interpolation of values |
| Rate limiting | per-identity, fixed window (**in-process — see below**) |
| Security headers | `nosniff`, `DENY`, `no-referrer` on every response |
| Error leakage | internal errors return a trace id, never a stack trace |
| Containers | non-root, dropped capabilities, read-only root filesystem |
| Secrets | no usable default; production refuses to start without `SECRET_KEY` or with a known-weak database password |
| Metrics cardinality | labelled by route template, never raw path |

**Rate limiting is per-replica.** N API replicas allow N × the configured
limit. It must move to Redis before scaling out; this is flagged in the code
and in the Kubernetes README rather than left to be discovered.

---

## 8 · Data localisation

No component reaches a foreign service at runtime:

- MapLibre with a configurable tile URL, not Mapbox (no token, no callback)
- no font CDN — the map deliberately avoids MapLibre's `glyphs` dependency
- no external model API; inference is local
- when the tile server is unreachable, overlays still render

The whole stack runs on NIC MeghRaj or the Gujarat State Data Centre with no
egress. This is tested: the browser verification in this repository ran with
the tile CDN blocked, and the map rendered.

---

## 9 · Threat model, briefly

| Threat | Control |
|---|---|
| Stolen database dump | No credentials stored; refresh tokens are digests; passwords are PBKDF2 |
| Compromised DVR | Isolated VLAN, no egress, gateway does not forward |
| Compromised core | No inbound path to any site |
| Insider surveillance abuse | Purpose required, every access audited for 7 years, audit reads audited |
| Credential stuffing | Lockout, indistinguishable failures, constant-time comparison |
| Stolen access token | 60-minute expiry, unique `jti` |
| Stolen refresh token | Single-use rotation makes it detectable |
| Evidence tampering | Hash-chained, append-only export ledger |
| Wrong vehicle stopped | Probable matches labelled, never auto-confirmed; evidence shown; fuzzy search says so |

That last row is a security control. A system that presents a probabilistic
appearance match as an established fact will eventually cause the wrong
person to be stopped, and no amount of TLS prevents that.
