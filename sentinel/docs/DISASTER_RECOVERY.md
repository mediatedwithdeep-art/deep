# Disaster Recovery

**Nine failure domains, with RPO, RTO, and what an operator sees at
1 minute, 10 minutes and 1 hour.**

---

## 0 · What is built, and what is designed

Stating this first, because a DR document that reads as if everything were
implemented is worse than none.

| | |
|---|---|
| **BUILT and tested** | Camera reconnection with backoff and state machine; scene-discontinuity recovery; per-worker fault isolation; migration checksum ledger; audit-write failure isolation; bus abstraction over memory/Redis |
| **DESIGNED, not built** | PostgreSQL replication and PITR; regional failover; edge store-and-forward; GPU worker draining; object-store replication; the RPO/RTO targets below |

The failure behaviour in the **camera and edge rows** is exercised by the
test suite. Everything at the regional and central tier is a plan.

---

## 1 · Targets

| Tier | RPO (data loss) | RTO (service restored) | Status |
|---|---|---|---|
| Camera / edge | 0 (buffered) | < 30 s | **BUILT** |
| Regional | 5 min | 15 min | DESIGNED |
| Central DB | 5 min | 30 min | DESIGNED |
| Central API | 0 | < 2 min | DESIGNED |
| Object store (clips) | 15 min | 1 h | DESIGNED |

**RPO 0 at the edge is a real claim and the only one here that is
tested.** A camera that drops mid-track still emits its in-flight vehicles
as sightings — `flush()` on disconnect — so the observation is not lost,
only interrupted.

---

## 2 · The nine domains

### 2.1 Camera failure — **BUILT**

One camera dies: cable cut, PoE fault, lens obscured, firmware crash.

| | |
|---|---|
| **Detection** | Reader marks RECONNECTING then OFFLINE after 3 consecutive failures; a *live but frozen* picture is caught separately by the scene-change EMA |
| **Blast radius** | One camera. Workers share no mutable state. |
| **RPO / RTO** | 0 / immediate — the estate does not wait for it |

- **1 min:** camera shows RECONNECTING; backoff 2→4→8→16→30 s with jitter.
- **10 min:** OFFLINE, estate health shows it, trust score falls. Alerts
  that depended on it are still raised by neighbouring cameras with a wider
  gate.
- **1 h:** unchanged; a field ticket is the only fix. **The system must not
  hide this** — roughly a fifth of a real government estate is broken at
  any moment, and a VMS that shows 100% health is lying.

*Tested:* `test_live_ingestion.py` — dropped stream reconnects and reports
RECONNECTING; unreachable camera goes OFFLINE without raising.

### 2.2 Edge node failure — DESIGNED

An edge box dies, taking 20–50 cameras with it.

| | |
|---|---|
| **Detection** | Health beacons stop; regional tier marks the group stale |
| **RPO** | Up to the local buffer's unflushed window (design target: 0, via write-ahead of metadata before acknowledgement) |
| **RTO** | Restart 2 min; cold replacement, hours |

- **1 min:** 20–50 cameras go stale together — a *correlated* failure,
  which the health view must distinguish from 20 unrelated ones, or an
  operator chases 20 tickets instead of one.
- **10 min:** cameras are re-homed to a neighbouring edge node if capacity
  exists, or run degraded from the regional tier over the WAN.
- **1 h:** if not restored, that coverage is simply absent, and the
  spatio-temporal gate widens accordingly — a vehicle can pass through the
  gap and reappear, so journeys inferred across it carry lower confidence.

### 2.3 Network / WAN outage — PARTLY BUILT

The link between a district and the centre fails.

| | |
|---|---|
| **Blast radius** | Cross-district correlation and central live view |
| **What keeps working** | Everything local: decode, detection, ANPR, local alerts |
| **RPO** | 0 with store-and-forward (DESIGNED) |
| **RTO** | Link restoration; queue drains |

- **1 min:** central map shows the district stale. Local operators see no
  change at all.
- **10 min:** metadata queues at the edge. Local alerting continues, which
  is the point of the hybrid model — a stolen vehicle in Rajkot is still
  detected in Rajkot.
- **1 h:** queue depth becomes the risk. At ~150 B/s/camera a 1,000-camera
  district queues ~540 MB/hour — days of buffer on any sane disk.

**The requirement this creates:** late-arriving sightings must carry
**PTS-derived capture time**, not arrival time, or a four-hour backlog
places every vehicle four hours late and the travel-time gate scores
fiction. This is why the PTS work was P0.

### 2.4 GPU worker failure — DESIGNED

An inference worker crashes or its GPU faults.

| | |
|---|---|
| **Detection** | Liveness probe; no sightings from its cameras |
| **RPO** | Frames in flight only — seconds |
| **RTO** | < 60 s, Kubernetes reschedules |

- **1 min:** its cameras produce no sightings. They are still decoding; the
  loss is analytics, not video.
- **10 min:** rescheduled onto a node with a free GPU, or its cameras are
  redistributed at reduced per-camera FPS across surviving workers.
- **1 h:** if GPU capacity is genuinely gone, the estate runs at reduced
  sampling — 3 fps instead of 6 — rather than dropping cameras entirely.
  **Degrade every camera slightly, never blind a district.**

