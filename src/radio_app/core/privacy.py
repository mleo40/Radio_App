"""Outbound privacy policy.

Applied per-transport, immediately before a message is handed to a transport, so
the same logical message can be sent with different identities on different media:

* Identity-carrying transports (amateur HF: JS8Call, Mercury) attach the operator
  callsign as the sender, because identifying on the air is required.
* Anonymous transports (Reticulum) replace the sender with a non-identifying
  address and STRIP any operator PII (callsign, grid square, name) from metadata.

This is capability-driven (``carries_operator_identity``), so any future transport
inherits the correct behaviour automatically.
"""

from __future__ import annotations

import copy

from ..transports.base import Transport
from .message import UnifiedMessage
from .station import Station

# Metadata keys considered personally identifying; removed on anonymous media.
_PII_KEYS = ("callsign", "grid", "grid_square", "name", "operator", "qth")


def apply_outbound_privacy(
    msg: UnifiedMessage, transport: Transport, station: Station
) -> UnifiedMessage:
    """Return a per-transport copy of ``msg`` with the correct identity applied.

    The original message is never mutated, so group fan-out across transports with
    different privacy rules stays correct.
    """
    out = copy.deepcopy(msg)
    caps = transport.capabilities()

    if caps.carries_operator_identity:
        # Amateur HF: identify with the callsign (legally required on the air).
        if station.callsign:
            out.sender = station.callsign
        # Grid may be shared on HF; keep it only if explicitly present already.
    else:
        # Anonymous transport (e.g. Reticulum): never leak operator identity.
        out.sender = transport.local_identity() or "anonymous"
        for key in _PII_KEYS:
            out.metadata.pop(key, None)

    return out

