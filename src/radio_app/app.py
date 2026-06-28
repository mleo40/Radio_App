"""Application assembly: wire config -> store, groups, filters, transports, router.

This is the single place that builds a fully-connected app from a :class:`Config`.
Every frontend (CLI, future TUI/GUI) constructs an :class:`App` and drives it,
which keeps the user interface a thin layer over a transport-agnostic core.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Config
from .core.compliance import ComplianceGuard
from .core.favorites import Favorites
from .core.filters import FilterEngine
from .core.groups import GroupRegistry
from .core.nomad_cache import NomadPageCache
from .core.nomadnet import NomadnetBrowser
from .core.radio_interlock import RadioInterlock
from .core.router import Router
from .core.timesource import WSJTXDTMonitor
from .core.selector import SelectionMode
from .core.station import Station
from .core.store import MessageStore
from .transports import load_transports

log = logging.getLogger(__name__)


class App:
    """A fully-assembled Radio_App instance."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = MessageStore(config.database_path())
        self.groups = GroupRegistry.from_config(config)
        self.filters = FilterEngine.from_config(config, self.groups)
        self.station = Station.from_config(config)
        self.compliance = ComplianceGuard(
            allow_encrypted_on_hf=config.allow_encrypted_on_hf()
        )
        self.favorites = Favorites.from_config(config)
        self.transports = load_transports(config.enabled_transports())
        # Radio interlock: the transports that drive the one physical HF radio
        # (JS8Call, Pat/Winlink over an RF modem, Mercury) must not key up over
        # each other. This single-owner token gates our transmit actions; the UI
        # claims/releases it as the operator switches modes / runs sessions.
        self.radio_interlock = RadioInterlock(self._radio_contenders())
        # HF transports route inbound traffic by our callsign (a message "TO" us
        # is DIRECT). Push the operator identity from [station] into any transport
        # that accepts it, so users don't have to duplicate it per transport block.
        self._apply_station_identity()
        # Tell every transport where downloaded content (attachments/files) goes,
        # so all modes save into the one central directory the user picked at
        # setup ([storage].download_dir). Done in-memory (not persisted per
        # transport) so changing the central path updates every mode at once.
        self._apply_download_dir()
        # Read-only NomadNet page browser over the Reticulum transport (if any),
        # with an offline page cache backed by the same database file.
        ret = next((t for t in self.transports if t.name == "reticulum"), None)
        self.nomad_cache = NomadPageCache(config.database_path())
        self.browser = NomadnetBrowser(ret, cache=self.nomad_cache)
        self.router = Router(
            transports=self.transports,
            store=self.store,
            groups=self.groups,
            filters=self.filters,
            default_mode=self._default_mode(),
            station=self.station,
            compliance=self.compliance,
        )
        self.wsjtx_monitor = WSJTXDTMonitor()
        self._started = False

    @classmethod
    def from_config_path(cls, path: str | None = None) -> App:
        return cls(Config.load(path))

    def _radio_contenders(self) -> list[str]:
        """Names of transports that drive the one physical HF radio.

        These are gated by the radio interlock so they don't transmit over each
        other. Winlink reports this dynamically: it only contends when an RF
        modem path is configured (telnet-only never touches the radio).
        """
        names: list[str] = []
        for t in self.transports:
            try:
                if t.capabilities().uses_shared_radio:
                    names.append(t.name)
            except Exception:  # noqa: BLE001 - a bad capability never blocks startup
                continue
        return names

    def _default_mode(self) -> SelectionMode:
        try:
            return SelectionMode(self.config.default_mode)
        except ValueError:
            return SelectionMode.AUTO

    def _apply_station_identity(self) -> None:
        """Push the operator callsign + subscribed groups into HF transports.

        Transports that recognise an operator identity expose ``set_identity``
        (e.g. JS8Call, which needs our callsign to tag inbound traffic addressed
        to us as DIRECT). Anonymous transports simply don't define it.
        """
        callsign = self.station.callsign
        groups = tuple(g.name for g in self.groups.all())
        for transport in self.transports:
            setter = getattr(transport, "set_identity", None)
            if callable(setter):
                setter(callsign, groups)

    def apply_download_dir(self) -> None:
        """Push the central download directory into every transport.

        Transports that save downloaded content (e.g. Reticulum attachments)
        expose ``set_download_dir``; others simply don't define it. Re-callable
        at runtime so changing ``[storage].download_dir`` (e.g. via setup) takes
        effect across all modes immediately.
        """
        path = str(self.config.download_dir())
        for transport in self.transports:
            setter = getattr(transport, "set_download_dir", None)
            if callable(setter):
                setter(path)

    # Internal alias used during construction.
    _apply_download_dir = apply_download_dir

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        # Start every transport concurrently so a slow/unreachable one (e.g. a
        # JS8Call host that's off the network, where the TCP connect sits in a
        # multi-second SYN timeout) doesn't serialise startup behind itself.
        async def _start_one(transport) -> None:
            try:
                await transport.start()
            except Exception:  # noqa: BLE001
                log.exception("failed to start transport %s", transport.name)

        if self.transports:
            await asyncio.gather(*(_start_one(t) for t in self.transports))
        await self.wsjtx_monitor.start()
        # Apply retention policy on startup.
        days = int(self.config.general.get("history_retention_days", 0) or 0)
        if days > 0:
            removed = self.store.purge_older_than(days)
            if removed:
                log.info("purged %d messages older than %d days", removed, days)
        self._started = True

    async def stop(self) -> None:
        # Persist favorite "last seen" timestamps so restarts don't re-alert.
        try:
            if self.favorites.dirty:
                self.favorites.save(self.config)
        except Exception:  # noqa: BLE001 - never let persistence break shutdown
            log.exception("failed to persist favorites")
        for transport in self.transports:
            try:
                await transport.stop()
            except Exception:  # noqa: BLE001
                log.exception("failed to stop transport %s", transport.name)
        await self.wsjtx_monitor.stop()
        self.store.close()
        self.nomad_cache.close()
        self._started = False

    @property
    def running_transports(self) -> list[str]:
        return [t.name for t in self.transports if t.running]

