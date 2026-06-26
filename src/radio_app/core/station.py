"""Operator/station identity.

Holds the amateur-radio operator's callsign and Maidenhead grid square. This
identity is REQUIRED on HF transports (you must identify on the air) and is
DELIBERATELY excluded from the Reticulum transport, which uses an anonymous
cryptographic identity instead (see :mod:`radio_app.core.privacy`).

The radio itself is driven by the transport application (e.g. JS8Call), so
Radio_App holds no rig/CAT configuration of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Loose amateur callsign pattern (1-2 char prefix, digit, 1-4 char suffix,
# optional /portable indicators). Intentionally permissive across countries.
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,4}(/[A-Z0-9]+)?$")
# Maidenhead grid: 2 letters, 2 digits, optional 2 letters (e.g. FN31, FN31pr).
_GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}([a-x]{2})?$")


@dataclass
class Station:
    """The local operator's identity."""

    callsign: str = ""
    grid_square: str = ""

    def __post_init__(self) -> None:
        self.callsign = self.callsign.strip().upper()
        self.grid_square = self.grid_square.strip()

    @classmethod
    def from_config(cls, config: object) -> Station:
        from ..config import Config  # local import to avoid a cycle

        assert isinstance(config, Config)
        s = config.station
        return cls(
            callsign=s.get("callsign", ""),
            grid_square=s.get("grid_square", ""),
        )

    # -- validation -----------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(self.callsign)

    def callsign_is_valid(self) -> bool:
        return bool(_CALLSIGN_RE.match(self.callsign))

    def grid_is_valid(self) -> bool:
        # An empty grid is allowed (optional); a present one must be well-formed.
        if not self.grid_square:
            return True
        return bool(_GRID_RE.match(self.grid_square))

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == valid)."""
        problems: list[str] = []
        if not self.callsign:
            problems.append("callsign is required")
        elif not self.callsign_is_valid():
            problems.append(f"callsign '{self.callsign}' does not look valid")
        if not self.grid_is_valid():
            problems.append(
                f"grid square '{self.grid_square}' is not a valid Maidenhead locator"
            )
        return problems

