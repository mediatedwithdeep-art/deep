# Infrastructure Sizing

**What hardware this needs, at 50 / 1,000 / 3,000 / 10,000 / 50,000 /
80,000 cameras — derived from the measured capacity model, not from a
vendor's sizing sheet.**

Every quantity here comes from [SCALE_BENCHMARK.md](SCALE_BENCHMARK.md) §4,
which carries the per-cell provenance labels. This document turns those
per-resource numbers into node counts and racks. It adds no new
measurements, and where it adds arithmetic it says so.

> **Nothing above 1,000 cameras has been executed.** The 50–1,000 columns
> rest on measured per-camera costs. Everything beyond is those costs
> multiplied out under the ten stated assumptions in SCALE_BENCHMARK §4.
> A sizing document that does not say this is a sales brochure.

---

## 0 · The shape the numbers force

Three facts from the measurements decide the entire topology, and they are
worth stating before any table:

1. **Decode dominates.** 2,853 CPU cores at 80,000 cameras, against 7.4
   cores for the whole event path. Video decoding is ~385× the cost of
   everything Sentinel does with the results.
2. **Decode is perfectly partitionable.** A camera's frames are needed only
   by that camera's pipeline. Nothing about decode requires a shared
   machine, a shared network, or a shared anything.
3. **Metadata is tiny.** 10,000 sightings/s at ~1.2 kB is ≈96 Mbps for the
   entire state.

(1) and (2) together mean the expensive resource is the one that can sit
anywhere. (3) means what has to travel is almost free. That is the whole
argument for putting compute at the edge, and it is an argument from
measurement rather than from architectural preference.

---

## 1 · The three tiers

| Tier | Sits at | Owns | Fails as |
|---|---|---|---|
| **Edge** | District HQ / large junction cluster | Decode, detect, track, ANPR, ReID, local alerting, 72 h ring buffer | 20–50 cameras dark |
| **Regional** | Range / commissionerate | Cross-camera correlation within the region, regional API, WARM storage | One region loses correlation; plate reads continue |
| **Central** | State data centre | State-wide search, long-term metadata, evidence store, federation registry | State view lost; districts keep working |

Video crosses a tier boundary only as an on-demand clip. This is not a
preference; see the 320 Gbps row.

---

## 2 · Node archetypes

Four machine types, sized from the measured per-camera costs.

**The GPU is the binding constraint, not the CPU**, and getting that the
wrong way round is the classic sizing error here. At 6 fps per camera (A3)
a T4-class GPU at 250 fps (A7) carries **41.7 cameras**, derated to **40**.
Decode measured **168 fps per vCPU** at 640×360 (A4), which is **28 cameras
per vCPU** — seven times as many. A node balanced on decode would arrive
with six idle CPUs per GPU and carry a quarter of the cameras its core
count suggests.

| Archetype | Spec | Serves | Basis |
|---|---|---|---|
| **E1 · District appliance** | 16 vCPU, 64 GB, 2× T4-class, 24 TB | **80 cameras** | CALCULATED, GPU-bound |
| **E2 · Edge node** | 32 vCPU, 128 GB, 4× T4-class, 64 TB | **160 cameras** | CALCULATED, GPU-bound |
| **R1 · Regional** | 32 vCPU, 256 GB, 16 TB NVMe | Correlation + WARM for ~5,000 cameras | CALCULATED from A5/A8/A9 |
| **C1 · Central DB** | 32 vCPU, 512 GB, 40 TB NVMe + object store | State metadata, partitioned | CALCULATED from A9/A10 |

E2's 32 vCPU is deliberate slack: decode for 160 cameras needs 5.7 vCPU,
and the rest absorbs reader threads, a reconnect storm across the whole
node, and the ANPR/ReID pre- and post-processing that sits either side of
the GPU.

**The GPU count is the softest number here.** A7 is a published figure, not
one measured on this project — there is no GPU on the benchmark host. Every
GPU row inherits that uncertainty. A CPU-only deployment is possible at
reduced per-camera FPS, and is what the simulation backend models today.

---

## 3 · The build-out

Node counts are `ceil(cameras / 160)`; storage is `4 Mbps × 7 days` per
camera (A1, A10).

