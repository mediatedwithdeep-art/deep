# Architecture Diagrams

Mermaid source, rendered by GitHub and most Markdown viewers.

---

## 1 · System overview

The single most important thing in this diagram is the dashed line: **video
does not cross it**. Only metadata flows edge → core, and only on demand
does a clip flow the other way.

```mermaid
flowchart TB
    subgraph CAM["Heterogeneous estate"]
        C1["IP cameras<br/>RTSP · ONVIF"]
        C2["Analog cameras<br/>on legacy DVRs"]
        C3["Municipal feeds<br/>HLS"]
        C4["Proprietary VMS<br/>vendor SDK"]
    end

    subgraph EDGE["Edge node — one per site"]
        DEC["Decode once<br/>ffmpeg / NVDEC"]
        DET["Detect · YOLOX"]
        TRK["Track · ByteTrack"]
        GATE{"Quality gate<br/>~80% refused"}
        ANPR["ANPR<br/>plate + OCR + lexicon"]
        REID["ReID · 512-d<br/>+ colour + type"]
        BUF[("Local ring buffer<br/>7–30 days video")]
    end

    subgraph CORE["State core"]
        BUS{{"Redis Streams<br/>→ Kafka at scale"}}
        MATCH["Cross-camera matcher<br/>gate → fuse → assign"]
        RULES["Alert engine<br/>configurable rules"]
        DB[("PostgreSQL<br/>PostGIS · pgvector<br/>partitioned")]
        API["API · FastAPI<br/>RBAC · audit"]
        UI["Command centre<br/>React · MapLibre"]
    end

    C1 & C2 & C3 & C4 --> DEC
    DEC --> DET --> TRK --> GATE
    DEC -.no re-encode.-> BUF
    GATE -->|passes| ANPR & REID
    GATE -.->|refused, ~80%| X["discarded"]
    ANPR & REID --> SIGHT["Sighting<br/>~5 kbps per camera"]

    SIGHT ==>|metadata only| BUS
    BUS --> MATCH --> RULES
    MATCH & RULES --> DB
    DB --> API --> UI
    RULES -->|WebSocket| UI
    BUF -.->|clip, on demand only|DB

    style X fill:#3d1418,stroke:#612025,color:#e5484d
    style SIGHT fill:#0d2a35,stroke:#17475a,color:#4cc9f0
    style BUF fill:#141b25,stroke:#33404f
```

---

## 2 · The spatio-temporal gate

Why an 85%-mAP appearance model is unusable against 49 cameras and
trustworthy against 3.

```mermaid
flowchart LR
    S["Sighting at camera A<br/>t = 10:04:12"] --> ADJ[("camera_adjacency<br/>road travel times<br/>from OSRM")]
    ADJ --> W["Reachable cameras<br/>+ arrival windows"]
    W --> F{"Did a sighting arrive<br/>inside the window?"}
    F -->|no| R["REJECTED<br/>never scored"]
    F -->|yes| SC["Fusion score"]

    SC --> P["plate · 0.45"]
    SC --> RE["appearance · 0.30"]
    SC --> CO["colour · 0.08"]
    SC --> TY["type · 0.07"]
    SC --> ST["reachability · 0.10"]

    P & RE & CO & TY & ST --> H["Hungarian assignment<br/>across the batch"]
    H --> D{"Decision band"}
    D -->|"≥ 0.80 with a plate"| AUTO["CONFIRMED"]
    D -->|"0.55 – 0.79"| PROB["PROBABLE<br/>operator confirms"]
    D -->|"< 0.55"| REJ["new vehicle"]

    style R fill:#3d1418,stroke:#612025,color:#e5484d
    style AUTO fill:#143024,stroke:#1d4a35,color:#2ea86b
    style PROB fill:#33230a,stroke:#4d3611,color:#d98a1a
```

Measured: **1.2 candidate cameras out of 49** in a 3-minute window — 97.6%
(**3.3 of 49** in a 5-minute window — 93.3%)
of comparisons never happen. Appearance alone can never reach CONFIRMED.

---

## 3 · Why ANPR is not enough

```mermaid
pie showData
    title Cameras that can physically resolve a plate (demo estate)
    "ANPR-capable" : 13
    "Wide-angle — plate never exceeds 90 px" : 37
```

74% of the estate is plate-blind. That is not a defect; it is what a
wide-angle junction camera is for. It is also the entire reason
re-identification exists in this design.

---

## 4 · Security zones for legacy DVRs

No inbound rules at any site. A compromised core cannot pivot into a camera
VLAN, because the gateway does not forward.

```mermaid
flowchart TB
    subgraph SITE["Police station / municipal site"]
        subgraph V10["VLAN 10 — cameras (no default gateway, no internet)"]
            DVR["Legacy DVR<br/>EOL firmware"]
            IPC["IP cameras"]
        end
        GW["Edge gateway<br/>ip_forward = 0<br/>credentials in RAM only"]
        DVR & IPC --> GW
    end

    GW ==>|"outbound only<br/>WireGuard + mTLS"| CORE["State core<br/>GSWAN / MeghRaj"]
    CORE -.->|"no inbound path"| X["✗"]

    style V10 fill:#3d1418,stroke:#612025,color:#f0a0a3
    style X fill:#070a0f,stroke:#612025,color:#e5484d
```

---

## 5 · Scaling shape

```mermaid
flowchart LR
    subgraph MVP["50 cameras — one command"]
        M1["1 ingestion process"] --> M2["Redis Streams"] --> M3["1 matcher"] --> M4[("PostgreSQL")]
    end

    subgraph DIST["3,000 — one district"]
        D1["12 ingestion pods<br/>GPU each"] --> D2["Redis Cluster"] --> D3["3–6 matchers<br/>one group"] --> D4[("PostgreSQL<br/>partitioned")]
    end

    subgraph STATE["80,000 — state"]
        S1["~2,000 edge sites<br/>K3s + GPU"] --> S2["Kafka<br/>partitioned by camera"] --> S3["matcher group<br/>per district"] --> S4[("ClickHouse + Milvus<br/>sharded by district")]
    end

    MVP -->|"more replicas"| DIST -->|"shard by district"| STATE
```

The application code is identical across all three. Only replica counts,
backing stores and placement change — see [SCALING.md](SCALING.md).
