# Cost–Benefit

**What the two architectures cost, and why the comparison is robust even
though the unit prices are not.**

---

## 0 · Read this before any number

**No procurement quote was obtained for this project.** Nothing here is a
tender price, a vendor discount, or a GeM rate. Every unit cost below is an
**ESTIMATE** against public list prices, and any real procurement will
differ — plausibly by a factor of two in either direction.

That sounds like it should invalidate the exercise. It does not, and the
reason is the point of this document:

> **The comparison is a ratio, and the ratio survives the uncertainty.**
> Centralised and federated buy the *same* GPUs, the *same* decode cores
> and the *same* cameras. They differ in backbone and central storage —
> 320 Gbps against 96 Mbps, and 24 PB centrally against nothing. Move every
> unit price by ±50% and the conclusion does not move at all.

So: treat the absolute totals as illustrative and the **difference** as the
finding. Where a number is load-bearing it is labelled.

---

## 1 · Unit costs assumed

| Item | Assumed | Label |
|---|---:|---|
| U1 · E2 edge node (32 vCPU, 128 GB, 4× T4-class, 64 TB) | ₹14,00,000 | ESTIMATED, list |
| U2 · R1 regional node | ₹9,00,000 | ESTIMATED, list |
| U3 · C1 central DB node | ₹18,00,000 | ESTIMATED, list |
| U4 · Central storage, per PB usable, redundant | ₹1,60,00,000 | ESTIMATED |
| U5 · State backbone, per Gbps per year | ₹6,00,000 | ESTIMATED |
| U6 · Edge WAN drop, per site per year | ₹1,20,000 | ESTIMATED |
| U7 · Power + cooling, per node per year | ₹90,000 | ESTIMATED |
| U8 · Operations staff, per FTE per year | ₹12,00,000 | ESTIMATED |

Cameras, poles, civil works and their maintenance are **excluded from both
sides**, because they are identical in both and including them only dilutes
the difference. They are the largest line item in a real programme.

---

## 2 · The comparison at 80,000 cameras

Capital, plus the first year of operating cost. Quantities come from
[INFRASTRUCTURE_SIZING.md](INFRASTRUCTURE_SIZING.md) §3.

| | Federated (this design) | Centralised | Note |
|---|---:|---:|---|
| Edge nodes, 500 × U1 | ₹70.00 cr | — | |
| Central compute, 500-node equivalent | — | ₹70.00 cr | the same GPUs, in one building |
| Regional nodes, 16 × U2 | ₹1.44 cr | — | no regional tier when everything is central |
| Central DB, 3 × U3 | ₹0.54 cr | ₹0.54 cr | identical |
| **Central video storage, 24 PB × U4** | **₹0.00 cr** | **₹38.40 cr** | the largest single difference |
| **Backbone, year 1 × U5** | **₹0.01 cr** <br>0.096 Gbps | **₹19.20 cr** <br>320 Gbps | recurs every year |
| Edge WAN drops, 500 × U6 | ₹6.00 cr | ₹6.00 cr | identical |
| Power + cooling × U7 | ₹4.67 cr <br>519 nodes | ₹4.53 cr <br>503 nodes | ~identical |
| **Total, year 1** | **₹82.66 cr** | **₹138.67 cr** | |

**Difference: ₹56.0 cr in year one.**

Storage and backbone alone account for **₹57.6 cr** of it — slightly more
than the total gap, because federated spends ₹1.4 cr on a regional tier
that a centralised design does not need and a little more on power for the
extra nodes. Everything else is within noise of identical, because it is
the same hardware in a different building.

Recurring years diverge further: backbone is an annual cost, so centralised
carries **₹19.2 cr/year** that federated does not, before any growth in
retention.

---

## 3 · Why the storage line is not negotiable

24 PB is not a procurement problem that a better discount solves. At 80,000
cameras and 4 Mbps the estate produces **3.46 PB per day**. Seven days is
the *minimum* useful window, and every additional day of central retention
is another 3.46 PB — **₹5.5 cr at U4**, each day, forever.

The federated design does not make that data cheaper. It makes it
**local**: the same 24 PB, spread across 500 edge nodes at 48 TB each, on
commodity storage inside a machine that had to exist anyway. It is bought
as part of U1, which is why it does not appear as a separate line.

**The sensitivity that matters.** U4 would have to fall to roughly a
seventh of the assumed rate before storage stopped dominating the
difference — and at that point retrieval latency for evidence has usually
been traded away, which is the one place latency is least acceptable.

---

## 4 · Benefits that are not cost savings

Stated separately because they are not quantified here and should not be
smuggled into a financial total.

| Benefit | Evidence | Status |
|---|---|---|
| Districts keep working when the WAN fails | DR §2.3; local alerting continues | **DESIGNED** |
| Investigation time cut by the gate | 20,000 comparisons → 473; 2,240 ms → 53 ms | **MEASURED**, simulation backend |
| Plate-independent tracking on a mostly wide-angle estate | 26% of the demo estate can resolve a plate | **MEASURED**, demo estate |
| Cross-department reuse of one camera estate | 26 departments, isolation enforced at the query layer | **TESTED** |
| Audit trail sufficient for DPDP purpose limitation | 7-year partitioned audit log; reads are themselves audited | **TESTED** |

**Not claimed:** crime-reduction figures, clearance-rate improvements, or
any outcome measured in convictions. This project has no data that would
support such a claim, and a cost-benefit case that asserts one without
evidence is the part a reviewer should distrust most.

---

## 5 · What would change these numbers

- **A GPU price move** shifts both columns equally. It does not affect the
  finding.
- **A cheaper central storage tier** (tape, or object storage at archival
  rates) is the only lever that materially narrows the gap — and it trades
  against retrieval latency for evidence, which is the one place latency is
  least acceptable.
- **Lower per-camera bitrate** (H.265 at 2 Mbps rather than H.264 at 4)
  halves both the 320 Gbps and the 24 PB. It halves the gap; it does not
  close it. The estate is already mixed-codec and this is real.
- **A shorter retention window** scales the storage line linearly and is a
  policy decision, not an engineering one. It is now a row in
  `partitioned_table_config` rather than a migration.

---

## 6 · Honest limits

- **No quotes, no tender rates, no vendor engagement.** Every unit cost is
  an estimate against list prices.
- **Absolute totals are illustrative.** Use the difference, not the sum.
- **Cameras and civil works are excluded**, and in a real programme they
  dominate both columns.
- **Operations staffing is not modelled** beyond a unit rate; the real
  figure depends on shift patterns and on how much of the estate is broken
  at any moment — which, for a real government estate, is roughly a fifth.
- **No GPU was benchmarked.** The compute line on both sides inherits A7.
