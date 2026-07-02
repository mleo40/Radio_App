"""On-demand HF band scan for JS8Call.

Cycles through a list of bands, transmitting a JS8Call heartbeat on each
(``JS8CallTransport.send_heartbeat()``) and tallying heartbeat-ACK replies
heard during a dwell window per band, so an operator can pick whichever band
they're actually being heard on right now instead of guessing. Transport/UI
agnostic — the TUI and CLI both drive this and render progress their own way
via the ``on_progress`` callback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from .message import UnifiedMessage

ProgressCallback = Callable[[str], None]


@dataclass
class BandResult:
    """Heartbeat replies heard on one band during its dwell window."""

    band: str
    heard_count: int = 0
    snrs: list[int] = field(default_factory=list)

    @property
    def avg_snr(self) -> float | None:
        return sum(self.snrs) / len(self.snrs) if self.snrs else None


@dataclass
class BandScanReport:
    """Result of one full scan run."""

    results: list[BandResult]
    original_band: str | None

    def best(self) -> BandResult | None:
        """Best band by heard-count, average SNR as a tiebreak. None if silent."""
        heard = [r for r in self.results if r.heard_count > 0]
        if not heard:
            return None

        def _key(r: BandResult) -> tuple[int, float]:
            return (r.heard_count, r.avg_snr if r.avg_snr is not None else -999.0)

        return max(heard, key=_key)


def _is_heartbeat_ack(msg: UnifiedMessage) -> bool:
    """True if an inbound JS8Call message looks like a heartbeat SNR reply."""
    if msg.transport != "js8call":
        return False
    if msg.metadata.get("js8_command") != "SNR":
        return False
    if msg.metadata.get("snr_report") is None:
        return False
    return "HEARTBEAT" in (msg.content or "").upper()


async def run_band_scan(
    transport: object,
    router: object,
    interlock: object,
    bands: list[str],
    dwell_s: float,
    grid: str = "",
    on_progress: ProgressCallback | None = None,
) -> BandScanReport | None:
    """Cycle ``bands``, heartbeat + listen ``dwell_s`` seconds on each.

    Returns ``None`` (after logging why) if the radio interlock is held by
    another transport. ``report.original_band`` is whatever band the operator
    was on before the scan started, for the caller to restore if declined.
    """
    from ..transports.js8call_transport import dial_for_band

    def _log(line: str) -> None:
        if on_progress is not None:
            on_progress(line)

    dec = interlock.acquire("js8call")
    if not dec.granted:
        _log(f"Band scan: radio busy ({dec.blocked_by}).")
        return None

    original_band = transport.current_band()
    results: list[BandResult] = []
    try:
        for band in bands:
            hz = dial_for_band(band)
            if hz is None:
                _log(f"Band scan: unknown band '{band}', skipping.")
                continue
            result = BandResult(band=band)
            results.append(result)

            def _on_msg(msg: UnifiedMessage, _action: object, _result=result) -> None:
                if _is_heartbeat_ack(msg):
                    _result.heard_count += 1
                    snr = msg.metadata.get("snr_report")
                    if isinstance(snr, int):
                        _result.snrs.append(snr)

            router.add_ui_callback(_on_msg)
            try:
                await transport.set_dial_freq(hz)
                _log(
                    f"Band scan: {band} — sending heartbeat, "
                    f"listening {int(dwell_s)}s…"
                )
                await transport.send_heartbeat(grid)
                await asyncio.sleep(dwell_s)
            finally:
                router.remove_ui_callback(_on_msg)
            snr_note = ""
            if result.avg_snr is not None:
                snr_note = f", avg SNR {result.avg_snr:+.0f} dB"
            _log(f"Band scan: {band} — {result.heard_count} heard{snr_note}")
    finally:
        interlock.release("js8call")

    return BandScanReport(results=results, original_band=original_band)
