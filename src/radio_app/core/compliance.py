"""Regulatory compliance guard.

Amateur-radio regulations (e.g. FCC Part 97.113(a)(4) in the US, and similar rules
in most countries) generally PROHIBIT transmitting messages encrypted or encoded to
obscure their meaning on the air. This guard makes the app *extremely explicit*
before any encrypted payload could be sent over an HF transport: such a send is
refused outright unless the operator has both enabled it in config AND confirms an
unmistakable warning at send time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..transports.base import Transport
from .message import UnifiedMessage

# A confirmation hook supplied by the UI: given the warning text, return True only
# if the operator explicitly accepts responsibility. Default: never confirm.
ConfirmCallback = Callable[[str], bool]


WARNING_TEXT = (
    "!!! REGULATORY WARNING !!!\n"
    "You are about to transmit an ENCRYPTED / obscured message over an HF\n"
    "amateur-radio transport ({transport}).\n\n"
    "On amateur bands this is PROHIBITED in most jurisdictions (messages must\n"
    "not be encoded to obscure their meaning). Sending this may be ILLEGAL and\n"
    "is solely YOUR responsibility as the licensed operator.\n\n"
    "Type the confirmation exactly to proceed; otherwise the message will NOT\n"
    "be sent over this transport."
)


@dataclass
class ComplianceDecision:
    allowed: bool
    reason: str = ""


class ComplianceGuard:
    """Decides whether an outbound message may go over a given transport."""

    def __init__(
        self,
        allow_encrypted_on_hf: bool = False,
        confirm: ConfirmCallback | None = None,
    ) -> None:
        self._allow_encrypted_on_hf = allow_encrypted_on_hf
        self._confirm = confirm

    def set_confirm(self, confirm: ConfirmCallback | None) -> None:
        self._confirm = confirm

    def check_outbound(
        self, msg: UnifiedMessage, transport: Transport
    ) -> ComplianceDecision:
        caps = transport.capabilities()
        wants_encryption = bool(getattr(msg, "encrypt", False))

        if not wants_encryption or not caps.prohibits_encryption:
            return ComplianceDecision(allowed=True)

        # Encrypted payload + a medium that prohibits encryption (amateur HF).
        if not self._allow_encrypted_on_hf:
            return ComplianceDecision(
                allowed=False,
                reason=(
                    f"refusing to send encrypted payload over {transport.name}: "
                    "encryption on amateur HF is prohibited "
                    "(set compliance.allow_encrypted_on_hf and confirm to override)"
                ),
            )

        # Explicitly enabled in config: still require an at-send confirmation.
        warning = WARNING_TEXT.format(transport=transport.name)
        if self._confirm is not None and self._confirm(warning):
            return ComplianceDecision(allowed=True, reason="operator confirmed")
        return ComplianceDecision(
            allowed=False,
            reason=f"operator did not confirm encrypted HF send on {transport.name}",
        )

