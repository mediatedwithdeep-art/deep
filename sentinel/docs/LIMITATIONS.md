# Known Limitations

Stating these makes the work more credible, not less. Every one is real, and
anyone who has deployed this class of system already knows them — a
submission that claims otherwise invites being disproved in one question.

---

## Accuracy

### Night ANPR on wide-angle cameras is poor

**37.9% end-to-end** ([BENCHMARKS.md](BENCHMARKS.md)). A general
surveillance camera has no useful illumination at plate distance. This is
optics, not tuning.

*What we do about it:* appearance and attributes carry the track when the
plate cannot be read, and night sightings are marked lower-confidence rather
than reported as equal.

### Below ~55 px of plate width, nothing works

At 58 px the end-to-end read rate is **11.2%**. A wide-angle junction camera
at 30 m simply does not resolve a plate. Setting `anpr_capable: true` does
not change the physics; it only wastes GPU on unreadable crops.

*What we do about it:* the quality gate refuses those crops, and Camera
Health reports how much of the estate is plate-blind (74% of the demo
estate).

### Similar vehicles are genuinely confusable

Two white hatchbacks at 720p under sodium lighting produce embeddings that
overlap: **66 of 800** hard negatives score above the weakest true match.

*What we do about it:* appearance matches are capped at **PROBABLE** and can
never auto-confirm. They surface for operator confirmation with their full
score breakdown. Only a plate match auto-confirms.

### Fake, obscured or missing plates defeat ANPR entirely

No CV system solves this. A deliberately obscured plate is unreadable by
design.

*What we do about it:* nothing, honestly. Appearance matching is the only
recourse, and it is weaker. This is a real gap.

---

## Coverage

### 50 cameras do not tile a city

Tracks have holes. A vehicle can leave the estate and re-enter minutes later
somewhere unexpected.

*What we do about it:* the road-network graph gives arrival windows for
downstream cameras, so an operator knows where to look. We never draw an
unobserved segment as if it were observed.

### The adjacency graph must be built, and is only as good as its input

If `build_adjacency.py` has not run, the gate is empty and appearance
matching runs against every camera — the false-positive rate goes from
usable to unusable. Camera positions and headings must be accurate; a camera
recorded 200 m from where it stands corrupts every travel-time estimate
through it.

### Clock drift on legacy DVRs can invert apparent direction

A DVR whose RTC has drifted 40 seconds can make a vehicle appear at camera B
*before* camera A.

*What we do about it:* frames are stamped with the gateway's NTP-disciplined
clock, never the camera's; every sighting carries `clock_confidence`, and low
confidence widens the gate rather than producing a false negative.

---

## Scale

### These numbers are the simulation backend's

Real models need weights, a GPU, and re-measurement on your own footage. The
binding constraint moves from this loop to **NVDEC and GPU**: roughly 30–45
sub-streams per T4-class GPU. The ~380-585-cameras-per-core figure is an upper
bound for the simulation path only.

### Domain shift is real

A model trained on VeRi-776 or COCO degrades on your cameras. COCO has no
`auto_rickshaw` class at all, and auto-rickshaws are a large share of Indian
traffic. A few thousand annotated frames from your own cameras will beat any
amount of generic pretraining.

*What we do about it:* operator verdicts on PROBABLE links are recorded as a
labelled training set, so the system can improve after deployment instead of
staying frozen at its hackathon accuracy. The retraining loop itself is not
built.

### Matcher cost grows with live vehicle count

At ~2,500 live vehicles, mean match time reaches ~135 ms per batch. Three
optimisations took it from 186 ms to 49 ms at 1,700 vehicles (gate-as-index,
vectorised cosine, lazy embeddings), but the growth is not eliminated.

*The real answer is sharding by district*, which the architecture supports
and this MVP does not exercise.

---

## Deployment gaps

These are genuinely not built, and would be needed before production:

| Gap | Consequence |
|---|---|
| **Rate limiting is in-process** | N API replicas allow N× the configured limit. Must move to Redis before scaling out. Called out in code and in the K8s README. |
| **No WS-Security on ONVIF** | Cameras requiring WS-UsernameToken digest need `onvif-zeep`. HTTP Digest works for most. |
| **No video recording tier** | Evidence export is designed and schema'd; the edge ring buffer that feeds it is not implemented. |
| **No BSA §63 certificate generation** | Schema and hash chain exist; the certificate renderer does not. |
| **Single PostgreSQL** | No replication or PITR. This database holds evidence — use a managed PostGIS with point-in-time recovery. |
| **No Keycloak / SSO** | Auth is self-contained JWT. Department-level federation needs an OIDC provider. |
| **No VAHAN / CCTNS integration** | An ANPR hit is only useful if it resolves to a registered owner. Schema anticipates it; the adapter is not written. |
| **Frontend has no automated tests** | Verified by driving a real browser and asserting rendered content; no unit tests. |

---

## Security caveats we chose to accept

**Tokens are held in `localStorage`.** XSS-readable. httpOnly cookies would
be better but need same-origin plus CSRF handling. Stated in
`frontend/src/lib/api.ts` rather than left to be discovered.

**The WebSocket token travels in the query string.** Browsers cannot set an
Authorization header on a WebSocket handshake. The token is short-lived, but
it can appear in proxy access logs — put the endpoint behind a proxy
configured not to log query strings.

**PBKDF2 rather than argon2id.** Weaker per unit of work, chosen to avoid a C
extension in the container build. 260,000 iterations, FIPS-approved, and the
iteration count is recorded in the hash so it upgrades transparently on next
login.

---

## What we would fix first with another week

1. Shard the matcher by district and prove it at 3,000 cameras.
2. Move rate limiting to Redis.
3. Fine-tune a detector on real Ahmedabad footage with the `auto_rickshaw`
   class, and re-measure everything.
4. Build the edge ring buffer and the BSA §63 certificate renderer, so
   evidence export is real rather than designed.
5. Wire the operator-verdict data into a weekly fusion-weight retune.
