"""Logging bootstrap tests.

The TUI takes over the terminal, so stray console log handlers (e.g. the one the
third-party ``meshcore`` package installs via ``logging.basicConfig`` at import)
must be removed - otherwise log records print over the Textual UI. These tests
exercise that cleanup without launching the TUI.
"""

from __future__ import annotations

import logging

import radio_app.logging_setup as logging_setup
from radio_app.config import Config


def _fresh_config(tmp_path, file="radio_app.log"):
    p = tmp_path / "config.toml"
    p.write_text(f'[logging]\nfile = "{file}"\nlevel = "INFO"\n')
    return Config.load(str(p))


def _reset_logging():
    """Undo any handler/state changes so tests don't bleed into each other."""
    logging_setup._CONFIGURED = False
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_configure_logging_strips_library_console_handler(tmp_path):
    _reset_logging()
    root = logging.getLogger()
    # Simulate meshcore's import-time logging.basicConfig(): a root StreamHandler
    # writing to the console.
    leaked = logging.StreamHandler()
    root.addHandler(leaked)
    try:
        logging_setup.configure_logging(_fresh_config(tmp_path), stderr=False)
        # The leaked console handler is gone; only the file handler remains.
        assert leaked not in root.handlers
        assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
        assert not any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in root.handlers
        )
    finally:
        _reset_logging()


def test_configure_logging_no_console_when_file_disabled(tmp_path):
    _reset_logging()
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())  # pretend a lib added one
    try:
        # File logging off + no stderr (the TUI case): no console handler should
        # survive. The in-memory ring handler is always installed (feeding the
        # Logs surface) and also keeps Python's lastResort from firing, so no
        # NullHandler is needed.
        logging_setup.configure_logging(_fresh_config(tmp_path, file=""), stderr=False)
        assert not any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in root.handlers
        )
        assert any(
            isinstance(h, logging_setup.RingBufferHandler) for h in root.handlers
        )
    finally:
        _reset_logging()


def test_configure_logging_cli_keeps_one_console_handler(tmp_path):
    _reset_logging()
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())  # a leaked one to be removed
    try:
        logging_setup.configure_logging(_fresh_config(tmp_path), stderr=True)
        console = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
        ]
        # Exactly the one we add for the CLI (the leaked one was stripped first).
        assert len(console) == 1
    finally:
        _reset_logging()

