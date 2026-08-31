# Requirement Traceability Matrix

**346 tests collected** · **45 API operations across 41 paths** · **10 migrations** · Phase 2B, 2026-08-31

Status vocabulary — used strictly, with no fourth option smuggled in:

| Status | Meaning |
|---|---|
| **IMPLEMENTED** | Code exists and runs. No test proves the specific property. |
| **TESTED** | An executed test asserts the property. Named in the Test column. |
| **SIMULATED** | Runs against the simulation backend only. Real-world figure unknown. |
| **DESIGNED** | Written down, argued, not built. |
| **PENDING EXTERNAL ACCESS** | Blocked on something outside this project's reach. |

---

## PART 3–5 · Sentinel integration, protocol routing, transport

| Requirement | Implementation | Evidence | Test | Demo | Status | Remaining risk |
|---|---|---|---|---|---|---|
| `GET /api/ingest` is the source of truth | `sentinel_catalogue.load_from_sentinel` | 50 cameras discovered from the sandbox gateway | `test_the_catalogue_is_the_source_of_truth` | 3 | **TESTED** | Field names in the real gateway are assumed; the parser accepts several spellings and reports which it resolved |
| Never hard-code the catalogue | Catalogue → `CameraSpec`; no literals | AST scan of the whole ingestion package | `test_no_camera_identifier_is_hard_coded_in_the_ingestion_package` | 3 | **TESTED** | — |
| New camera appears with no code change | Reconciler add/remove/change | Camera added upstream mid-run is opened | `test_a_camera_added_upstream_appears_without_a_code_change` | 3 | **TESTED** | — |
| Removed camera is retired | `LiveEstate.remove` | Reader stopped and closed | `test_a_camera_removed_upstream_is_retired` | — | **TESTED** | — |
| Cosmetic change does not restart a stream | Material-change filter | Rename leaves the reader running | `test_a_cosmetic_rename_does_not_tear_down_a_working_stream` | — | **TESTED** | — |
| RTSP → AI pipeline | `LiveStreamReader` (PyAV) | Real RTSP 1.0 server, real RTP | `test_both_codecs_decode_over_rtsp` | 6 | **TESTED** | Only against our sandbox, not Sentinel |
| WHEP → browser | `whep_url` served from the catalogue | Migration 0009; served verbatim | `test_playback_urls_are_served_verbatim_from_the_catalogue` | 3 | **TESTED** | — |
| HLS fallback | `hls_url` from the catalogue | Same | Same | 3 | **TESTED** | — |
| Browser never consumes RTSP | API returns no RTSP URL on any route | Four routes asserted | `test_no_camera_route_ever_returns_a_credential_or_an_rtsp_url` | 3 | **TESTED** | — |
| RTSP forced over TCP | `rtsp_transport=tcp`, no config path to UDP | Sandbox refuses UDP with 461 | `test_rtsp_is_pinned_to_tcp`, `test_the_sandbox_refuses_udp_so_a_misconfigured_client_is_loud` | 6 | **TESTED** | — |
| **Real Sentinel gateway** | — | — | — | — | **PENDING EXTERNAL ACCESS** | Host, credentials and API docs were never available |

## PART 6–8 · Timing, variable FPS, codecs

| Requirement | Implementation | Evidence | Test | Demo | Status | Remaining risk |
|---|---|---|---|---|---|---|
| All timing derived from PTS | `Frame.pts_time`, `capture_time` anchored once | PTS 0.8 s vs arrival 11 ms on connect | `test_frame_timing_comes_from_pts_not_from_arrival` | 6 | **TESTED** | Without RTCP NTP the anchor carries a constant one-way delay; intervals are unaffected |
| Never use arrival time | Removed; worker passes `frame.capture_time` | Worker builds `LiveStreamReader`, not `FrameReader` | `test_capture_time_is_monotonic_and_spans_real_video_time` | 6 | **TESTED** | — |
| Never use `CAP_PROP_FPS` / forced CFR | `-vf fps=` removed; `FrameReader` **deleted**, not deprecated | FPS from PTS reproduced 15.00/12.00/4.00 exactly; AST scan of every ingestion module | `test_no_forced_constant_frame_rate_survives_anywhere_in_ingestion`, `test_the_removed_cfr_reader_has_not_come_back` | 6 | **TESTED** | — |
| PTS carried to tracker | `age_s`, `velocity` px/s, `predict(dt)` | 200 px/s source read as 12.7 px/s before the fix | `test_irregular_frame_intervals_are_preserved_end_to_end` | — | **TESTED** | — |
| Tracker retires by elapsed time, not frames | `max_age_s` | 6 fps and 25 fps cameras retire together | `test_track_age_is_seconds_so_slow_and_fast_cameras_agree` | — | **TESTED** | — |
| Irregular intervals preserved (0/40/120/165 ms) | Per-interval velocity and prediction | Brief's own example | `test_irregular_frame_intervals_are_preserved_end_to_end`, `test_prediction_scales_with_the_real_gap` | — | **TESTED** | — |
| Travel-time gate uses PTS | Gate consumes `Sighting.timestamp` = capture time | Falls out of the above | `test_event_processor.py` gate suite | 9 | **TESTED** | — |
| Variable FPS tolerated | Status from measured, not declared, rate | 4 fps camera ONLINE; declared-25-delivering-4 DEGRADED | `test_a_low_frame_rate_camera_is_not_treated_as_a_failure` | 6 | **TESTED** | — |
| Mixed resolutions | Aspect ratio preserved | 4:3 source stays 4:3 | `test_aspect_ratio_is_preserved_rather_than_stretched` | — | **TESTED** | Letterbox not implemented; scale-by-long-edge only |
| H.264 **and** H.265 | Both decoded over RTSP/TCP | 43 × H.264 + 7 × H.265 concurrently | `test_both_codecs_decode_over_rtsp` | 6 | **TESTED** | — |

