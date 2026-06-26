"""Application assembly: wire config -> store, groups, filters, transports, router.

This is the single place that builds a fully-connected app from a :class:`Config`.
Every frontend (CLI, future TUI/GUI) constructs an :class:`App` and drives it,
which keeps the user interface a thin layer over a transport-agnostic core.
"""

from __future__ import annotations

import logging

from .config import Config
from .core.compliance import ComplianceGuard
from .core.favorites import Favorites
from .core.filters import FilterEngine
from .core.groups import GroupRegistry
from .core.nomadnet import NomadnetBrowser
from .core.router import Router
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
        # HF transports route inbound traffic by our callsign (a message "TO" us
        # is DIRECT). Push the operator identity from [station] into any transport
        # that accepts it, so users don't have to duplicate it per transport block.
        self._apply_station_identity()
        # Read-only NomadNet page browser over the Reticulum transport (if any).
        ret = next((t for t in self.transports if t.name == "reticulum"), None)
        self.browser = NomadnetBrowser(ret)
        self.router = Router(
            transports=self.transports,
            store=self.store,
            groups=self.groups,
            filters=self.filters,
            default_mode=self._default_mode(),
            station=self.station,
            compliance=self.compliance,
        )
        self._started = False

    @classmethod
    def from_config_path(cls, path: str | None = None) -> App:
        return cls(Config.load(path))

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

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        for transport in self.transports:
            try:
                await transport.start()
            except Exception:  # noqa: BLE001
                log.exception("failed to start transport %s", transport.name)
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
        self.store.close()
        self._started = False

    @property
    def running_transports(self) -> list[str]:
        return [t.name for t in self.transports if t.running]