| | 50 | 1,000 | 3,000 | 10,000 | 50,000 | 80,000 |
|---|---:|---:|---:|---:|---:|---:|
| **Edge nodes** | 1× E1 | 7 | 19 | 63 | 313 | **500** |
| Edge GPUs | 2 | 28 | 76 | 252 | 1,252 | **2,000** |
| Edge vCPU | 16 | 224 | 608 | 2,016 | 10,016 | 16,000 |
| **Regional nodes** (R1) | 0 | 1 | 1 | 2 | 10 | 16 |
| **Central DB** (C1) | shared | 1 | 1 | 1 + replica | 2 sharded | 3 sharded |
| **Backbone to centre**, metadata only | 0.06 Mbps | 1.2 Mbps | 3.6 Mbps | 12 Mbps | 60 Mbps | **96 Mbps** |
| **Backbone if video centralised** | 200 Mbps | 4 Gbps | 12 Gbps | 40 Gbps | 200 Gbps | **320 Gbps** |
| **Metadata/year** (A9, A10) | 237 GB | 4.7 TB | 14 TB | 47 TB | 237 TB | 379 TB |
| **Video 7 d, held at the edge** (A1) | 15 TB | 302 TB | 907 TB | 3.0 PB | 15 PB | **24 PB** |

Labels: 50 and 1,000 are **CALCULATED** from measured per-camera costs.
3,000 and above are **PROJECTED** — the same arithmetic, no execution.

The 2,000 GPUs here against SCALE_BENCHMARK's 1,920 is per-node rounding:
that table divides total frame rate by GPU throughput, this one buys whole
nodes. The 4% gap is the cost of not being able to purchase 0.8 of a node.

**Read the last two rows together.** The 24 PB of video never moves. It
sits on 500 edge nodes at 48 TB each and is pulled only when an
investigator asks for a specific clip. Centralising it would require both
the 320 Gbps backbone and a 24 PB central store; doing neither is what the
architecture buys.

---

## 4 · Storage and retention

Retention is a row in `partitioned_table_config`, not a constant in a
migration, and the tiers are enforced by a CHECK constraint
(`0010_storage_tiers.sql`). An operator changes a window with an UPDATE.

| Table | HOT | WARM | COLD → detach | Drop | Why |
|---|---:|---:|---:|---:|---|
| `detection` | 1 d | 3 d | 3 d | 3 d | Per-frame boxes; nothing in the UI reads them |
| `vehicle_sighting` | **7 d** | **15 d** | 30 d | 90 d | The operational table; 7/15 is the configurable window |
| `plate_read` | 36 d | 182 d | 365 d | 365 d | Investigations reach back further than operations |
| `alert` | 73 d | 365 d | 730 d | 730 d | Two years of alert history |
| `event`, `camera_health` | 3 d | 15 d | 30 d | 30 d | Operational telemetry |
| `audit_log` | 255 d | 1,277 d | 2,555 d | 2,555 d | **7 years — DPDP; outlives everything** |

**HOT** is indexed and served directly. **WARM** stays queryable but is no
longer expected to be fast. **COLD** is detached from the parent table, so
the planner stops considering it while the rows remain on disk for export.
Only `drop_old_partitions()` deletes, and the CHECK holds the drop at or
beyond the cold boundary — so archival can never be overtaken by deletion.

Video retention is an edge-local policy (72 h ring buffer by design; **not
measured — no edge appliance has been built**).

*Tested:* `tests/test_database.py` — tier ordering, the CHECK that enforces
it, tier transitions at 1/10/20/99 days, retention reconfigured without a
migration, and that a cold detach leaves the rows readable.

---

## 5 · What this sizing does not cover

- **No edge appliance exists.** E1/E2 are specifications derived from
  measured per-camera costs on a 4 vCPU x86 host, not a built and measured
  box. Thermal, power and physical-security constraints at a district
  facility are unaddressed.
- **The GPU rows inherit A7**, which is a published figure. No GPU was
  available to this project.
- **Nothing above 1,000 cameras was executed.**
- **No network was measured.** Bandwidth rows are arithmetic over assumed
  bitrates; see [NETWORK_BANDWIDTH_PLAN.md](NETWORK_BANDWIDTH_PLAN.md).
- **Database sharding above 10,000 cameras is a plan**, not a tested
  configuration. Partitioning is implemented; cross-shard query routing is
  not.
- **High availability is designed, not built** — see
  [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) §2.6. The single largest
  unmitigated risk remains the unreplicated central database.
