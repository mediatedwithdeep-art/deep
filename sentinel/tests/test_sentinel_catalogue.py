"""Catalogue discovery and reconciliation, against a real gateway.

PART 3's requirement is that `GET /api/ingest` is the source of truth and
that nothing is hard-coded. These tests run against the sandbox gateway,
which serves the contract's URL shapes over real HTTP, and they also feed
the parser deliberately awkward payloads — because the real Sentinel
gateway's field names are unknown and a parser that only handles one
spelling would fail silently against it.
"""

from __future__ import annotations

import pytest

from ingestion.camera_config import CameraSpec
from ingestion.sentinel_catalogue import (
    CatalogueError, fetch_catalogue, load_from_sentinel, parse_catalogue,
    reconcile,
)
from sentinel_sandbox.gateway import SandboxCamera, SandboxGateway
from sentinel_sandbox.rtsp_server import make_clip


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> str:
    p = tmp_path_factory.mktemp("cat") / "c.mp4"
    return make_clip(str(p), "h264", seconds=2.0, fps=10.0)


@pytest.fixture
def gateway(clip):
    gw = SandboxGateway()
    gw.add(SandboxCamera("cam-001", "Jodhpur Cross Roads", 23.027, 72.512,
                         heading_deg=47, media_path=clip, anpr_capable=True))
    gw.add(SandboxCamera("cam-002", "Nehru Bridge North", 23.021, 72.575,
                         heading_deg=190, media_path=clip))
    gw.add(SandboxCamera("cam-003", "Iskcon Junction", 23.028, 72.507,
                         media_path=clip))
    gw.start()
    yield gw
    gw.stop()


# ── discovery ────────────────────────────────────────────────────────

def test_the_catalogue_is_the_source_of_truth(gateway):
    specs, report = load_from_sentinel(gateway.catalogue_url)
    assert {s.camera_id for s in specs} == {"cam-001", "cam-002", "cam-003"}
    assert report.entry_count == 3
    assert report.envelope_key == "streams"


def test_advertised_urls_are_used_verbatim_not_reconstructed(gateway):
    specs, _ = load_from_sentinel(gateway.catalogue_url)
    advertised = {s["id"]: s["rtsp_url"] for s in gateway.catalogue()["streams"]}
    for spec in specs:
        assert spec.stream_url == advertised[spec.camera_id]


def test_playback_urls_are_carried_through_for_protocol_routing(gateway):
    """RTSP feeds the AI; the browser gets WHEP with HLS as fallback. All
    three come from the catalogue rather than from string surgery on a
    port number."""
    specs, _ = load_from_sentinel(gateway.catalogue_url)
    s = next(x for x in specs if x.camera_id == "cam-001")
    assert s.stream_url.startswith("rtsp://")
    assert s.extra["whep_url"].endswith("/stream/cam-001/whep")
    assert s.extra["hls_url"].endswith("/live/stream/cam-001/index.m3u8")


def test_capability_metadata_survives_discovery(gateway):
    specs, _ = load_from_sentinel(gateway.catalogue_url)
    by_id = {s.camera_id: s for s in specs}
    assert by_id["cam-001"].anpr_capable is True
    assert by_id["cam-002"].anpr_capable is False
    assert by_id["cam-001"].heading_deg == 47
    assert by_id["cam-003"].heading_deg is None


def test_a_camera_added_upstream_appears_without_a_code_change(gateway, clip):
    before, _ = load_from_sentinel(gateway.catalogue_url)
    gateway.add(SandboxCamera("cam-004", "Paldi Crossing", 23.01, 72.56,
                              media_path=clip))
    after, _ = load_from_sentinel(gateway.catalogue_url)
    assert len(after) == len(before) + 1
    assert "cam-004" in {s.camera_id for s in after}


def test_an_unreachable_gateway_raises_a_clear_error():
    with pytest.raises(CatalogueError, match="unreachable"):
        fetch_catalogue("http://127.0.0.1:9", timeout_s=2.0)


