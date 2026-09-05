# 04 — Bridging Legacy Analog DVRs Securely

Roughly a fifth of an 80,000-camera government estate is analog cameras on DVRs, much of it 8–15 years old. This is simultaneously the easiest integration problem and the single largest security liability in the system.

---

## 1. Getting the pixels — four options, in order of preference

### Option A — the DVR's own RTSP server (zero hardware, covers ~70% of DVRs)

Almost every DVR shipped since ~2012 exposes RTSP per channel. Try, in order:

```
Hikvision / rebrands   rtsp://{u}:{p}@{ip}:554/Streaming/Channels/{ch}02
Dahua / CP Plus        rtsp://{u}:{p}@{ip}:554/cam/realmonitor?channel={ch}&subtype=1
Hi3520 generic OEM     rtsp://{u}:{p}@{ip}:554/user={u}_password={p}_channel={ch}_stream=1.sdp
XMEye / Sofia OEM      rtsp://{u}:{p}@{ip}:554/user={u}&password={p}&channel={ch}&stream=1.sdp
ONVIF fallback         rtsp://{u}:{p}@{ip}:554/onvif{ch}
```

Note `subtype=1` / `stream=1` / `Channels/x02` — that is the **sub-stream**, which is what the AI pipeline wants (doc 01 §1.3). Most integration guides tell you to pull the main stream; do not.

Practical obstacles you will hit: many DVRs cap concurrent RTSP sessions at 4–8 (so **pull once at the edge and fan out locally** — never let 3 consumers each open a session), some require Basic rather than Digest auth, and a great many will only speak RTSP-over-TCP reliably.

### Option B — external video encoder (for DVRs with no usable RTSP)

BNC in, RTSP out. Axis M7104 / M7116, Hikvision DS-6704HUHI, Dahua DH-NVS. ~₹8,000–18,000 per 4 channels. Bypasses the DVR entirely for the live path — the DVR keeps recording locally and untouched, which matters because you must not disturb an existing evidentiary recording chain.

### Option C — capture card on an edge box

Blackmagic DeckLink / cheap TW686x-based cards + GStreamer `v4l2src`. Use when the DVR is genuinely dead-ended and an encoder budget is unavailable. Highest per-channel cost in labour.

### Option D — vendor SDK bridge (for proprietary VMS: Milestone, Genetec, CP Plus)

Wrap the SDK in a small sidecar that republishes to RTSP locally, then treat it as Option A. Isolate this per vendor — SDKs are the least reliable code in the system and must never be able to take down the ingest tier.

**Signal quality note:** analog CVBS is 720×576 at best, often much worse after 100 m of degraded coax. HD-over-coax (AHD/TVI/CVI) DVRs deliver 720p/1080p and are worth identifying separately in the registry — they are ANPR-capable, plain CVBS is not. Tag `signal_class` on every legacy camera so the CV tier sets expectations correctly rather than wasting GPU on unreadable plates.

---

## 2. Security — the part that matters more

### 2.1 The threat, stated plainly

Legacy DVRs are the worst-maintained network devices in Indian government infrastructure. Typical findings on any real estate survey:

- Default or trivial credentials (`admin/admin`, `admin/12345`) on a large fraction of units
- Telnet open, no TLS anywhere, HTTP-only web UIs
- Firmware years past end-of-support, with published unauthenticated RCE (the Hikvision and Dahua backdoor families, the long tail of Sofia/XMEye OEM firmware)
- Frequently **port-forwarded to the public internet** by whoever installed them, so they are already indexed on Shodan and Censys
- Chinese-OEM firmware with undocumented outbound "cloud" connections (XMEye, Hik-Connect) in a police network

Connecting these directly to a state police network would create a lateral-movement path from the public internet into GSWAN. **This is the risk the architecture must neutralise, and it is worth saying so explicitly in your submission — it demonstrates threat modelling rather than feature listing.**

### 2.2 The control: outbound-only edge gateway, DVR on an isolated segment

**The DVR must never be routable from anywhere except its own edge gateway, and the site must never accept an inbound connection.**

```
┌─── SITE (police station / municipal office) ────────────────────┐
│                                                                  │
│   VLAN 10 — CAMERA (isolated, no default gateway, no internet)  │
│   ┌────────┐  ┌────────┐  ┌────────┐                            │
│   │  DVR   │  │ IP cam │  │ IP cam │   ← cannot initiate egress │
│   └───┬────┘  └───┬────┘  └───┬────┘     cannot be reached from │
│       └───────────┴───────────┘          outside this VLAN      │
│                   │ (only the gateway may cross)                │
│            ┌──────┴───────────────────┐                         │
│            │  EDGE GATEWAY            │                         │
│            │  • dual-homed, forwarding disabled                 │
│            │  • nftables default-deny both directions           │
│            │  • credentials in local vault, never on the wire   │
│            │  • decode + AI, metadata only leaves               │
│            │  • WireGuard, OUTBOUND ONLY                        │
│            └──────┬───────────────────┘                         │
│   VLAN 20 — TRANSIT                  │                          │
└──────────────────────────────────────┼──────────────────────────┘
                                       │  outbound mTLS over WireGuard
                                       ▼                     GSWAN / MPLS
                              ┌────────────────┐
                              │  STATE CORE    │  ← no inbound rules at site
                              └────────────────┘
```

