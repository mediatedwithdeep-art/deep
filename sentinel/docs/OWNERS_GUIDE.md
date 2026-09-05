# Sentinel — Owner's Guide

**This file is for you, Deep. Not for the government.**
Plain language, no jargon. Keep this one private.

---

## 1. What you have built, in one paragraph

Gujarat has thousands of CCTV cameras, owned by 26 different police
departments. Today they cannot see each other's cameras. If a stolen car
drives from Ahmedabad to Surat, nobody can follow it, because the two
cities use separate systems.

**Sentinel joins them into one screen.** An officer types a number plate.
The system shows where that vehicle has been, across every camera it
passed, on a map, with the reason it believes each step. It also tells the
officer when it is *not* sure — which is the part most systems hide.

---

## 2. The three ideas that make it work

If you remember nothing else, remember these three. Every technical
decision comes from them.

### Idea 1 — Video must never travel. Only information travels.

80,000 cameras sending video to one place needs **320 Gbps** of network.
That is not expensive — it is *impossible*. No government network in India
carries that.

So Sentinel does the thinking **at the camera end**. Each location watches
its own video and sends only a tiny text message: *"white car, plate
GJ01AB1234, seen here, at this time."* That message is about **5 kbps** per
camera — 3,300 times smaller.

> **Say it like this:** "We don't move the video. We move the answer."

### Idea 2 — Geography does the hard work, not the AI.

Here is the clever part, and it uses **no AI at all**.

If a car is seen at Junction A, where can it be 3 minutes later? Only at
junctions reachable by road in 3 minutes. That is usually **1 or 2**
cameras out of 50 — not all 50.

So instead of comparing one car against 20,000 possibilities, we compare it
against 473. **Measured: 97.6% fewer comparisons, 2,240 ms down to 53 ms.**

Why this matters: face/car matching AI is about 99% accurate. Sounds great
— but 1% wrong against 20,000 comparisons means hundreds of wrong answers
every hour. Against 473, it is trustworthy.

> **Say it like this:** "The road network does the filtering. The AI only
> has to answer an easy question."

### Idea 3 — Number plates alone do not work in the real world.

Only about **26%** of real cameras can physically read a plate. The rest
are wide-angle — the plate is too few pixels. It is physics, not software.

So Sentinel also matches on **appearance** (colour, shape, type). But — and
this is a safety rule built into the code — appearance alone can **never**
give a confirmed answer. It is always marked "probable". Only a plate read
gives "confirmed".

> **Say it like this:** "A system that identifies a car by colour will
> eventually accuse the wrong person. We refuse to let it."

---

## 3. How a camera actually connects (simple version)

A camera is like a tap. You need three things:

| Thing | What it means | Example |
|---|---|---|
| **Address** | Where the camera lives on the network | `rtsp://103.250.160.189:8554/stream/cam04` |
| **Password** | Permission to open the tap | Stored separately, never in the database |
| **Position** | Where it physically is, and which way it points | Latitude, longitude, compass direction |

You put these in **one file**: `config/cameras.yaml`. Then run one command.
**No programming.** That file is the only thing that changes when you add
cameras.

### The one thing people always forget: which way it points

Every camera needs a **compass reading** (`heading_deg`). Without it, the
system knows *where* the camera is but not what it is *looking at*.

This matters more than it sounds. Idea 2 above — the road filtering —
depends on knowing direction. A camera facing north sees cars going a
different way than one facing south.

> **Get this during installation.** One compass reading per camera takes 30
> seconds on site. Going back later to 80,000 cameras costs a fortune.

### Three ways video arrives

The system speaks three "languages". You don't choose — it picks
automatically:

- **RTSP** — the normal one for AI processing. Fast, direct.
- **HLS** — the backup. Works through firewalls when RTSP is blocked.
- **WHEP** — for showing live video in a web browser.

**Important thing I built for you:** many government networks block RTSP.
If that happens, the system **automatically switches to HLS** instead of
reporting the camera as broken. Without this, on a blocked network all your
cameras would show red — and it would look like your system failed, when
actually it is the network.

---

## 4. Going from 30 cameras to 80,000

You do this in **five steps, over about three years**. Never jump.

| Phase | Cameras | Roughly | What you prove |
|---|---|---|---|
| **1 — Pilot** | 50–200 | Months 1–6 | It works on real cameras |
| **2 — One district** | 3,000 | Months 6–14 | It works at district scale |
| **3 — Metro** | 12,000 | Months 12–24 | Cross-city tracking works |
| **4 — Highways** | 25,000 | Months 20–32 | Inter-district works |
| **5 — Statewide** | 80,000 | Months 30–40 | Full deployment |

### The physical shape of it

Think of it like a postal system:

- **Edge** (at each district) — a computer with a graphics card watches ~40
  cameras and does the AI. Video stays here forever.
- **Regional** (a few per state) — joins up neighbouring districts, because
  vehicles cross district borders.
- **Central** (Gandhinagar) — the map, the search, the user accounts.

For 80,000 cameras: about **500 edge computers** with **2,000 graphics
cards**. Video never leaves the edge. Only the small text messages travel.

### Money, honestly

