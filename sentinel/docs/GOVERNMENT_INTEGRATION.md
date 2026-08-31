# Government Record-System Integration

**VAHAN · SARTHI · eGujCop · AFIS · NAFIS**

---

## 0 · Read this first: nothing is connected

**No government record system is connected in this deployment.** No
credential, endpoint or authorisation for any of the five was available to
this project, and none was sought.

Every adapter ships with a **mock backend** that fabricates records
deterministically from the query string. Every record it returns is stamped
`provenance=MOCK`, and that stamp travels with the record into the alert,
the API response, the UI and the audit log.

```bash
$ curl -s localhost:8000/api/v1/intelligence/status | jq '.any_real_integration, .notice'
false
"No government record system is connected in this deployment. Every record
 below is generated locally for demonstration and describes no real person
 or vehicle."
```

This is a first-class API endpoint rather than a line in a document, because
an operator should not have to have read a document to know what they are
looking at.

### Why the real clients are not written

A plausible-looking VAHAN client, written against an API specification
nobody on this project has read, would produce code that appears
integrated, passes its own tests, and fails on first contact — while making
the submission look more complete than it is. The honest artefact is
`RealBackend`, which raises and names exactly what is missing:

| System | What real access actually requires |
|---|---|
| **VAHAN** | MoRTH/NIC VAHAN 4.0 API agreement, a registered integrator identity, an IP allow-listed gateway |
| **SARTHI** | MoRTH/NIC SARATHI API agreement; personal-data access additionally requires a DPDP-compliant purpose registration |
| **eGujCop** | Gujarat Police CCTNS integration approval and a state-network (GSWAN) route |
| **AFIS** | Gujarat State FSL authorisation; biometric queries are examiner-initiated and cannot be automated from a VMS |
| **NAFIS** | NCRB NAFIS authorisation; same examiner-initiated constraint |

None of these is a configuration value. Each is an institutional process.

**Status: PENDING EXTERNAL ACCESS** for all five.

---

## 1 · Turning one system real

```bash
SENTINEL_GOVT_REAL=VAHAN,EGUJCOP
```

The default is mock for every system, and the direction matters: a default
of "real" would mean a misconfiguration silently produced
`AUTHORITATIVE`-stamped records from a backend nobody had verified. An
unknown system name fails at startup rather than being ignored.

Implementing a real backend means writing one method:

```python
class VahanBackend:
    provenance = Provenance.AUTHORITATIVE
    def fetch(self, query: str, *, timeout_s: float) -> dict | None: ...
```

Everything else — authorisation, timeout, quota, audit, minimisation,
provenance — is enforced by `GovtAdapter` around that call, so no
individual backend can forget one of them.

---

## 2 · API abstraction

```
GovtAdapter.lookup(query, purpose, actor, case_ref) -> GovtRecord | None
```

One shape for five very different systems, because the things that must be
true of a lookup are the same in all five cases and are individually easy
to forget.

```
        caller
          │  purpose + actor + case_ref
          ▼
    ┌─────────────────────────────────────────┐
    │ GovtAdapter                             │
    │  1. purpose is served by this system?   │  → AdapterError
    │  2. case_ref present if personal data?  │  → AdapterError
    │  3. local quota has a token?            │  → RateLimited
    │  4. backend.fetch(timeout)              │  → UpstreamTimeout
    │                                         │  → AuthorizationRequired
    │  5. project to the purpose's fields     │  ← minimisation happens HERE
    │  6. stamp provenance                    │
    │  7. audit                               │
    └─────────────────────────────────────────┘
          │
          ▼
    GovtRecord(frozen)   system, provenance, fields, purpose, retrieved_at
```

`GovtRecord` is frozen. Re-labelling mock data as authoritative requires
constructing a new object, which is greppable, rather than assigning an
attribute.

---

## 3 · Data minimisation

**The single most important property in this file.** DPDP Act 2023 makes
purpose limitation a legal obligation, not a preference, and a system that
fetches everything and filters at the UI has already processed everything.

