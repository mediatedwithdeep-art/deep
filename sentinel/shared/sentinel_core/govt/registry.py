"""Builds the adapter set for a deployment, from configuration.

Which backend each system uses is a DEPLOYMENT decision, and it is made
here in one place so that "is this system connected for real?" has exactly
one answer that can be printed, logged and shown in the UI.

The default is mock for every system, and switching one to real requires
naming it explicitly. That direction matters: a default of "real" would
mean a misconfiguration silently produced `AUTHORITATIVE`-stamped records
from a backend nobody had verified.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .adapters import ADAPTERS, REAL_REQUIREMENTS, mock_backend, real_backend
from .base import AuditSink, GovtAdapter, Provenance

#: Per-system quota, calls/minute, on OUR side of the call. Deliberately
#: conservative: exceeding a published government quota costs the
#: integration agreement, not just the request.
DEFAULT_QUOTA = {
    "VAHAN": 120, "SARTHI": 30, "EGUJCOP": 120, "AFIS": 10, "NAFIS": 10,
}

#: Biometric systems get a longer budget because a fingerprint search is
#: genuinely slow; a plate lookup that takes three seconds is broken.
DEFAULT_TIMEOUT_S = {
    "VAHAN": 3.0, "SARTHI": 3.0, "EGUJCOP": 3.0, "AFIS": 15.0, "NAFIS": 15.0,
}


@dataclass
class AdapterRegistry:
    adapters: dict[str, GovtAdapter]

    def __getitem__(self, system: str) -> GovtAdapter:
        return self.adapters[system.upper()]

    def get(self, system: str) -> GovtAdapter | None:
        return self.adapters.get(system.upper())

    @property
    def any_real(self) -> bool:
        return any(a.provenance.is_real for a in self.adapters.values())

    def status(self) -> list[dict]:
        """What every interface should be able to show about integrations.

        Includes the requirement for the systems that are not connected, so
        the answer to "why is this mock?" travels with the status rather
        than living only in a document.
        """
        out = []
        for name, a in sorted(self.adapters.items()):
            out.append({
                "system": name,
                "provenance": a.provenance.value,
                "is_real_data": a.provenance.is_real,
                "banner": a.provenance.banner,
                "timeout_s": a.timeout_s,
                "quota_per_minute": a._bucket.per_minute,
                "purposes": sorted(p.value for p in a.RELEASE),
                "requirement_for_real_access":
                    None if a.provenance.is_real else REAL_REQUIREMENTS.get(name),
                "stats": {
                    "calls": a.stats.calls, "hits": a.stats.hits,
                    "misses": a.stats.misses, "timeouts": a.stats.timeouts,
                    "rate_limited": a.stats.rate_limited,
                    "unauthorized": a.stats.unauthorized,
                    "mean_ms": round(a.stats.mean_ms, 2),
                },
            })
        return out


def build_registry(*, real_systems: set[str] | None = None,
                   audit: AuditSink | None = None) -> AdapterRegistry:
    """Build every adapter.

    `real_systems` names the systems this deployment has authorised access
    to. Anything not named gets the mock backend and stamps every record
    it returns `MOCK`. Read from `SENTINEL_GOVT_REAL` (comma-separated)
    when not passed.
    """
    if real_systems is None:
        raw = os.environ.get("SENTINEL_GOVT_REAL", "")
        real_systems = {s.strip().upper() for s in raw.split(",") if s.strip()}

    unknown = real_systems - set(ADAPTERS)
    if unknown:
        raise ValueError(f"unknown government system(s): {sorted(unknown)}")

    adapters: dict[str, GovtAdapter] = {}
    for name, cls in ADAPTERS.items():
        backend = real_backend(name) if name in real_systems else mock_backend(name)
        adapters[name] = cls(
            backend,
            timeout_s=DEFAULT_TIMEOUT_S[name],
            per_minute=DEFAULT_QUOTA[name],
            audit=audit)
    return AdapterRegistry(adapters)