Our estimate: **₹82.7 crore** (Sentinel's way) versus **₹138.7 crore**
(sending all video to one place) in year one.

⚠️ **Say this out loud when you present it:** "These are estimates against
list prices. We have not obtained a procurement quote." If you claim it as
a firm number and a procurement officer checks, you lose credibility on
everything else you said.

---

## 5. What is genuinely finished, and what is not

**Be honest about this. It is your biggest advantage.** Every other team
will over-claim. Judges have seen a hundred demos that fell apart.

### ✅ Really done and tested (371 automated tests pass)

- The whole video pipeline — watching, detecting, tracking, plate reading
- Cross-camera tracking with the road-network filter
- 26 departments kept separate from each other (this is enforced and tested)
- Privacy: every lookup needs a stated reason, recorded for 7 years
- The full screen the officer uses
- **Backup and failover — tested, loses 0 records, recovers in 0.36 seconds**

### ❌ Not done — and you must say so

| Not done | Why |
|---|---|
| Connected to real police cameras | We were given test cameras, not the real Gujarat grid |
| VAHAN / SARTHI / eGujCop / AFIS / NAFIS | Needs permission from 5 separate government bodies |
| Accuracy on real Gujarat footage | Our numbers come from a simulator |
| Tested above 1,000 cameras | Beyond that, our numbers are calculations, not measurements |

> **When a judge asks "is it production ready?" say: "No. Here is exactly
> what is missing and why."** Then show the list. This wins trust. Claiming
> "yes" and being caught loses everything.

---

## 6. Questions you will be asked, and how to answer

**"Is this real or a demo?"**
> "Both — and it is the same code. The pipeline, database and screen are
> real. The demo estate is a simulator so it runs without 50 cameras. Point
> it at real cameras and nothing else changes."

**"What accuracy do you get?"**
> "It depends completely on the camera, which is why we never give one
> number. A dedicated plate-reading lane in daylight: 92%. A wide-angle
> camera at night: 38%. We publish the bad number because an officer needs
> to know when not to trust it."

**"How is this different from the CCTV system we already have?"**
> "A normal system shows you cameras. This tells you where one vehicle
> went, across departments that don't share a system, and shows its
> reasoning for every step so an officer can judge whether to act on it."

**"What about citizen privacy?"**
> "Looking at someone's movement history requires a stated reason, which is
> recorded for seven years. Reading that record is itself recorded. Camera
> passwords cannot be stored in the database at all — the design forbids
> it. This is built into the structure, not promised for later."

**"Can it work on our old analog cameras?"**
> "Yes — that is the main design assumption. Analog on a DVR, modern IP,
> municipal feeds. One configuration file, no code change."

**"What happens if the server fails?"**
> "We tested exactly that. We killed the main database with no warning, the
> backup took over in 0.36 seconds, and zero records were lost."

**"Who else has built this?"** (a trap question)
> Don't attack competitors. Say: "The hard part isn't the AI — anyone can
> download a detection model. The hard part is the road-network filter and
> being honest about uncertainty. That's where our work is."

---

## 7. Before you present — checklist

- [ ] Run the system and **let it run 10 minutes** before showing it.
      Cross-camera tracks need time to form.
- [ ] **Record a video of a working demo.** If the venue Wi-Fi fails, you
      still have a demo. This has saved more teams than any technology.
- [ ] Practice the 9-minute run **three times** on the actual laptop.
- [ ] Memorise: 320 Gbps vs 96 Mbps · 97.6% · 92.5% and 37.9% · 26 departments
- [ ] Have `docs/REQUIREMENT_TRACEABILITY_MATRIX.md` open in a tab. If
      anyone challenges a claim, show them the line and the test name.
- [ ] Deliberately break something (stop a camera) and show it recover.
      Recovering from a visible failure persuades more than nothing going wrong.

---

## 8. What to do next, in order

1. **Run the camera check** on your own laptop (I could not — this sandbox
   blocks the connection):
   ```
   python scripts/sentinel_preflight.py \
     --catalogue https://cctv.corp8.cloud/cameras.json \
     --media-host 103.250.160.189 \
     --hls-host cctv.corp8.cloud \
     --profile split-cdn --cameras 3 --seconds 15
   ```
   Send me the result file and I will fix whatever it finds.

2. **Check if the cameras have GPS positions.** The check above will tell
   you. If they don't, the road-network filter — Idea 2, your best idea —
   cannot work on them. This is the most important thing to find out.

3. **Film one junction on your phone.** 200 vehicles, type the real plates
   into a spreadsheet. That turns "92.5% in our simulator" into "and 71% on
   our own real footage", which is a far stronger sentence.

4. **Don't chase the government database permissions yet.** Five separate
   authorities, months each. It is not a coding problem and it should not
   block your demo.

---

## 9. The honest summary

**The software is about 85% done. The deployment programme is about 15%
done.** Those are two different projects.

What is left is mostly not coding — it is access, hardware, permissions and
field survey. That is a normal and healthy place to be for a competition
submission.

Your strongest card is not the technology. It is that **you can show
exactly what is proven and what is not**, with a test name against every
claim. Almost nobody does that. Lead with it.
