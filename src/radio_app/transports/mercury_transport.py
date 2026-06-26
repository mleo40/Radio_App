"""Mercury HF modem transport (high-throughput HF data).

Mercury (Rhizomatica) is an HF data modem driven over a control socket. This is
the most niche integration of the three; the adapter provides the lifecycle and
capabilities so it participates in selection, with the wire protocol marked TODO.
"""

from __future__ import annotations

import asyncio
import logging

from ..core.message import AddressType, UnifiedMessage
from .base import ReachabilityStatus, Transport, TransportCapabilities, probe_tcp

log = logging.getLogger(__name__)


class MercuryTransport(Transport):
    name = "mercury"
    surface = "chat"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            max_message_size=8_192,       # higher HF throughput than JS8
            supports_broadcast=True,
            supports_addressing=True,
            supports_groups=False,        # via labelled broadcast convention
            supports_encryption=False,    # amateur HF
            supports_delivery_confirmation=True,  # modem-level ARQ
            is_realtime=False,
            typical_latency_s=10.0,
            needs_internet=False,
            address_scheme="node_id",
            carries_operator_identity=True,  # amateur HF: identify with callsign
            prohibits_encryption=True,       # encryption prohibited on amateur HF
            uses_shared_radio=True,          # drives the one HF radio (sound+CAT+PTT)
        )

    async def start(self) -> None:
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", 7373))
        try:
            self._reader, self._writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            log.warning("Mercury modem not reachable at %s:%s (%s).", host, port, exc)
            self._running = False
            return
        self._running = True
        self._reader_task = asyncio.create_task(self._read_loop())
        log.info("Mercury transport connected to %s:%s", host, port)

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
        if self._writer:
            self._writer.close()

    async def check_reachable(self) -> ReachabilityStatus:
        """Reachable iff the Mercury control socket accepts a connection."""
        host = self.config.get("host", "127.0.0.1")
        port = int(self.config.get("port", 7373))
        return await probe_tcp(host, port)

    async def send(self, msg: UnifiedMessage) -> bool:
        if not self._running or self._writer is None:
            return False
        # TODO: frame msg.content per Mercury's control protocol and write it.
        prefix = f"@{msg.group} " if msg.address_type is AddressType.GROUP else ""
        log.info("[mercury] would send: %s%s", prefix, msg.content)
        return True

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while self._running:
            try:
                data = await self._reader.readline()
            except (asyncio.CancelledError, OSError):
                break
            if not data:
                break
            # TODO: decode Mercury frames into a UnifiedMessage and emit:
            #   await self._emit(UnifiedMessage(...))

