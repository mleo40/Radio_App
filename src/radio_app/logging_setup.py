"""Centralised logging bootstrap.

The TUI takes over the terminal, so stderr-only logs are invisible while it
runs. This module installs a file handler (default: next to the user config)
plus a stderr handler for plain CLI commands, so behaviour like "is the
Reticulum announce handler actually firing?" can be observed via ``tail -f``.

Configuration lives under ``[logging]`` in ``config.toml``:

    [logging]
    file = "radio_app.log"     # relative -> alongside the config; "" disables
    level = "INFO"             # DEBUG | INFO | WARNING | ERROR
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .config import Config

_CONFIGURED = False


def _strip_console_handlers(root: logging.Logger) -> None:
    """Remove console (stream) handlers imported libraries attached to root.

    Notably the third-party ``meshcore`` package calls ``logging.basicConfig()``
    at import time, which installs a ``StreamHandler`` to stderr on the root
    logger. Left in place it prints our log records straight to the terminal -
    which corrupts the Textual TUI display (the "INFO: radio_app.transports..."
    lines leaking over the UI). We own console logging here, so drop any such
    handler (but never the file handlers, which subclass StreamHandler).
    """
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            root.removeHandler(handler)


def configure_logging(config: Config, *, stderr: bool = True) -> Path | None:
    """Install handlers once per process. Returns the log file path, if any."""
    global _CONFIGURED
    if _CONFIGURED:
        return _current_file_path()

    section = config.data.get("logging", {}) if hasattr(config, "data") else {}
    level_name = str(section.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    file_setting = section.get("file", "radio_app.log")

    root = logging.getLogger()
    # Drop console handlers other imports may have installed (see helper); we
    # add our own below only when explicitly asked (stderr=True for the CLI).
    _strip_console_handlers(root)
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path: Path | None = None
    if file_setting:
        candidate = Path(str(file_setting)).expanduser()
        if not candidate.is_absolute():
            candidate = config.path.parent / candidate
        candidate.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(candidate)
        fh.setLevel(level)
        fh.setFormatter(fmt)
        root.addHandler(fh)
        log_path = candidate
        # Stash for _current_file_path()
        os.environ["RADIO_APP_LOG_FILE"] = str(candidate)

    if stderr:
        sh = logging.StreamHandler()
        sh.setLevel(level)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # With no handlers at all, Python's "last resort" handler prints WARNING+
    # records to stderr - which would still leak over the TUI when file logging
    # is disabled. A NullHandler keeps the root logger quiet on the console.
    if not root.handlers:
        root.addHandler(logging.NullHandler())

    _CONFIGURED = True
    if log_path is not None:
        logging.getLogger(__name__).info(
            "logging to %s (level=%s)", log_path, level_name
        )
    return log_path


def _current_file_path() -> Path | None:
    raw = os.environ.get("RADIO_APP_LOG_FILE")
    return Path(raw) if raw else None