## PART 9–13 · Resilience and the live POC

| Requirement | Implementation | Evidence | Test | Demo | Status | Remaining risk |
|---|---|---|---|---|---|---|
| Backoff 2→4→8→16→30 s with jitter | `backoff_delay` | 44 real recoveries in the 50-camera run | `test_backoff_is_exponential_capped_and_jittered` | 14 | **TESTED** | — |
| ONLINE / DEGRADED / OFFLINE / RECONNECTING | `CameraStatus` + migration 0007 | RECONNECTING observed distinct from OFFLINE | `test_a_dropped_stream_reconnects_and_reports_reconnecting` | 14 | **TESTED** | — |
| Reader drives the state machine | `ReaderHealth.status` | Worker health reads from it | `test_an_unreachable_camera_goes_offline_without_raising` | 14 | **TESTED** | — |
| Scene discontinuity ends tracks | `reset_for_discontinuity` | Same frames signalled vs not: 2 sightings vs 1 fused | `test_a_discontinuity_does_not_swallow_the_next_vehicle` | — | **TESTED** | — |
| Discontinuity ≠ dropped frame | Reader raises only on non-monotonic/jumped PTS | A single dropped frame does not break a track | `test_an_ordinary_gap_does_not_end_a_track` | — | **TESTED** | — |
| ReID gallery not contaminated | `_track_state` cleared; new track = new state | Two vehicles never share a descriptor | `test_a_discontinuity_does_not_swallow_the_next_vehicle` | — | **TESTED** | — |
| Live-only evaluation, no seeking | AST scan; no `-ss`/`-re`; non-live scheme refused | `start()` refuses a file path | `test_the_live_path_never_seeks`, `test_a_file_url_is_not_accepted_as_a_live_camera` | — | **TESTED** | — |
| No file-download dependency | Live path opens nothing | AST scan | `test_nothing_in_the_live_path_downloads_or_opens_a_local_file` | — | **TESTED** | — |
| Never publish to the gateway | GET only; no ANNOUNCE/RECORD | Source scan | `test_the_catalogue_client_never_writes_to_the_gateway` | — | **TESTED** | — |
| Connection pacing | Stagger + concurrency cap | 50 opens over 16.9 s; peak in-flight capped | `test_connections_are_staggered_rather_than_opened_at_once`, `test_concurrent_opens_are_capped` | — | **TESTED** | — |
| One capture per camera | Reader registry | Two consumers share one decode | `test_exactly_one_capture_exists_per_camera` | — | **TESTED** | — |
| `SENTINEL_LIVE_TEST_REPORT.md` | 50-camera measured run | 68,606 frames, 672.9 fps, 0 OFFLINE | — | — | **TESTED** | Against our sandbox, not Sentinel |

## PART 14–17 · Analytics quality