Each lookup names a **purpose**, and the purpose selects the fields:

| Purpose | Released by VAHAN | Justification |
|---|---|---|
| `WATCHLIST_SCREENING` | `registration_number`, `registration_status`, `is_stolen`, `blacklist_status` | Deciding whether to raise an alert. This runs for **every vehicle that passes a camera**. |
| `VEHICLE_VERIFICATION` | + make, model, colour, class, fuel, year, RTO | Confirming the vehicle matches what the camera saw. |
| `REGISTERED_INVESTIGATION` | + **owner name, address, chassis, engine**, insurance, PUC | A named case. **Requires `case_ref`.** |

The consequence worth stating plainly: **a false-positive alert cannot
expose a citizen who was never relevant.** Screening a plate that turns out
to be a misread returns a status flag and nothing more; there is no code
path by which the owner's home address is retrieved in order to decide
whether to raise an alert.

Minimisation happens inside `lookup()`, before the `GovtRecord` exists.
Nothing downstream is ever handed the full upstream payload and trusted to
filter it for itself.

### Systems that have no screening purpose

`SARTHI`, `AFIS` and `NAFIS` serve **only** `REGISTERED_INVESTIGATION`.

A camera reads a plate, never a driver. There is no traffic-camera workflow
that justifies pulling a licence record or a fingerprint search for every
vehicle that passes a junction, so the purpose does not exist and the call
raises rather than being merely discouraged.

### Fingerprint systems never identify

AFIS and NAFIS return a candidate count, a score, and
`decision = REFER_TO_EXAMINER`. Never a name.

A fingerprint match is an examiner's determination in Indian practice. A
VMS printing `IDENTIFIED: <name>` from an algorithmic score would be
manufacturing evidence.

---

## 4 · Authentication and authorisation

Two separate layers, and both apply.

**Our API → the caller.** Route permissions:

| Endpoint | Permission | Reasoning |
|---|---|---|
| `GET /intelligence/status` | `vehicle:read` | Which systems are connected is not sensitive. |
| `POST /intelligence/screen` | `watchlist:read` | Queries a government system. Not a read of our own data, so not the lowest role. |
| `GET /intelligence/{system}/{query}` | `evidence:export` + `X-Reason` | The path that can release personal data. Belongs to investigators, not to everyone with a login. |

`REGISTERED_INVESTIGATION` additionally requires `case_ref`. The audit trail
must be able to answer *"under which case was this citizen's address
retrieved?"* years later; without a reference it cannot, so the lookup does
not happen.

**Us → the upstream system.** Per-system credentials held in the secret
store and never in the database, matching the rule already enforced for
camera credentials. `RealBackend` is where they would be read; there is no
column anywhere in this schema that can hold one.

---

## 5 · Timeouts

| System | Budget | Why |
|---|---|---|
| VAHAN, SARTHI, eGujCop | 3 s | A plate lookup that takes three seconds is broken. |
| AFIS, NAFIS | 15 s | A fingerprint search is genuinely slow. |

A timeout raises `UpstreamTimeout`, is counted in `stats.timeouts`, and is
audited as `TIMEOUT`. It never returns an empty record, because **"no hit"
and "the system was down" must never look the same to an operator deciding
whether to stop a vehicle.**

---

## 6 · Rate limiting

Enforced on **our** side of the call, per system:

| System | Calls/min |
|---|---|
| VAHAN, eGujCop | 120 |
| SARTHI | 30 |
| AFIS, NAFIS | 10 |

Government systems publish quotas and withdraw access from integrators that
exceed them. An ANPR storm — one misread plate generating the same lookup
fifty times a second — must exhaust a token bucket here rather than the
integration agreement.

Exceeding the bucket raises `RateLimited` (HTTP 429) and is audited.

---

## 7 · Failure handling

| Condition | Exception | HTTP | Retryable? |
|---|---|---|---|
| Not authorised in this deployment | `AuthorizationRequired` | **501** | **No** |
| Upstream did not answer | `UpstreamTimeout` | 504 | Yes |
| Our quota exhausted | `RateLimited` | 429 | Later |
| Purpose not served / no case_ref | `AdapterError` | 422 | No |