Why outbound-only is the load-bearing control: it means **no port forwarding, no public DVR exposure, no inbound firewall rules to get wrong at 2,000 sites, and no attack surface reachable from the internet.** A compromised core cannot pivot into a site's camera VLAN either, because the gateway does not forward.

### 2.3 Layered controls

| Layer | Control |
|---|---|
| **Segmentation** | Camera VLAN with no default route. Private IPv4. Gateway is dual-homed with `net.ipv4.ip_forward=0` — it is a proxy, not a router. |
| **Transport** | WireGuard (ChaCha20-Poly1305) site→core underlay, initiated outbound. mTLS on top for application identity — defence in depth, and the mTLS cert is what the API authorises against. |
| **Machine identity** | X.509 per gateway, issued by an internal CA. **SPIFFE/SPIRE** for automated short-lived cert rotation at scale — 2,000 gateways with manual certs is an operational impossibility. |
| **Credential handling** | DVR credentials live in the core vault (HashiCorp Vault / OpenBao), leased to the gateway with a short TTL, held in memory only. Never in a config file, never in the database in plaintext, never in the camera registry API response. |
| **Egress control** | Gateway default-deny egress; allow only the core endpoint. This is what stops XMEye/Hik-Connect phoning home from your police network. |
| **Firmware posture** | Where firmware cannot be updated (usually), compensate: change all default credentials, disable Telnet/UPnP/P2P-cloud, and accept the device as untrusted-but-contained. Record `firmware_risk` in the registry so the risk is visible rather than forgotten. |
| **Monitoring** | Any inbound connection attempt to a camera VLAN, any egress from a gateway to a non-core destination, and any new MAC on the camera VLAN are all high-severity alerts. On a segment this static, anomaly detection is genuinely easy and genuinely effective. |
| **Access control** | Keycloak, realm per department, RBAC down to camera groups. All access decisions logged immutably. |

### 2.4 Compliance and evidentiary integrity (India-specific)

This is where a submission wins or loses against evaluators who deploy systems rather than judge demos.

- **DPDP Act 2023** — video of identifiable persons is personal data. Establish purpose limitation, define retention per camera class, log every access with a stated reason, and support erasure workflows for non-evidentiary footage. Build the audit log on day 1; it cannot be retrofitted.
- **BSA 2023 §63** (Bharatiya Sakshya Adhiniyam, which replaced Indian Evidence Act §65B) — electronic evidence requires an accompanying certificate identifying the device, its operation, and the integrity of the record. **Auto-generate a §63 certificate with every exported clip**, carrying: source camera ID and location, capture window, device identity, hash of the exported file, and the operator identity that ordered the export. Almost nobody in a hackathon does this and it is the difference between "interesting demo" and "a thing a prosecutor can use."
- **Chain of custody** — hash-chain every evidence export: `H(n) = SHA256(H(n-1) ‖ clip_hash ‖ metadata ‖ timestamp)`. Append-only table, periodically anchored (an internal timestamping authority is sufficient; a public blockchain is not required and invites data-localisation objections).
- **Data localisation** — all storage and processing on Indian soil: NIC MeghRaj / Gujarat State Data Centre. No foreign SaaS in the video path. This is also the argument for MapLibre + Bhuvan over Google Maps or Mapbox (doc 02).
- **CCTNS / ICJS interoperability** — an ANPR hit is only useful if it resolves to a vehicle and an owner. Integrate **VAHAN** for RC lookup and expose a CCTNS-compatible alert payload. Model this as an adapter behind an interface; the production credentials will not exist during a hackathon, so ship a mock implementation with the real schema.

### 2.5 Migration path — do not rip and replace

80,000 cameras cannot be modernised in one programme. Stage it:

1. **Integrate** — every camera reachable through an edge gateway, no field hardware changed. Immediate state-wide visibility.
2. **Contain** — segment and isolate every legacy DVR, rotate all credentials, kill P2P cloud egress. Security improves without capex.
3. **Upgrade selectively** — replace only cameras whose *analytics value* justifies it: junctions, borders, toll plazas, entry/exit corridors. Use the trust score and the sighting-contribution statistics from §01 §3 to rank spend by measured usefulness rather than by age.
4. **Standardise** — mandate ONVIF Profile-S/T, 1 s GOP, dual-stream and NTP in all future tenders. This is a one-page procurement annexure that permanently removes the integration problem for every future camera, and it costs nothing.

Point 4 is worth calling out explicitly in the submission: the most valuable long-term deliverable of this project is not the software, it is the **procurement standard** that stops the estate getting more heterogeneous every year.