| Requirement | Implementation | Evidence | Test | Demo | Status | Remaining risk |
|---|---|---|---|---|---|---|
| Full designated-vehicle pipeline | detect → track → gate → ANPR + ReID + colour + type, fused | Benchmark suites 1–5 | `test_ai_pipeline.py` | 4–8 | **SIMULATED** | Never run on real imagery |
| ANPR quality gate | `quality.assess`, ~80% rejection | Benchmark suite 3 | `test_gate_allows_anpr_on_a_large_sharp_daylight_crop` | 5 | **SIMULATED** | Thresholds tuned against the simulator |
| ANPR accuracy by camera class | 92.5% day lane / 37.9% night wide-angle | Benchmark suite 3, reproduced | — | 5 | **SIMULATED** | Simulation backend's numbers, labelled as such |
| Camera capability metadata | `anpr_capable` per camera | Drives gate and role | `test_capability_metadata_survives_discovery` | 3 | **TESTED** | Set by hand; not derived from catalogue optics |
| Cross-camera ReID with confidence | Fusion score, CONFIRMED vs PROBABLE | `NO_PLATE_CEILING = 0.79` | `test_fusion.py` (14 tests) | 8 | **TESTED** | Appearance can never auto-confirm — a safety property, not a tuning choice |
| Gate reduction measured | Suite 7 | 1.2 of 49 at 180 s; 3.3 at 300 s | `test_event_processor.py` | 9 | **TESTED** | — |
| Gate reports pairs and ms | `MatcherStats` counts scored pairs and the ungated counterfactual; gate and scorer timed separately | 112 µs/pair measured; 20,000 pairs ungated vs 473 gated; 2,240 ms vs 53 ms | `test_the_gate_is_measured_in_pairs_scored_not_only_in_candidates`, `test_the_gate_reports_milliseconds_not_only_counts` | 9 | **TESTED** | Per-pair cost is the simulation backend's scorer; the pair counts are exact |

## PART 18–22 · Integration, departments, federation

| Requirement | Implementation | Evidence | Test | Demo | Status | Remaining risk |
|---|---|---|---|---|---|---|
| VAHAN / SARTHI / eGujCop / AFIS / NAFIS adapters | `sentinel_core.govt`, 5 adapters | 33 tests | `test_govt_integration.py` | 13 | **TESTED** (mock backends) | No real endpoint |
| Real vs mock is unambiguous | `Provenance` on every record, frozen | Banner reaches the API response | `test_every_mock_record_is_stamped_mock` | 13 | **TESTED** | — |
| Data minimisation by purpose | `RELEASE` per purpose, applied in `lookup()` | Screening releases no personal data | `test_screening_releases_a_status_flag_and_no_personal_data` | 13 | **TESTED** | — |
| ANPR → plate → adapter → rule → alert | `intelligence.screen_plate` | End-to-end with provenance | `test_the_full_intelligence_path_produces_a_provenance_stamped_hit` | 13 | **TESTED** | — |
| Auth, timeout, audit, rate limit, failure handling | `GovtAdapter` wraps every call | Quota, timeout, 501-not-503 | `test_the_local_quota_is_enforced_on_our_side_of_the_call` | 13 | **TESTED** | — |
| `GOVERNMENT_INTEGRATION.md` | Written | — | — | — | **TESTED** | — |
| **Real government data** | `RealBackend` raises, naming the requirement | — | `test_a_real_backend_refuses_and_names_what_is_missing` | — | **PENDING EXTERNAL ACCESS** | Five separate institutional processes |
| 26 departments | Seeded | `departments 26` | Seed output | 2 | **IMPLEMENTED** | 6 own cameras in the Ahmedabad demo; the rest are seeded empty rather than given invented estates |
| Roles: State/Dept Admin, Operator, Investigator, Auditor | `Role` enum + `PERMISSIONS` | Migration 0008 | `test_the_auditor_cannot_watch_the_estate` | 1 | **TESTED** | — |
| **Department-scoped access enforced** | `dept_filter` on every query | 25 tests, two departments | `test_a_department_user_cannot_list_another_departments_cameras` | 1 | **TESTED** | Enforced at the query layer, not the UI |
| Cross-department write blocked | Scoped UPDATE/DELETE; POST refuses | Camera stays ONLINE after a foreign DELETE | `test_a_department_admin_cannot_disable_another_departments_camera` | 1 | **TESTED** | — |
| VMS federation models 1–4 | Three models compared with arithmetic | `NETWORK_BANDWIDTH_PLAN.md` §1 | — | 15 | **DESIGNED** | Three models, not four; the fourth (peer-to-peer mesh) was judged not credible at this scale and is not written up |
| Legacy analog / DVR | Vendor URL templates, DVR adapter | `ingestion/adapters.py` | `test_ingestion.py` | 3 | **IMPLEMENTED** | Never tested against real hardware |
| Edge / regional / central | Three tiers with per-link budgets | `NETWORK_BANDWIDTH_PLAN.md` §1 | — | 15 | **DESIGNED** | No edge appliance exists |

## PART 23–30 · Scale, storage, network, DR