**501, not 503, for an unauthorised system.** A 5xx that implies an outage
would have an operator retrying all shift for access this deployment has
never had.

Screening degrades rather than failing: if VAHAN cannot answer, eGujCop is
still consulted and the reason VAHAN could not answer is returned in
`degraded`, alongside the hits. The operator sees both.

```json
{
  "plate": "GJ05XY9999",
  "hits": [ ... ],
  "degraded": {"VAHAN": "not authorised in this deployment: ..."},
  "contains_real_data": false
}
```

---

## 8 · Audit

Every lookup — hit, miss, timeout, rate-limit, refusal — is written to the
same `audit_log` as everything else in the system, not to an
integration-specific log nobody reviews. A query against a citizen's
registration record is at least as sensitive as viewing a camera.

Recorded: system, query, purpose, provenance, result, actor, case reference,
latency.

Two properties:

- **A refused lookup is audited too.** A refusal that leaves no record is
  one an attacker can retry indefinitely without ever appearing in a
  review.
- **An audit-sink failure never fails the lookup it is recording.** A broken
  audit table must not take the estate down with it — but the failure is
  logged loudly rather than swallowed.

---

## 9 · The demonstrated path

```
camera frame
   → ANPR read + confidence
   → lexicon-constrained plate correction   (plate_rules.correct)
   → confidence floor 0.72                  ← below this, NO lookup happens
   → screen_plate()
        → VAHAN    WATCHLIST_SCREENING      (status flag only)
        → eGujCop  WATCHLIST_SCREENING      (wanted flag only)
   → rule: is_stolen → STOLEN_VEHICLE / CRITICAL
   → alert, carrying provenance to the operator's screen
```

**The confidence floor is the part worth defending.** OCR on a 720p
sub-stream is wrong often enough that screening every read would query a
citizen's registration record on the strength of a misread character. The
floor is applied *before* the call, and a test asserts the adapter's call
counter stays at zero for a weak read.

Correction runs first: a single O/0 confusion would otherwise query a plate
that does not exist and return a confident "no record" for a vehicle that
has one.

Run it:

```bash
curl -s -XPOST localhost:8000/api/v1/intelligence/screen \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"plate":"GJ05XY9999","confidence":0.95}' | jq
```

---

## 10 · What is tested

`tests/test_govt_integration.py` — 35 tests. Mostly **negative**: an
integration that returns data is easy to demonstrate; one that cannot be
talked into returning a citizen's address to a false-positive alert is the
one worth having.

- Every mock record is stamped `MOCK`; provenance cannot be assigned away
- Screening releases a status flag and no personal data
- Verification releases the vehicle but not the owner
- Personal data requires a registered investigation **with** a case reference
- The full upstream payload never reaches the caller
- SARTHI / AFIS / NAFIS cannot be screened against passing traffic
- A fingerprint system never returns an identification
- Local quota is enforced; timeouts are raised, not swallowed
- A degraded system is reported as degraded, not as "no hit"
- Every lookup is audited with purpose and actor; refusals too
- An audit-sink failure never fails the lookup
- A low-confidence or ungrammatical read never reaches a government system
- An unauthorised system answers 501, not 503

---

## 11 · Limitations

- **Nothing has been tested against a real government endpoint.** Field
  names, response shapes and error semantics in the mock backends are
  assumptions.
- **The mock hit rates are invented.** ~6% stolen, ~5% wanted are chosen to
  make a demo show something. They are not a claim about Gujarat.
- **Mock names and addresses are drawn from a fixed ten-item list** and are
  obviously synthetic on inspection. Documents are structurally valid but
  reserved-range values.
- **AFIS/NAFIS automation is likely not permissible at all.** Both are
  modelled as examiner-initiated. If a real deployment finds otherwise, the
  `REFER_TO_EXAMINER` decision should still stand.
