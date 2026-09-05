# Statewide Rollout

**How this reaches 80,000 cameras across Gujarat, in an order where each
phase is useful on its own and each one can be stopped.**

---

## 0 · The principle

A rollout plan whose value arrives only at the end is a plan that gets
cancelled in year two. Every phase below delivers a working capability to a
real user, and no phase depends on the one after it.

The second principle is less comfortable: **each phase must be allowed to
fail without taking the previous one down.** That is why the edge tier
comes before the state tier, and why cross-district correlation is late
rather than early — it is the capability that requires everything else to
already work.

---

## 1 · Phases

| Phase | Scope | Cameras | Delivers | Gate to the next phase |
|---|---|---:|---|---|
| **0 · POC** | One commissionerate, existing cameras | 50–200 | Live ingest, ANPR, cross-camera on one district | Measured ANPR accuracy per camera class on **real** imagery |
| **1 · District** | Ahmedabad City + Traffic | ~3,000 | Full command centre, alerting, evidence export | 30 days at &lt;1% missed-sighting rate; operator sign-off |
| **2 · Metro** | Surat, Vadodara, Rajkot | ~12,000 | Four independent districts, regional tier proven | Regional failover executed, not designed |
| **3 · Highway** | State highway corridors | ~25,000 | Inter-district vehicle movement | Cross-district correlation measured across a real border |
| **4 · State** | Remaining districts | ~80,000 | Statewide search and federation | — |

Phase 0 exists to answer one question the current work cannot: **what is
the real ANPR accuracy on this estate?** Every published figure in this
repository is the simulation backend's. Committing to Phase 1 procurement
before that number is measured on real imagery is the single largest
programme risk.

---

## 2 · What has to be true before Phase 1

These are entry criteria, not aspirations. Each is currently unmet.

| Requirement | Today | Needed |
|---|---|---|
| Real Sentinel gateway integration | **PENDING EXTERNAL ACCESS** | Host, credentials, API documentation |
| ANPR accuracy on real imagery | SIMULATED (92.5% / 37.9%) | Measured per camera class, published |
| Government record adapters | Five mock backends, every record stamped `MOCK` | Authorised access to VAHAN, SARTHI, eGujCop, AFIS, NAFIS — five separate institutional processes |
| Database replication | **None** — single instance, no PITR | Streaming replica + WAL archiving. This is the largest unmitigated risk in the system |
| Edge appliance | Does not exist | Built and thermally qualified for a district facility |
| GPU throughput | Assumption A7, published figures | Measured on the procured GPU |
| Failover | Designed | Executed, with the RPO/RTO figures observed rather than targeted |

**The database replication row is the one that should block procurement.**
Today a primary loss costs every sighting since the last backup, and there
is no backup schedule in the MVP. Everything else on this list degrades a
capability; that one loses evidence.

---

## 3 · Per-district sequence

Each district repeats the same six steps. The order is deliberate — survey
before procurement, because a camera without a compass bearing is very
expensive to retrofit.

1. **Survey.** Position, and **`heading_deg` per camera**. Without a
   bearing a camera is a dot with no field of view, and because the
   adjacency graph is directional the cross-camera gate is materially
   weaker. One compass reading per site during survey; near-impossible to
   retrofit across thousands of sites later.
2. **Catalogue.** Register with the gateway. Nothing is hard-coded; the
   catalogue is the source of truth and the reconciler picks up additions,
   removals and changes without a code change.
3. **Adjacency.** Build the road-travel-time graph for the district. This
   is what makes appearance matching usable — 1.2 candidate cameras of 49
   within 3 minutes on the demo estate.
4. **Edge deployment.** One E2 per ~160 cameras
   ([INFRASTRUCTURE_SIZING.md](INFRASTRUCTURE_SIZING.md) §2).
5. **Calibration.** Set `anpr_capable` per camera from measured plate
   resolution, not from the procurement spreadsheet. On the demo estate
   only 26% of cameras can physically resolve a plate; an estate that
   claims otherwise produces a dotted line and blames the model.
6. **Operator training and sign-off**, including what *probable* means and
   why the system shows a confidence rather than an answer.

---

## 4 · Rollout risks

| Risk | Consequence | Mitigation | Status |
|---|---|---|---|
| Real ANPR accuracy far below simulation | The programme's central claim weakens | Phase 0 measures it before Phase 1 commits | **Mitigated by sequencing** |
| Missing `heading_deg` across a district | Gate materially weaker; more false positives | Survey step 1; refuse cameras without a bearing | **Designed** |
| A fifth of the estate broken at any time | Coverage gaps that look like system faults | Health view reports it rather than hiding it; correlated failures shown as one incident | **PARTLY BUILT** — correlation view is designed |
| Government adapter access never granted | Watchlist screening stays mock | Adapters are interface-complete; a real backend raises and names what is missing | **Contained** |
| District WAN unreliable | Central view stale | Local alerting continues; store-and-forward | **DESIGNED**, not built |
| Central DB loss | Every sighting since last backup | Replication + PITR | **NOT MITIGATED** |
| Vendor lock-in on cameras | Estate cannot be extended | ONVIF/RTSP adapters; no vendor SDK in the ingest path | **BUILT** |

---

## 5 · What this plan does not do

- **It does not commit to dates.** Phase durations depend on procurement
  and on institutional processes outside this project's control —
  particularly the five separate authorisations behind the government
  adapters.
- **It does not assume the current accuracy figures hold.** They are the
  simulation backend's, and Phase 0 exists to replace them.
- **It does not cost the camera estate itself**, which in a real programme
  dominates every figure in [COST_BENEFIT.md](COST_BENEFIT.md).
- **It assumes the edge appliance can be built to the E2 specification.**
  No such appliance exists today.