| Requirement | Implementation | Evidence | Test | Demo | Status | Remaining risk |
|---|---|---|---|---|---|---|
| Load simulator | `scripts/scale_test.py` | 50 / 250 / 500 / 1,000 measured | — | 16 | **TESTED** | Event path only; no video decode |
| `SCALE_BENCHMARK.md` | Written, every cell labelled | Two controls isolate the simulator's share | — | 16 | **TESTED** | — |
| Capacity at 50/1k/3k/10k/50k/80k | Table with per-cell provenance | 10 stated assumptions | — | 16 | **PROJECTED** above 1,000 | Nothing above 1,000 cameras was run |
| Bandwidth: centralised vs federated vs hybrid | `NETWORK_BANDWIDTH_PLAN.md` | 320 Gbps vs 96 Mbps | — | 15 | **DESIGNED** | No WAN measured |
| `DISASTER_RECOVERY.md` | Nine domains, RPO/RTO, 1 min / 10 min / 1 h | — | — | — | **DESIGNED** | Only the camera and edge rows are tested; no failover has been executed |
| `INFRASTRUCTURE_SIZING.md` | Node archetypes and build-out, 50 → 80,000 | GPU-bound at 40 cameras/GPU; 500 edge nodes at 80,000 | — | 16 | **DESIGNED** | No edge appliance exists; GPU rows inherit assumption A7 |
| `COST_BENEFIT.md` | Federated vs centralised, year 1 | ₹82.7 cr vs ₹138.7 cr; storage + backbone are ₹57.6 cr of the ₹56 cr gap | — | 15 | **DESIGNED** | No procurement quote was obtained; unit costs are estimates against list prices |
| `STATEWIDE_ROLLOUT.md` | Five phases, per-district sequence, entry criteria | Phase 1 entry criteria listed, all currently unmet | — | — | **DESIGNED** | Commits to no dates; depends on external authorisations |
| Tiered HOT/WARM/COLD storage | `hot_days`/`warm_days`/`cold_days` per table, ordered by CHECK; `partition_tier()`, `storage_tier_report()`, `detach_cold_partitions()` | `vehicle_sighting` at 7/15/30; tiers resolve HOT→WARM→COLD→EXPIRED at 1/10/20/99 days | `test_a_partition_moves_through_the_tiers_as_it_ages`, `test_cold_detach_removes_a_partition_from_the_query_path_without_dropping_it` (8 tests) | — | **TESTED** | COLD detaches for archival; only `drop_old_partitions()` deletes. No object-store export job exists yet |
| District sharding | Adjacency graph is local by construction | `SCALING.md` | — | — | **DESIGNED** | Never exercised across two districts |

## Cross-cutting · Security

| Requirement | Implementation | Evidence | Test | Demo | Status |
|---|---|---|---|---|---|
| Authentication | JWT, forged tokens rejected | 6 routes asserted 401 | `test_every_camera_route_refuses_an_unauthenticated_caller` | 1 | **TESTED** |
| Authorisation by role | `PERMISSIONS` map | Auditor blocked from cameras | `test_the_auditor_cannot_watch_the_estate` | 1 | **TESTED** |
| Department isolation | `dept_filter` | A cannot see B | `test_a_department_user_cannot_list_another_departments_cameras` | 1 | **TESTED** |
| Secrets never logged or returned | `credential_ref` only; schema-level test | No route returns `vault://` | `test_no_camera_route_ever_returns_a_credential_or_an_rtsp_url` | — | **TESTED** |
| Camera credentials protected | No column can hold a plaintext credential | `test_database.py` schema assertion | `test_database.py` | — | **TESTED** |
| Input validation | Pydantic bounds; parameterised SQL | Out-of-range coords 422; SQL metachar is data | `test_a_sql_metacharacter_in_a_filter_is_data_not_syntax` | — | **TESTED** |
| Audit logging | `audit_log`, partitioned | Denials audited before refusal | `test_a_cross_department_denial_is_written_to_the_audit_log` | 12 | **TESTED** |
| Unauthorised camera access blocked | 404, not 403 | Enumeration cannot confirm existence | `test_fetching_another_departments_camera_by_id_is_not_found` | 1 | **TESTED** |

---

## The five things this submission cannot claim

Collected here so no reader has to assemble them from the table.

1. **Nothing has run against the real Sentinel gateway.** Every live-feed
   result is against `tools/sentinel_sandbox` — a real RTSP 1.0 server with
   real RTP and real PTS, but ours.
2. **No government record system is connected.** Five adapters, five mock
   backends, every record stamped `MOCK`.
3. **AI accuracy is the simulation backend's.** 92.5% day / 37.9% night are
   reproducible and honest about what they measure, and they measure a
   simulator.
4. **Nothing above 1,000 cameras was executed.** Everything from 3,000
   upward is arithmetic over stated assumptions.
5. **`make demo` has never been run end to end.** The Compose file
   validates (`docker compose config -q` passes) but the image layers are
   unreachable from this network — the Docker blob CDN returns 403 by
   policy, re-confirmed this session. What *has* been run end to end is the
   host-native path: all 10 migrations onto a virgin database, the 26-department
   seed, the API serving that estate over HTTP with a real JWT login, and
   45 operations across 41 paths in the generated OpenAPI document.
