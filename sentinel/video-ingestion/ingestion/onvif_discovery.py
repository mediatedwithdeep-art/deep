"""
ONVIF WS-Discovery + profile enumeration.

This auto-onboards roughly half a typical estate with no manual data entry.
Run it from the edge gateway, because WS-Discovery is multicast and does not
cross a router -- a central discovery service would find nothing.

Deliberately dependency-free (raw UDP + regex over the SOAP envelope) so it
runs on a minimal edge image without a SOAP stack. For full ONVIF device
management (PTZ, events, imaging) use `onvif-zeep`; for discovery and stream
URLs this is enough and far more robust to the malformed SOAP that cheap
OEM firmware emits.
"""

from __future__ import annotations

import re
import socket
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.request import Request, urlopen
from urllib.error import URLError

WS_DISCOVERY_ADDR = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{mid}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
  </e:Body>
</e:Envelope>"""


@dataclass
class Profile:
    token: str
    name: str = ""
    encoding: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    stream_uri: str = ""

    @property
    def is_substream(self) -> bool:
        """Heuristic. Cameras do not label sub-streams consistently, so infer
        from resolution -- anything at or below D1/720p is the sub-stream."""
        return self.width > 0 and self.width <= 1280


@dataclass
class DiscoveredDevice:
    ip: str
    xaddr: str
    types: str = ""
    scopes: list[str] = field(default_factory=list)
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    profiles: list[Profile] = field(default_factory=list)

    @property
    def name_hint(self) -> str:
        for s in self.scopes:
            if "/name/" in s:
                return s.rsplit("/", 1)[-1].replace("%20", " ")
        return ""

    @property
    def location_hint(self) -> str:
        for s in self.scopes:
            if "/location/" in s:
                return s.rsplit("/", 1)[-1].replace("%20", " ")
        return ""

    def best_substream(self) -> Profile | None:
        subs = [p for p in self.profiles if p.is_substream and p.stream_uri]
        if subs:
            return max(subs, key=lambda p: p.width)
        return next((p for p in self.profiles if p.stream_uri), None)

    def best_mainstream(self) -> Profile | None:
        withuri = [p for p in self.profiles if p.stream_uri]
        return max(withuri, key=lambda p: p.width) if withuri else None


def discover(timeout_s: int = 8, iface_ip: str = "0.0.0.0") -> list[DiscoveredDevice]:
    """Multicast probe; collect every responder within the timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 4))
    if iface_ip != "0.0.0.0":
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(iface_ip))
    sock.bind((iface_ip, 0))
    sock.settimeout(1.0)

    msg = _PROBE.format(mid=uuid.uuid4()).encode()
    # Send three times: WS-Discovery is UDP multicast and cheap OEM firmware
    # drops probes under load. The spec itself recommends retransmission.
    for _ in range(3):
        try:
            sock.sendto(msg, (WS_DISCOVERY_ADDR, WS_DISCOVERY_PORT))
        except OSError:
            pass

    found: dict[str, DiscoveredDevice] = {}
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        dev = _parse_probe_match(data.decode("utf-8", "replace"), addr[0])
        if dev and dev.xaddr not in found:
            found[dev.xaddr] = dev
    sock.close()
    return list(found.values())


def _parse_probe_match(xml: str, src_ip: str) -> DiscoveredDevice | None:
    # Regex rather than a strict XML parse: OEM firmware emits envelopes that
    # a conforming parser rejects outright, and we only need two fields.
    m = re.search(r"<[^>]*XAddrs[^>]*>(.*?)</[^>]*XAddrs>", xml, re.S | re.I)
    if not m:
        return None
    xaddr = m.group(1).strip().split()[0]
    scopes: list[str] = []
    sm = re.search(r"<[^>]*Scopes[^>]*>(.*?)</[^>]*Scopes>", xml, re.S | re.I)
    if sm:
        scopes = sm.group(1).split()
    tm = re.search(r"<[^>]*Types[^>]*>(.*?)</[^>]*Types>", xml, re.S | re.I)
    ip = re.sub(r"^https?://", "", xaddr).split("/")[0].split(":")[0] or src_ip
    return DiscoveredDevice(ip=ip, xaddr=xaddr, types=(tm.group(1).strip() if tm else ""),
                            scopes=scopes)


# ─────────────────────────────────────────────────────────────────────────
# Media service: GetProfiles / GetStreamUri
# ─────────────────────────────────────────────────────────────────────────

