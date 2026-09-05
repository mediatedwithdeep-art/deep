"""A minimal, real RTSP 1.0 server — the local stand-in for the Sentinel gateway.

WHY THIS EXISTS
───────────────
The Sentinel sandbox host and credentials were not available to this team,
but the *contract* is known: RTSP on :8554/stream/<id>, WHEP on
:8889/stream/<id>/whep, HLS on /live/stream/<id>/index.m3u8. Testing the
live-feed path against a mock object would have proved nothing — the bug
that broke every RTSP camera in Phase 1 (`-stimeout`, removed after ffmpeg
4.x) was invisible to unit tests and only appeared when a real ffmpeg was
asked to open a real RTSP URL.

So this is not a mock. It is a real RTSP server: it speaks the real
protocol, carries real RTP with real timestamps from real H.264/H.265
encoders, and can drop connections and loop media on demand. Pointing the
system at the genuine Sentinel sandbox is then a change of one base URL.

WHAT IT DELIBERATELY DOES NOT ALLOW
───────────────────────────────────
UDP transport is refused with 461 Unsupported Transport. The requirement is
that the estate is TCP-only — UDP loss over a shared government WAN arrives
as corruption rather than loss, and produces green smears that a detector
happily finds vehicles in. A server that merely *prefers* TCP cannot prove
a client is configured correctly; one that refuses UDP can.

SCOPE
─────
Enough RTSP to be genuinely exercised by ffmpeg/ffprobe: OPTIONS,
DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER, with RTP and RTCP
interleaved over the control connection (RFC 2326 §10.12). It is a test
fixture, not a production media server.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("sentinel.sandbox.rtsp")

RTSP_VERSION = "RTSP/1.0"
PUBLIC = "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"


@dataclass
class MediaSource:
    """One camera's media, and how it should behave.

    `drop_after_s` and `loop` exist so resilience can be tested rather than
    asserted: a camera that never fails proves nothing about reconnection.
    """
    camera_id: str
    path: str                      # backing media file
    codec: str = "h264"            # h264 | hevc
    loop: bool = True              # loop forever → real scene discontinuity
    drop_after_s: float | None = None   # kill the session to force reconnect
    _sdp: str | None = field(default=None, repr=False)


class _Session:
    __slots__ = ("id", "source", "proc", "udp", "rtcp_udp", "playing",
                 "started_at", "packets", "channel")

    def __init__(self, session_id: str, source: MediaSource):
        self.id = session_id
        self.source = source
        self.proc: subprocess.Popen | None = None
        self.udp: socket.socket | None = None
        self.rtcp_udp: socket.socket | None = None
        self.playing = False
        self.started_at = 0.0
        self.packets = 0
        self.channel = 0


class RtspServer:
    """Threaded RTSP server. One thread per connection; fine for ~50 clients."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8554,
                 path_prefix: str = "/stream"):
        self.host = host
        self.port = port
        self.path_prefix = path_prefix.rstrip("/")
        self.sources: dict[str, MediaSource] = {}
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        # Observability, so tests can assert on what the server saw rather
        # than on what the client believes it did.
        self.stats = {
            "describes": 0, "setups": 0, "plays": 0, "teardowns": 0,
            "udp_refused": 0, "connections": 0, "forced_drops": 0,
        }

    # ── registration ──────────────────────────────────────────────────

    def add(self, source: MediaSource) -> None:
        self.sources[source.camera_id] = source

    def rtsp_url(self, camera_id: str) -> str:
        return f"rtsp://{self.host}:{self.port}{self.path_prefix}/{camera_id}"

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        # If port 0 was requested, learn what we actually got.
        self.port = self._sock.getsockname()[1]
        self._sock.listen(64)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="rtsp-accept")
        self._thread.start()
        log.info("RTSP server on rtsp://%s:%d%s/<camera_id>",
                 self.host, self.port, self.path_prefix)

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def __enter__(self) -> "RtspServer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ── accept ────────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            self.stats["connections"] += 1
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True,
                             name=f"rtsp-conn-{addr[1]}").start()

    # ── one connection ────────────────────────────────────────────────

    def _serve(self, conn: socket.socket, addr) -> None:
        session: _Session | None = None
        buf = b""
        conn.settimeout(30.0)
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\r\n\r\n" in buf:
                    head, _, buf = buf.partition(b"\r\n\r\n")
                    text = head.decode("utf-8", "replace")
                    session = self._handle(conn, text, session)
                    if session is None and "TEARDOWN" in text:
                        return
        finally:
            if session:
                self._end_session(session)
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, conn: socket.socket, text: str,
                session: _Session | None) -> _Session | None:
        lines = text.split("\r\n")
        if not lines or " " not in lines[0]:
            return session
        method, url, *_ = lines[0].split()
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        cseq = headers.get("cseq", "0")

        def reply(code: str, extra: str = "", body: str = "") -> None:
            payload = body.encode()
            msg = f"{RTSP_VERSION} {code}\r\nCSeq: {cseq}\r\n"
            if extra:
                msg += extra
            if payload:
                msg += f"Content-Length: {len(payload)}\r\n"
            msg += "\r\n"
            try:
                conn.sendall(msg.encode() + payload)
            except OSError:
                pass

        camera_id = self._camera_from_url(url)

        if method == "OPTIONS":
            reply("200 OK", f"Public: {PUBLIC}\r\n")
            return session

        if method == "DESCRIBE":
            src = self.sources.get(camera_id)
            if src is None:
                reply("404 Not Found")
                return session
            self.stats["describes"] += 1
            sdp = self._sdp_for(src)
            reply("200 OK",
                  f"Content-Type: application/sdp\r\nContent-Base: {url}/\r\n",
                  sdp)
            return session

        if method == "SETUP":
            src = self.sources.get(camera_id)
            if src is None:
                reply("404 Not Found")
                return session
            transport = headers.get("transport", "")
            # The whole point: refuse UDP so a misconfigured client is a
            # loud failure rather than a stream that silently corrupts.
            if "tcp" not in transport.lower():
                self.stats["udp_refused"] += 1
                log.warning("refused non-TCP transport for %s: %s",
                            camera_id, transport)
                reply("461 Unsupported Transport")
                return session
            m = re.search(r"interleaved=(\d+)-(\d+)", transport)
            channel = int(m.group(1)) if m else 0
            session = _Session(uuid.uuid4().hex[:12], src)
            session.channel = channel
            self.stats["setups"] += 1
            reply("200 OK",
                  f"Session: {session.id}\r\n"
                  f"Transport: RTP/AVP/TCP;unicast;interleaved={channel}-{channel+1}\r\n")
            return session

        if method == "PLAY":
            if session is None:
                reply("454 Session Not Found")
                return session
            self.stats["plays"] += 1
            reply("200 OK", f"Session: {session.id}\r\nRange: npt=0.000-\r\n")
            self._start_stream(conn, session)
            return session

        if method in ("GET_PARAMETER", "SET_PARAMETER"):
            reply("200 OK", f"Session: {session.id}\r\n" if session else "")
            return session

        if method == "TEARDOWN":
            self.stats["teardowns"] += 1
            reply("200 OK")
            if session:
                self._end_session(session)
            return None

        reply("501 Not Implemented")
        return session

    def _camera_from_url(self, url: str) -> str:
        path = re.sub(r"^rtsp://[^/]+", "", url)
        path = path.split("?")[0].rstrip("/")
        if path.startswith(self.path_prefix + "/"):
            path = path[len(self.path_prefix) + 1:]
        # ffmpeg appends /streamid=0 to the aggregate control URL
        return path.split("/")[0]

    # ── SDP ───────────────────────────────────────────────────────────

    def _sdp_for(self, src: MediaSource) -> str:
        """Ask ffmpeg for the real SDP for this media.

        Generating it from the same encoder that will produce the RTP
        guarantees the payload type and sprop parameter sets match. Writing
        an SDP by hand is how you get a stream that negotiates and then
        decodes to nothing.
        """
        if src._sdp:
            return self._sdp_for_tcp(src._sdp)
        # A unique path per call: two clients describing the same camera at
        # once would otherwise race on one file, and one of them would read
        # it after the other had unlinked it.
        sdp_path = os.path.join(
            tempfile.gettempdir(),
            f"sentinel-sandbox-{src.camera_id}-{uuid.uuid4().hex[:8]}.sdp")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-i", src.path, "-c", "copy", "-f", "rtp",
               "-sdp_file", sdp_path, "-t", "0.1",
               "rtp://127.0.0.1:41000"]
        subprocess.run(cmd, capture_output=True, timeout=30)
        try:
            with open(sdp_path) as fh:
                src._sdp = fh.read()
        except OSError:
            src._sdp = ""
        finally:
            if os.path.exists(sdp_path):
                os.unlink(sdp_path)
        return self._sdp_for_tcp(src._sdp)

    @staticmethod
    def _sdp_for_tcp(sdp: str) -> str:
        """Zero the transport port and add control attributes.

        In interleaved mode the port carries no meaning; RFC 2326 wants a
        control URL so the client can address the media stream.
        """
        out, seen_media = [], False
        for line in sdp.splitlines():
            if line.startswith("m=video"):
                parts = line.split()
                parts[1] = "0"
                out.append(" ".join(parts))
                seen_media = True
                continue
            out.append(line)
        if seen_media:
            out.append("a=control:streamid=0")
        return "\r\n".join(out) + "\r\n"

    # ── streaming ─────────────────────────────────────────────────────

    def _start_stream(self, conn: socket.socket, session: _Session) -> None:
        src = session.source
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        udp.settimeout(1.0)
        rtp_port = udp.getsockname()[1]
        session.udp = udp

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if src.loop:
            cmd += ["-stream_loop", "-1"]
        # -re paces at real time: without it the whole clip is delivered in
        # milliseconds and every timing measurement downstream is fiction.
        cmd += ["-re", "-i", src.path, "-c", "copy", "-an",
                "-f", "rtp", f"rtp://127.0.0.1:{rtp_port}"]
        session.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE)
        session.playing = True
        session.started_at = time.time()
        threading.Thread(target=self._relay, args=(conn, session), daemon=True,
                         name=f"rtsp-relay-{session.id}").start()

    def _relay(self, conn: socket.socket, session: _Session) -> None:
        """Forward RTP as interleaved binary over the control connection.

        RFC 2326 §10.12 framing: '$' | channel | 2-byte big-endian length.
        """
        udp = session.udp
        drop_at = (session.started_at + session.source.drop_after_s
                   if session.source.drop_after_s else None)
        while self._running and session.playing:
            if drop_at and time.time() >= drop_at:
                # Deliberate mid-stream failure, so reconnect logic is
                # exercised against a real dropped socket.
                self.stats["forced_drops"] += 1
                log.info("dropping session %s to force a reconnect", session.id)
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                break
            try:
                data, _ = udp.recvfrom(65535)
            except socket.timeout:
                if session.proc and session.proc.poll() is not None:
                    break
                continue
            except OSError:
                break
            if not data:
                continue
            header = b"$" + bytes([session.channel]) + len(data).to_bytes(2, "big")
            try:
                conn.sendall(header + data)
            except OSError:
                break
            session.packets += 1
        self._end_session(session)

    def _end_session(self, session: _Session) -> None:
        session.playing = False
        if session.proc and session.proc.poll() is None:
            session.proc.kill()
            try:
                session.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if session.udp:
            try:
                session.udp.close()
            except OSError:
                pass
            session.udp = None


# ── media generation ─────────────────────────────────────────────────

def make_clip(path: str, codec: str = "h264", seconds: float = 6.0,
              width: int = 640, height: int = 360, fps: float = 15.0,
              pattern: str = "testsrc2") -> str:
    """Encode a real clip. Used to build the sandbox estate.

    Deliberately real video, not a still: a detector run against a frozen
    frame produces identical embeddings forever and would flatter every
    measurement taken through it.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to build sandbox media")
    encoder = {"h264": "libx264", "hevc": "libx265"}[codec]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i",
           f"{pattern}=size={width}x{height}:rate={fps}:duration={seconds}",
           "-c:v", encoder, "-pix_fmt", "yuv420p",
           "-g", str(int(fps)), "-bf", "0"]
    if codec == "hevc":
        cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=none"]
    cmd += [path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"encode failed: {(r.stderr or '')[:300]}")
    return path