### 2.5 Message bus failure — PARTLY BUILT

Redis / the event bus becomes unavailable.

| | |
|---|---|
| **Blast radius** | Sightings cannot reach the event processor |
| **RPO** | Design target 0 via local spool; today, in-flight messages are lost |
| **RTO** | < 5 min |

- **1 min:** producers spool locally (DESIGNED). Alerting stops.
- **10 min:** spool grows; if it fills, the oldest *low-value* sightings
  are dropped first — a sighting with no plate and a weak embedding is
  worth less than one with a confident plate, and that ordering must be
  explicit rather than FIFO.
- **1 h:** bus restored, spool drains. Alerts fire late, and **they must be
  visibly marked late** — an alert timestamped now for a vehicle seen an
  hour ago will otherwise send officers to a junction the car left long
  since.

### 2.6 Database failure — DESIGNED

PostgreSQL is unavailable or corrupt.

| | |
|---|---|
| **Current state** | Single instance, no replication, no PITR — stated in LIMITATIONS.md |
| **Design** | Streaming replica + WAL archiving; RPO 5 min, RTO 30 min |

- **1 min:** API returns 503 on database-backed routes; `/health` reports
  the database down. Ingestion and detection keep running and spool.
- **10 min:** promote the replica. Sightings from the spool replay.
- **1 h:** if the primary is unrecoverable and no replica exists — today's
  situation — the loss is every sighting since the last backup. **This is
  the single largest unmitigated risk in the system** and the honest
  statement is that the MVP has no answer to it.

**Partitioning helps recovery:** sighting tables are range-partitioned by
time, so a restore can bring back recent partitions first and backfill
older ones, rather than blocking on a whole-database restore.

### 2.7 API failure — DESIGNED

The API layer is down; everything behind it is healthy.

| | |
|---|---|
| **RPO** | 0 — the API holds no state |
| **RTO** | < 2 min |

- **1 min:** operators cannot log in or search. Ingestion, detection and
  alert *generation* continue — alerts are simply not visible yet.
- **10 min:** restarted or failed over. Stateless, N replicas behind a load
  balancer.
- **1 h:** unchanged. The rate limiter is in-process, so N replicas allow
  N× the limit until it moves to Redis — noted in the code, not fixed.

### 2.8 Storage failure — DESIGNED

The clip/evidence object store is unavailable.

| | |
|---|---|
| **RPO** | 15 min (replication lag) |
| **RTO** | 1 h |

- **1 min:** clip playback and evidence export fail. Live view is
  unaffected — it does not go through the store.
- **10 min:** exports queue. The **hash chain in the export ledger must not
  advance** for an export that did not complete, or the ledger claims
  custody of an artefact that does not exist.
- **1 h:** edge ring buffers still hold ~72 h, so nothing is permanently
  lost yet; recovery re-pulls from the edge.

### 2.9 Total regional failure — DESIGNED

An entire regional site is lost — power, fire, flood.

| | |
|---|---|
| **RPO** | 5 min to the centre |
| **RTO** | Hours |

- **1 min:** the centre loses a whole region. Edge nodes keep detecting and
  buffering.
- **10 min:** cameras re-home to an adjacent region if the WAN allows;
  cross-camera matching for the lost region stops.
- **1 h:** the region runs on edge-local alerting only. **Cross-camera
  correlation is the capability that is actually lost** — plate reads
  continue, journeys do not.

---

## 3 · Design properties that limit blast radius

Each of these is already in the system and each exists for a reason a DR
plan cares about.

| Property | What it prevents |
|---|---|
| Workers share no mutable state | One camera's fault cannot corrupt another's |
| A worker never raises into the supervisor | One bad frame cannot take down an estate |
| Audit writes fail open, loudly | A broken audit table cannot take the system down |
| Migration checksum ledger | A silently edited migration cannot be applied over a live schema |
| Time-range partitioning | Restore recent data first; drop old data cheaply |
| Bus abstraction | Memory backend for a single node, Redis for a fleet, without code change |
| Camera credentials in a secret store, never the DB | A database compromise does not hand over the estate |
| PTS-derived capture time | A late batch still places vehicles at the right moment |

---

## 4 · What would have to be built

In the order that reduces risk fastest:

1. **PostgreSQL streaming replication + WAL archiving.** The largest single
   gap (2.6). Without it, RPO is "since the last backup" and there is no
   backup schedule in the MVP.
2. **Local spool for the bus** (2.5), so a bus outage costs latency rather
   than data.
3. **Edge store-and-forward** (2.3), which makes the WAN-outage RPO of 0
   real rather than designed.
4. **Health-view correlation** (2.2), so 50 cameras failing together read
   as one edge-node incident.
5. **Late-alert marking** (2.5), so a delayed alert cannot send officers to
   a stale location.

---

## 5 · Honest limits

- **No failover has been executed.** Nothing in §2 beyond the camera and
  edge rows has been tested by inducing the failure. The RPO/RTO figures
  are targets, not observations.
- **No backup exists in the MVP.** There is no scheduled dump, no WAL
  archive and no restore drill.
- **The 72 h edge buffer is a design figure**, not a measurement; no edge
  appliance has been built.
- **Multi-region object replication is assumed, not configured.**
