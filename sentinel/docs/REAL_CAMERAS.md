# Connecting Real Cameras

**You edit one file: `config/cameras.yaml`. Nothing else.**

No source changes, no rebuild, no redeploy. The demo estate and a live
estate differ only by that file and one environment variable.

---

## The three steps

### 1 · Put the credentials in the environment, not the config

```bash
# .env
SENTINEL_CAM_AHM101=viewer:the-real-password
SENTINEL_CAM_AMCDVR3=admin:another-password
```

A password written into `cameras.yaml` is tolerated for a laptop demo and
**refused outright** when `ENVIRONMENT=production` — the loader raises on
startup rather than running with credentials at rest in a config file that
ends up in a backup, a ticket, or a screen share.

### 2 · Describe the cameras

```yaml
cameras:
  - camera_id: AHM-SAT-101              # your own stable identifier
    name: Jodhpur Cross Roads - NE approach
    latitude: 23.02705
    longitude: 72.51192
    heading_deg: 47                     # WHICH WAY IT LOOKS — see below
    fov_deg: 82
    range_m: 65
    protocol: RTSP
    stream_url:    rtsp://10.42.7.14:554/Streaming/Channels/101
    substream_url: rtsp://10.42.7.14:554/Streaming/Channels/102
    credential_ref: env:SENTINEL_CAM_AHM101
    anpr_capable: false
    zone: Satellite
    department: GP_AHM
```

### 3 · Restart ingestion

```bash
make hybrid     # real cameras PLUS the simulated estate  ← use this at a venue
make live       # real cameras only
```

`hybrid` is the right choice for a demonstration: if the venue network drops
your feeds, the simulated estate keeps running and the demo degrades instead
of ending.

---

## The two fields people get wrong

### `heading_deg` — not optional in practice

Without it, a camera is a dot on a map with no field of view, and because
the adjacency graph is **directional**, the cross-camera gate is materially
weaker. The API returns a warning when you omit it.

It costs one compass reading per camera during survey and is very expensive
to retrofit across thousands of sites. Capture it.

### `substream_url` — the single biggest scale lever

Almost every IP camera and DVR channel exposes two encodings of the same
scene: a main stream (1080p/4K, 4–8 Mbps) and a sub-stream (D1–720p,
0.3–1 Mbps).

**The AI consumes the sub-stream.** 704×576 is enough for detection, colour,
type and re-identification. This cuts decode and network cost **6–8×** — the
difference between 3 GPUs and 25 for the same camera count.

The main stream is recorded locally and pulled only for evidence, and used
live only on dedicated ANPR lanes where plate pixels matter.

---

## Vendor URL patterns

Note the sub-stream selector in each — `102`, `subtype=1`, `stream=1`.

| Vendor | Main | Sub |
|---|---|---|
| Hikvision (and rebrands) | `/Streaming/Channels/101` | `/Streaming/Channels/102` |
| Dahua / CP Plus | `/cam/realmonitor?channel=1&subtype=0` | `...&subtype=1` |
| Axis | `/axis-media/media.amp` | `/axis-media/media.amp?resolution=640x480` |
| Uniview | `/media/video1` | `/media/video2` |
| Hi3520 / XMEye OEM | `/user={u}_password={p}_channel=1_stream=0.sdp` | `..._stream=1.sdp` |
| Generic ONVIF | `/onvif1` | `/onvif2` |

Unsure? The system will find them:

```bash
curl -X POST localhost:8000/api/v1/cameras/discover \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"gateway_code":"EDGE-AHM-03","cidr":"10.42.7.0/24"}'
```

ONVIF WS-Discovery auto-onboards roughly half of a typical estate with no
manual data entry.

---

## Analog cameras on legacy DVRs

Around a fifth of a government estate. Almost every DVR shipped since ~2012
exposes RTSP per channel, so usually **no hardware is needed**:

```yaml
  - camera_id: AMC-DVR3-CH07
    name: Zone-3 Yard Gate
    latitude: 23.0410
    longitude: 72.5610
    heading_deg: 190
    protocol: DVR
    substream_url: rtsp://10.42.9.3:554/cam/realmonitor?channel=7&subtype=1
    credential_ref: env:SENTINEL_CAM_AMCDVR3
    width: 704
    height: 576
    anpr_capable: false        # CVBS cannot resolve a plate. Be honest here.
    department: AMC
```

Practical obstacles you will hit:

- **Concurrent session limits.** Many DVRs cap RTSP at 4–8 sessions. Sentinel
  pulls each camera once and fans out internally; never point three
  consumers at one DVR channel.
- **Auth quirks.** Some require Basic rather than Digest.
- **TCP only.** UDP loss over a shared WAN looks like corruption, not loss,
  and produces green smears the detector happily finds vehicles in.
- **Clock drift.** Legacy DVR RTCs drift minutes per week. Sentinel stamps
  frames with the gateway's NTP-disciplined clock, never the camera's — a
  reversed timestamp would corrupt an entire trajectory.

Setting `anpr_capable: true` on a 704×576 camera does not make it read
plates. The API warns; the physics does not care. See
[LEGACY_INTEGRATION.md](LEGACY_INTEGRATION.md) for encoders and the full
security model.

---

## Dedicated ANPR lanes

An ANPR camera is a different installation, not a setting:

```yaml
  - camera_id: GSRTC-ANPR-02
    name: Geeta Mandir bus stand - exit ramp
    latitude: 23.0132
    longitude: 72.5905
    heading_deg: 275
    fov_deg: 28                 # NARROW: this is what makes a plate big enough
    range_m: 40
    protocol: RTSP
    stream_url: rtsp://10.42.11.8:554/Streaming/Channels/101   # MAIN stream
    credential_ref: env:SENTINEL_CAM_GSRTC02
    width: 1920
    height: 1080
    anpr_capable: true
    role: ANPR
```

