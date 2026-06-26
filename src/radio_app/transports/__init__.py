"""Transport plugins.

Importing this package registers the built-in transports (via ``__init_subclass__``
in :mod:`radio_app.transports.base`). :func:`load_transports` then instantiates the
ones enabled in the config. Adding a new platform is just a new module here (or a
third-party package) that subclasses ``Transport`` and sets ``name``.
"""

from __future__ import annotations

import logging

# Import built-ins so they self-register.
# NOTE: mercury is temporarily disabled (not registered) per design decision -
# re-enable by restoring the import below. Its config block is also ignored
# while commented out.
from . import (
    js8call_transport,  # noqa: F401,E402
    meshcore_transport,  # noqa: F401,E402
    # mercury_transport,  # noqa: F401,E402  # TODO: re-enable mercury mode later
    reticulum_transport,  # noqa: F401,E402
    winlink_transport,  # noqa: F401,E402
)
from .base import TRANSPORT_REGISTRY, Transport, TransportCapabilities

log = logging.getLogger(__name__)

__all__ = [
    "Transport",
    "TransportCapabilities",
    "TRANSPORT_REGISTRY",
    "load_transports",
    "discover_plugin_transports",
]


def discover_plugin_transports() -> None:
    """Load third-party transports advertised via the ``radio_app.transports``
    entry-point group, so platforms can be shipped as separate pip packages with
    no changes to this codebase.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return
    try:
        eps = entry_points(group="radio_app.transports")
    except TypeError:  # Python <3.10 API shape
        eps = entry_points().get("radio_app.transports", [])  # type: ignore
    for ep in eps:
        try:
            ep.load()  # importing registers the subclass
        except Exception:  # noqa: BLE001
            log.exception("failed to load transport plugin %s", ep.name)


def load_transports(enabled: dict[str, dict]) -> list[Transport]:
    """Instantiate the transports named in ``enabled`` (name -> config block)."""
    discover_plugin_transports()
    transports: list[Transport] = []
    for name, cfg in enabled.items():
        cls = TRANSPORT_REGISTRY.get(name)
        if cls is None:
            log.warning("Unknown transport '%s' in config; skipping.", name)
            continue
        transports.append(cls(cfg))
    return transports