def test_the_client_only_ever_issues_get(gateway):
    """It performs read-only discovery: it must never publish a stream or
    call a control API on the gateway."""
    import inspect

    from ingestion import sentinel_catalogue as mod
    src = inspect.getsource(mod)
    for verb in ('method="POST"', 'method="PUT"', 'method="DELETE"',
                 'method="PATCH"'):
        assert verb not in src, f"catalogue client contains {verb}"
    assert 'method="GET"' in src


# ── tolerance to the real gateway's unknown field names ──────────────

def test_alternative_field_spellings_are_understood_and_reported():
    """The real gateway's field names are unknown. A parser that handles
    one spelling would produce an empty estate and no explanation."""
    doc = {"items": [{
        "streamId": "AHM-014",
        "title": "Gujarat College Cross",
        "lat": "23.0201", "lng": "72.5510",
        "bearing": 275,
        "uri": "rtsp://10.20.0.4:8554/stream/AHM-014",
        "res": "1920x1080", "frame_rate": "25",
        "lpr": "yes",
    }]}
    specs, report = parse_catalogue(doc)
    assert len(specs) == 1
    s = specs[0]
    assert s.camera_id == "AHM-014"
    assert s.name == "Gujarat College Cross"
    assert (s.latitude, s.longitude) == (23.0201, 72.5510)
    assert s.heading_deg == 275
    assert (s.width, s.height) == (1920, 1080)
    assert s.fps == 25.0
    assert s.anpr_capable is True
    # and it must say which spellings it used, so a mismatch is diagnosable
    assert report.resolved["id"] == "streamId"
    assert report.resolved["rtsp"] == "uri"
    assert "1 entries under items" in report.summary()


def test_a_bare_list_and_an_id_keyed_mapping_are_both_accepted():
    bare = [{"id": "a", "rtsp_url": "rtsp://h:8554/stream/a"}]
    assert len(parse_catalogue(bare)[0]) == 1
    mapping = {"cam-9": {"rtsp_url": "rtsp://h:8554/stream/cam-9"}}
    specs, report = parse_catalogue(mapping)
    assert specs[0].camera_id == "cam-9"
    assert report.envelope_key == "<mapping>"


def test_missing_urls_are_derived_from_the_documented_shape():
    """Derivation is a fallback, never the primary source."""
    doc = {"streams": [{"id": "cam-77"}]}
    specs, _ = parse_catalogue(doc, gateway_host="10.9.9.9")
    s = specs[0]
    assert s.stream_url == "rtsp://10.9.9.9:8554/stream/cam-77"
    assert s.extra["whep_url"] == "http://10.9.9.9:8889/stream/cam-77/whep"
    assert s.extra["hls_url"] == \
        "http://10.9.9.9:8888/live/stream/cam-77/index.m3u8"


def test_one_malformed_entry_does_not_take_the_estate_offline():
    doc = {"streams": [
        {"id": "good-1", "rtsp_url": "rtsp://h:8554/stream/good-1"},
        {"no_identifier_at_all": True},
        {"id": "good-2", "rtsp_url": "rtsp://h:8554/stream/good-2"},
    ]}
    specs, _ = parse_catalogue(doc)
    assert {s.camera_id for s in specs} == {"good-1", "good-2"}


def test_a_camera_without_coordinates_is_flagged_not_placed_in_the_atlantic():
    """(0, 0) is in the Gulf of Guinea. Silently defaulting there would
    corrupt every travel-time estimate through that camera."""
    doc = {"streams": [{"id": "cam-x", "rtsp_url": "rtsp://h:8554/stream/cam-x"}]}
    specs, _ = parse_catalogue(doc)
    assert specs[0].extra["geolocated"] is False


def test_no_camera_identifier_is_hard_coded_in_the_ingestion_package():
    """PART 3: the catalogue is discovery, not configuration."""
    import ast
    import pathlib
    import re

    pkg = pathlib.Path(__file__).resolve().parents[1] / "video-ingestion" / "ingestion"
    pattern = re.compile(r"\bcam-\d{3,}\b|\bAHM-[A-Z]{2,}-\d+\b")
    offenders = []
    for path in pkg.glob("*.py"):
        tree = ast.parse(path.read_text())
        # Only executable string constants count. Docstrings and comments
        # illustrate usage; they are documentation, not configuration.
        docstrings = {
            ast.get_docstring(n, clean=False)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                              ast.AsyncFunctionDef))
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and pattern.search(node.value)
                    and node.value not in docstrings):
                offenders.append(
                    f"{path.name}:{node.lineno}: {node.value[:70]}")
    assert not offenders, "hard-coded camera identifiers:\n" + "\n".join(offenders)