_NS = {
    "s":    "http://www.w3.org/2003/05/soap-envelope",
    "trt":  "http://www.onvif.org/ver10/media/wsdl",
    "tt":   "http://www.onvif.org/ver10/schema",
    "tds":  "http://www.onvif.org/ver10/device/wsdl",
}

_SOAP = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
 <s:Body xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
         xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
         xmlns:tt="http://www.onvif.org/ver10/schema">{body}</s:Body>
</s:Envelope>"""


def _soap_call(xaddr: str, body: str, user: str | None, pwd: str | None,
               timeout: int = 6) -> ET.Element | None:
    """POST a SOAP body.

    Note: WS-UsernameToken digest auth is omitted here for brevity -- most
    cameras accept HTTP Digest on the media service, and the ones that do not
    should be handled with `onvif-zeep`. Do not ship this without adding
    WS-Security if your estate requires it.
    """
    req = Request(xaddr, data=_SOAP.format(body=body).encode(),
                  headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    if user:
        import base64
        tok = base64.b64encode(f"{user}:{pwd or ''}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    try:
        with urlopen(req, timeout=timeout) as r:
            return ET.fromstring(r.read())
    except (URLError, ET.ParseError, OSError):
        return None


def enrich(dev: DiscoveredDevice, user: str | None = None,
           pwd: str | None = None) -> DiscoveredDevice:
    """Fill in device info, profiles and stream URIs for a discovered device."""
    info = _soap_call(dev.xaddr, "<tds:GetDeviceInformation/>", user, pwd)
    if info is not None:
        for tag, attr in (("Manufacturer", "manufacturer"), ("Model", "model"),
                          ("FirmwareVersion", "firmware")):
            el = info.find(f".//tds:{tag}", _NS)
            if el is not None and el.text:
                setattr(dev, attr, el.text.strip())

    prof_resp = _soap_call(dev.xaddr, "<trt:GetProfiles/>", user, pwd)
    if prof_resp is None:
        return dev

    for p in prof_resp.findall(".//trt:Profiles", _NS):
        token = p.get("token") or ""
        if not token:
            continue
        prof = Profile(token=token)
        nm = p.find("tt:Name", _NS)
        if nm is not None and nm.text:
            prof.name = nm.text
        enc = p.find(".//tt:VideoEncoderConfiguration", _NS)
        if enc is not None:
            e = enc.find("tt:Encoding", _NS)
            prof.encoding = e.text if e is not None and e.text else ""
            w = enc.find(".//tt:Width", _NS)
            h = enc.find(".//tt:Height", _NS)
            f = enc.find(".//tt:FrameRateLimit", _NS)
            prof.width = int(w.text) if w is not None and w.text else 0
            prof.height = int(h.text) if h is not None and h.text else 0
            prof.fps = float(f.text) if f is not None and f.text else 0.0

        uri_resp = _soap_call(dev.xaddr, (
            "<trt:GetStreamUri>"
            "<trt:StreamSetup>"
            "<tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
            "</trt:StreamSetup>"
            f"<trt:ProfileToken>{token}</trt:ProfileToken>"
            "</trt:GetStreamUri>"), user, pwd)
        if uri_resp is not None:
            u = uri_resp.find(".//tt:Uri", _NS)
            if u is not None and u.text:
                prof.stream_uri = u.text.strip()
        dev.profiles.append(prof)
    return dev


def discover_and_enrich(timeout_s: int = 8, user: str | None = None,
                        pwd: str | None = None) -> list[DiscoveredDevice]:
    return [enrich(d, user, pwd) for d in discover(timeout_s)]


if __name__ == "__main__":
    import json, sys
    devices = discover_and_enrich(
        timeout_s=int(sys.argv[1]) if len(sys.argv) > 1 else 8,
        user=sys.argv[2] if len(sys.argv) > 2 else None,
        pwd=sys.argv[3] if len(sys.argv) > 3 else None)
    print(json.dumps([{
        "ip": d.ip, "manufacturer": d.manufacturer, "model": d.model,
        "firmware": d.firmware, "name_hint": d.name_hint,
        "location_hint": d.location_hint,
        "sub": (d.best_substream().stream_uri if d.best_substream() else None),
        "main": (d.best_mainstream().stream_uri if d.best_mainstream() else None),
        "profiles": [{"token": p.token, "res": f"{p.width}x{p.height}",
                      "fps": p.fps, "sub": p.is_substream} for p in d.profiles],
    } for d in devices], indent=2))
    print(f"\n{len(devices)} ONVIF device(s) found", file=sys.stderr)