The plate must exceed ~90 px (≈110 px at night) to be readable. That needs a
narrow field of view, a close range, and IR at night. Expect **10–15%** of a
general estate to qualify — the other 85% still contribute through
appearance, colour and type.

---

## Security: cameras must not be reachable from anywhere else

Legacy DVRs are the worst-maintained devices on a government network:
default credentials, end-of-life firmware with published RCEs, and a
depressing number already port-forwarded to the public internet.

The control is **outbound-only edge gateways**:

```
┌── SITE ─────────────────────────────────────────────┐
│  VLAN 10 — CAMERAS (no default gateway, no internet)│
│    DVR   IP cam   IP cam                            │
│      └──────┬──────┘                                │
│      ┌──────┴──────────────┐                        │
│      │ EDGE GATEWAY        │  ip_forward = 0        │
│      │ credentials in RAM  │  outbound mTLS only    │
│      │ metadata only exits │                        │
│      └──────┬──────────────┘                        │
└─────────────┼───────────────────────────────────────┘
              ▼  no inbound rules at the site, ever
         STATE CORE
```

No port forwarding, no public DVR exposure, no inbound firewall rules to get
wrong at 2,000 sites. A compromised core cannot pivot into a camera VLAN
because the gateway does not forward.

The shipped Kubernetes NetworkPolicy enforces the Kubernetes half of this:
only ingestion pods may reach camera subnets, and only on 554/80/443.

---

## Verifying an onboarding

```bash
# Does the URL work, and what is actually on the other end?
curl -X POST localhost:8000/api/v1/cameras/AHM-SAT-101/probe \
     -H "Authorization: Bearer $TOKEN"
```

```json
{
  "reachable": true, "status": "ONLINE",
  "codec": "h264", "width": 704, "height": 576, "fps": 12.0, "rtt_ms": 48.2,
  "anpr_capable": false,
  "warnings": ["resolution too low for ANPR; camera contributes via ReID and attributes only"]
}
```

Then rebuild the adjacency graph — **do not skip this**:

```bash
python database/build_adjacency.py --max-dist 5000
```

```
50 cameras
214 adjacency edges (avg 4.3 downstream candidates per camera)
Gate selectivity: 4.3 candidates per sighting instead of 49 — a 91.2% reduction
```

That last line is the number that determines your cross-camera precision.
Read it. Without the adjacency graph the gate is empty and appearance
matching runs against every camera in the estate.

---

## Bulk onboarding

For hundreds of cameras, `POST /api/v1/cameras/bulk` accepts up to 5,000
rows and returns per-row results. Partial success is normal and expected: a
malformed row is reported and skipped rather than aborting the other 1,999.

---

## Checklist

- [ ] Credentials in `.env` as `credential_ref`, never in `cameras.yaml`
- [ ] `heading_deg` captured for every camera
- [ ] `substream_url` set wherever the camera has one
- [ ] `anpr_capable` set honestly — resolution and field of view, not hope
- [ ] Camera VLANs isolated behind an edge gateway, outbound-only
- [ ] Every camera probed and `ONLINE`
- [ ] `build_adjacency.py` run, and its selectivity line read
- [ ] NTP configured on every gateway

---

## Connecting the Sentinel Camera Grid

The grid publishes each camera over three protocols, from **two different
hosts** — a CDN cannot proxy RTP, so HLS is served from the CDN hostname
behind an access password while RTSP and WHEP come direct from the media
host. Nothing about those addresses is compiled in; they are configuration:

```bash
export SENTINEL_CATALOGUE_URL=https://<cdn-host>/cameras.json
export SENTINEL_GATEWAY_PROFILE=split-cdn
export SENTINEL_MEDIA_HOST=<media-host>        # RTSP 8554, WHEP 8889
export SENTINEL_HLS_HOST=<cdn-host>            # HLS over TLS
export SENTINEL_CATALOGUE_BASIC=user:password  # if the catalogue is protected
```

Verify before you rely on it:

```bash
python scripts/sentinel_preflight.py \
    --catalogue "$SENTINEL_CATALOGUE_URL" \
    --media-host "$SENTINEL_MEDIA_HOST" \
    --hls-host "$SENTINEL_HLS_HOST" \
    --profile split-cdn --cameras 3 --seconds 15
```

It drives the production catalogue client and the production reader, so a
pass is evidence about the system rather than about the script. It reports
which catalogue field spellings the gateway actually used, whether the RTSP
port is reachable, and — per camera — frames decoded, PTS monotonicity,
frame rate measured from PTS rather than from the reported rate, and the
spread of inter-frame gaps. It writes a JSON report you can attach to a bug
report or paste back.

**If port 8554 is closed on your network**, that is expected on many
government and corporate WANs and is not a camera fault. Each camera
carries its HLS URL as a fallback transport and the reader rotates to it
after repeated failures, cyclically rather than permanently — a blocked
port and a temporarily sick CDN are indistinguishable from the client, so
committing forever to whichever was up at startup would be wrong.

**Cameras without coordinates cannot join the adjacency graph.** If the
catalogue carries no `latitude`/`longitude`, preflight says so per camera.
Those cameras still ingest, but the spatio-temporal gate cannot score them,
which is the component the whole cross-camera argument rests on. One
compass reading and one GPS fix per camera during survey is the fix.
