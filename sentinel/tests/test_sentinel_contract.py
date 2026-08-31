"""Sentinel contract conformance.

The Phase 2B brief lists twenty things that must be true of the live-feed
integration. Most are asserted in the three suites that do the real work:

    test_sentinel_catalogue.py   discovery and reconciliation
    test_live_ingestion.py       protocol, PTS, codecs, reconnect (real RTSP)
    test_live_supervisor.py      pacing and one-capture-per-camera

This file does two jobs those cannot.

1. **It closes the three gaps** the brief names that nothing else covered:
   no seeking, no dependency on downloading a file, and never publishing
   to the gateway. Each is a NEGATIVE property -- the absence of a
   behaviour -- which is exactly the kind that no ordinary test exercises
   and that reappears the moment somebody debugging a stream adds a
   convenient `-ss`.

2. **It pins the checklist to the tests.** `test_every_contract_item_has_a
   _named_test` fails if a named test is renamed or deleted. Without it a
   checklist in a document drifts away from the suite silently, and the
   next reader trusts a tick that no longer means anything.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from ingestion import live_reader, sentinel_catalogue
from ingestion.live_reader import LiveStreamReader

ROOT = pathlib.Path(__file__).resolve().parents[1]
INGEST = ROOT / "video-ingestion" / "ingestion"


# ── [ ] no seeking ───────────────────────────────────────────────────

def test_the_live_path_never_seeks():
    """PART 11: live-only evaluation.

    A seek on a live stream is either meaningless or a lie. Against a
    genuinely live camera there is nothing to seek to; against a
    file-backed test source it silently converts the measurement into a
    fast-forward through recorded video, which is precisely the shortcut
    that makes a live-feed benchmark worthless.

    Checked in the AST rather than by behaviour because the failure is
    somebody ADDING a seek later, and no runtime test of correct behaviour
    catches an addition.
    """
    for path in (INGEST / "live_reader.py", INGEST / "live_supervisor.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name != "seek", f"{path.name}:{node.lineno} calls seek()"


def test_no_ffmpeg_seek_or_speed_option_reaches_a_live_input():
    """`-ss`, `-sseof`, `-itsoffset` and `-re` are all ways of saying "this
    is not live". `-re` in particular reads a FILE at native rate, which
    looks like a live stream and is not one."""
    forbidden = {"ss", "sseof", "itsoffset", "re", "stream_loop", "readrate"}
    opts = LiveStreamReader("rtsp://host/stream/x")._open_options()
    assert not (forbidden & set(opts)), (
        f"live input carries {forbidden & set(opts)}")


# ── [ ] no file-download dependency ──────────────────────────────────

def test_nothing_in_the_live_path_downloads_or_opens_a_local_file():
    """PART 11 again, from the other side.

    The demo estate is file-backed and that is fine -- it is labelled a
    demo. What must not happen is the LIVE path acquiring a file
    dependency: a cached clip, a downloaded sample, a temp file written
    then re-read. Each turns a live measurement into a recorded one and
    none of them is visible in the output.
    """
    src = (INGEST / "live_reader.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            assert name != "open", (
                f"live_reader.py:{node.lineno} opens a file")
            attr = getattr(node.func, "attr", None)
            assert attr not in ("urlretrieve", "download", "urlopen"), (
                f"live_reader.py:{node.lineno} downloads {attr}")
    for banned in ("requests.get", "httpx.get", "urllib.request"):
        assert banned not in src, f"live_reader.py references {banned}"


def test_a_file_url_is_not_accepted_as_a_live_camera():
    """A local path must not quietly become a camera. If it did, a
    misconfigured catalogue entry would produce a perfect-looking stream
    with fabricated liveness."""
    r = LiveStreamReader("/var/tmp/recorded.mp4", camera_id="x")
    assert not r.is_live_url, "a filesystem path was accepted as live"
    r2 = LiveStreamReader("file:///var/tmp/recorded.mp4", camera_id="x")
    assert not r2.is_live_url, "a file:// URL was accepted as live"
    assert LiveStreamReader("rtsp://host/stream/1").is_live_url
    assert LiveStreamReader("https://host/live/x.m3u8").is_live_url


# ── [ ] no gateway publishing ────────────────────────────────────────

def test_the_catalogue_client_never_writes_to_the_gateway():
    """We are a CONSUMER of the Sentinel estate, not a participant in it.

    A client that can POST to the catalogue can also corrupt it, and the
    blast radius of a bug in our reconciler would become the gateway's
    camera list rather than our own.
    """
    src = (INGEST / "sentinel_catalogue.py").read_text()
    for verb in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"',
                 "'POST'", "'PUT'", "'DELETE'", "'PATCH'"):
        assert verb not in src, f"catalogue client references {verb}"


def test_the_rtsp_client_never_announces_or_records():
    """RTSP ANNOUNCE and RECORD are how a client PUBLISHES media to a
    server. We only ever play. Publishing to a government gateway from a
    monitoring system would be, at best, an incident."""
    src = (INGEST / "live_reader.py").read_text()
    for method in ("ANNOUNCE", "RECORD"):
        assert method not in src, f"live_reader references RTSP {method}"


# ── [ ] the checklist cannot silently rot ────────────────────────────

#: Every item the brief names, and the test that holds it. A path of
#: "<file>::<test>" is checked to exist; the point is that renaming or
#: deleting one of these fails HERE, loudly, instead of leaving a ticked
#: box in a document that means nothing.
CONTRACT: dict[str, str] = {
    "GET /api/ingest":
        "test_sentinel_catalogue.py::test_the_catalogue_is_the_source_of_truth",
    "dynamic camera discovery":
        "test_sentinel_catalogue.py::test_a_camera_added_upstream_appears_without_a_code_change",
    "no hard-coded catalogue":
        "test_sentinel_catalogue.py::test_no_camera_identifier_is_hard_coded_in_the_ingestion_package",
    "RTSP URL from catalogue":
        "test_sentinel_catalogue.py::test_advertised_urls_are_used_verbatim_not_reconstructed",
    "WHEP URL from catalogue":
        "test_sentinel_catalogue.py::test_playback_urls_are_carried_through_for_protocol_routing",
    "HLS URL from catalogue":
        "test_sentinel_catalogue.py::test_playback_urls_are_carried_through_for_protocol_routing",
    "RTSP forced over TCP":
        "test_live_ingestion.py::test_rtsp_is_pinned_to_tcp",
    "PTS preservation":
        "test_live_ingestion.py::test_frame_timing_comes_from_pts_not_from_arrival",
    "variable frame interval":
        "test_ai_pipeline.py::test_irregular_frame_intervals_are_preserved_end_to_end",
    "H.264":
        "test_live_ingestion.py::test_both_codecs_decode_over_rtsp",
    "H.265":
        "test_live_ingestion.py::test_both_codecs_decode_over_rtsp",
    "reconnect backoff":
        "test_live_ingestion.py::test_backoff_is_exponential_capped_and_jittered",
    "reconnect jitter":
        "test_live_ingestion.py::test_backoff_is_exponential_capped_and_jittered",
    "scene discontinuity":
        "test_ai_pipeline.py::test_a_discontinuity_does_not_swallow_the_next_vehicle",
    "mixed resolutions":
        "test_live_ingestion.py::test_aspect_ratio_is_preserved_rather_than_stretched",
    "mixed frame rates":
        "test_live_ingestion.py::test_a_low_frame_rate_camera_is_not_treated_as_a_failure",
    "no seeking":
        "test_sentinel_contract.py::test_the_live_path_never_seeks",
    "no file-download dependency":
        "test_sentinel_contract.py::test_nothing_in_the_live_path_downloads_or_opens_a_local_file",
    "no gateway publishing":
        "test_sentinel_contract.py::test_the_catalogue_client_never_writes_to_the_gateway",
    "controlled connection pacing":
        "test_live_supervisor.py::test_connections_are_staggered_rather_than_opened_at_once",
}


@pytest.mark.parametrize("item,ref", sorted(CONTRACT.items()))
def test_every_contract_item_has_a_named_test(item, ref):
    filename, test_name = ref.split("::")
    path = pathlib.Path(__file__).parent / filename
    assert path.exists(), f"{item}: {filename} is missing"
    tree = ast.parse(path.read_text())
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert test_name in names, (
        f"'{item}' is held by {ref}, which no longer exists. Either restore "
        f"it or point this entry at whatever replaced it -- do not delete "
        f"the row, because that is how a checklist stops meaning anything.")


def test_the_checklist_covers_every_item_the_brief_names():
    """Guards the other direction: an item dropped from CONTRACT would
    otherwise make the suite pass by covering less."""
    assert len(CONTRACT) == 20, (
        f"the brief names 20 contract items, CONTRACT has {len(CONTRACT)}")
