"""Adapters for Government of India / Gujarat record systems.

    VAHAN     national vehicle registration      (MoRTH)
    SARTHI    national driving licence           (MoRTH)
    eGujCop   Gujarat Police CCTNS-linked records
    AFIS      state fingerprint identification
    NAFIS     national fingerprint identification (NCRB)

NONE OF THESE IS CONNECTED
──────────────────────────
No credential, endpoint or authorisation for any of these systems was
available to this project, and none was sought. Every adapter here ships
with a MOCK backend that fabricates plausible records from a fixed seed,
and every record it returns is stamped `provenance=MOCK`.

That stamp is not decoration. It travels with the record into the alert,
into the API response, into the UI and into the audit log, and it cannot
be removed by any code path short of configuring a real authorised
endpoint. An operator must never be able to look at a screen in this
system and be unsure whether "owner: Ramesh Patel, chassis MA3xxxx" is a
real citizen's data or something a demo made up. That confusion is how a
demonstration becomes evidence in a case file.

The real backends are deliberately unimplemented rather than stubbed with
guessed request shapes: writing a plausible-looking VAHAN client against
an API nobody here has read produces code that looks integrated and is
not, which is worse than an honest gap. `RealBackend` raises with the
specific authorisation each system requires.
"""

from .base import (
    AdapterError, AuthorizationRequired, GovtAdapter, GovtRecord,
    Provenance, RateLimited, UpstreamTimeout,
)
from .registry import AdapterRegistry, build_registry

__all__ = [
    "AdapterError", "AdapterRegistry", "AuthorizationRequired", "GovtAdapter",
    "GovtRecord", "Provenance", "RateLimited", "UpstreamTimeout",
    "build_registry",
]
