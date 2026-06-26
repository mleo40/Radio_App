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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .config import Config

_CONFIGURED = False

# Default number of records the in-process Logs surface retains. Bounded so a
# long-running, chatty session can't grow memory without limit.
_RING_CAPACITY = 2000


@dataclass(frozen=True)
class LogRecordView:
    """Immutable, UI-friendly snapshot of a single log record.

    Kept deliberately small (no exc traceback objects, no live record refs) so
    the ring buffer can hold thousands of entries cheaply and hand them to the
    TUI without risk of mutating logging state.
    """

    created: float
    level_no: int
    level_name: str
    name: str
    message: str


class RingBufferHandler(logging.Handler):
    """A logging handler that retains the most recent records in memory.

    The TUI takes over the terminal, so the only "live" view of what the app is
    doing has historically been ``tail -f`` on the log file. This handler keeps
    a bounded, thread-safe ring of recent records so a dedicated in-app **Logs**
    surface can render them live (level-filterable, follow/pause) without
    re-reading the file. ``max_level_no`` tracks the highest severity observed
    since the last :meth:`reset_peak`, powering the status-bar WARN/ERR badge.
    """

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        super().__init__()
        self._buf: deque[LogRecordView] = deque(maxlen=capacity)
        self._lock = Lock()
        self.max_level_no = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message = f"{message}\n{self.format(record)}"
        except Exception:  # noqa: BLE001 - never let logging crash the app
            message = str(getattr(record, "msg", ""))
        view = LogRecordView(
            created=record.created,
            level_no=record.levelno,
            level_name=record.levelname,
            name=record.name,
            message=message,
        )
        with self._lock:
            self._buf.append(view)
            if record.levelno > self.max_level_no:
                self.max_level_no = record.levelno

    def snapshot(self, min_level: int = 0) -> list[LogRecordView]:
        """Return a copy of retained records at or above ``min_level``."""
        with self._lock:
            records = list(self._buf)
        if min_level <= 0:
            return records
        return [r for r in records if r.level_no >= min_level]

    def peak_level(self) -> int:
        """Highest severity seen since the last :meth:`reset_peak`."""
        with self._lock:
            return self.max_level_no

    def reset_peak(self) -> None:
        """Clear the high-water severity mark (e.g. when the operator views Logs)."""
        with self._lock:
            self.max_level_no = 0

    def clear(self) -> None:
        """Drop all retained records and reset the peak severity."""
        with self._lock:
            self._buf.clear()
            self.max_level_no = 0


_RING_HANDLER: RingBufferHandler | None = None


def get_ring_handler() -> RingBufferHandler | None:
    """Return the process-wide in-memory log handler, if logging is configured."""
    return _RING_HANDLER


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

    # Always install the in-memory ring buffer so the TUI's Logs surface has a
    # live feed regardless of whether file logging is enabled. It captures
    # everything at the root level; the surface filters by level on read.
    global _RING_HANDLER
    ring = RingBufferHandler()
    ring.setLevel(logging.DEBUG)
    ring.setFormatter(fmt)
    root.addHandler(ring)
    _RING_HANDLER = ring

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