# ── reconciliation ───────────────────────────────────────────────────

def _spec(cam_id: str, **kw) -> CameraSpec:
    base = dict(camera_id=cam_id, name=cam_id, latitude=23.0, longitude=72.5,
                stream_url=f"rtsp://h:8554/stream/{cam_id}")
    base.update(kw)
    return CameraSpec(**base)


def test_reconcile_detects_additions_removals_and_material_changes():
    running = {"a": _spec("a"), "b": _spec("b"), "c": _spec("c")}
    catalogue = [
        _spec("a"),                                   # unchanged
        _spec("b", stream_url="rtsp://h:8554/stream/b-new"),   # moved
        _spec("d"),                                   # new
    ]                                                 # c disappeared
    r = reconcile(catalogue, running)
    assert [s.camera_id for s in r.added] == ["d"]
    assert r.removed == ["c"]
    assert [s.camera_id for s, _ in r.changed] == ["b"]
    assert r.unchanged == ["a"]
    assert not r.is_noop
    assert "+1 added, -1 removed, ~1 changed" in r.summary()


def test_a_cosmetic_rename_does_not_tear_down_a_working_stream():
    running = {"a": _spec("a", name="Old Name")}
    r = reconcile([_spec("a", name="New Name")], running)
    assert r.is_noop and r.unchanged == ["a"]


def test_an_identical_catalogue_is_a_noop():
    running = {"a": _spec("a"), "b": _spec("b")}
    assert reconcile([_spec("a"), _spec("b")], running).is_noop


def test_a_camera_removed_upstream_is_retired(gateway):
    specs, _ = load_from_sentinel(gateway.catalogue_url)
    running = {s.camera_id: s for s in specs}
    gateway.hide("cam-002")
    after, _ = load_from_sentinel(gateway.catalogue_url)
    r = reconcile(after, running)
    assert r.removed == ["cam-002"]


# ── layering: catalogue + survey overlay ─────────────────────────────

def test_load_cameras_discovers_the_estate_from_the_catalogue(gateway):
    from ingestion.camera_config import load_cameras
    specs = load_cameras(catalogue_url=gateway.catalogue_url)
    assert {s.camera_id for s in specs} >= {"cam-001", "cam-002", "cam-003"}


def test_a_survey_overlay_can_supply_a_heading_the_gateway_lacks(gateway, tmp_path):
    """Without a heading a camera is a dot with no field of view, and the
    adjacency graph is directional -- so the gate gets materially weaker."""
    from ingestion.camera_config import load_cameras
    overlay = tmp_path / "survey.yaml"
    overlay.write_text(
        "cameras:\n"
        "  - {camera_id: cam-003, name: Iskcon Junction, latitude: 23.028,\n"
        "     longitude: 72.507, heading_deg: 312}\n")
    specs = {s.camera_id: s for s in load_cameras(
        catalogue_url=gateway.catalogue_url, yaml_path=str(overlay))}
    assert specs["cam-003"].heading_deg == 312
    # and the rest of the estate still comes from the catalogue
    assert "cam-001" in specs


def test_an_unreachable_gateway_does_not_take_the_estate_offline(tmp_path):
    """A catalogue outage must not remove cameras that are working."""
    from ingestion.camera_config import load_cameras
    overlay = tmp_path / "known.yaml"
    overlay.write_text(
        "cameras:\n"
        "  - {camera_id: KNOWN-1, name: Already running, latitude: 23.0,\n"
        "     longitude: 72.5}\n")
    specs = load_cameras(catalogue_url="http://127.0.0.1:9",
                         yaml_path=str(overlay))
    assert [s.camera_id for s in specs] == ["KNOWN-1"]
