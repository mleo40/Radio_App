"""Tests for CLI quiet-mode stderr suppression (``_silence_stderr`` / ``_with_app``).

Covers a real gap: background library noise (e.g. Reticulum's own
[Notice]/[Error] lines, written directly to fd 2 from a background thread)
must be suppressed for the *entire* start/run/stop lifetime of a quiet
command, not just during ``app.start()``/``app.stop()`` separately, leaving
a gap during the command body itself where the noise leaked through.
"""
from __future__ import annotations

import asyncio
import os

from radio_app.cli import App, _silence_stderr, _with_app


def _capture_fd2(tmp_path, fn):
    """Run fn() with fd 2 redirected to a temp file; return its contents."""
    capture_path = tmp_path / "stderr_capture.txt"
    saved = os.dup(2)
    with capture_path.open("wb") as fh:
        os.dup2(fh.fileno(), 2)
        try:
            fn()
        finally:
            os.dup2(saved, 2)
            os.close(saved)
    return capture_path.read_text()


def test_silence_stderr_suppresses_raw_fd_writes(tmp_path):
    def _write_direct():
        with _silence_stderr():
            os.write(2, b"should not appear\n")

    text = _capture_fd2(tmp_path, _write_direct)
    assert "should not appear" not in text


class _FakeConfig:
    # Disables file logging entirely so configure_logging() never needs
    # config.path (a real Config only has one alongside a loaded file).
    data = {"logging": {"file": ""}}


class _FakeApp:
    def __init__(self):
        self.config = _FakeConfig()
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def test_with_app_quiet_suppresses_noise_during_func(tmp_path, monkeypatch):
    """Regression: noise written DURING func(app) must be suppressed too,
    not just noise during start()/stop() separately."""
    fake_app = _FakeApp()
    monkeypatch.setattr(
        App, "from_config_path", classmethod(lambda cls, path: fake_app)
    )

    async def func(app):
        os.write(2, b"leaked during func\n")
        return 0

    def _run_it():
        asyncio.run(_with_app("unused-config.toml", func, quiet=True))

    text = _capture_fd2(tmp_path, _run_it)
    assert "leaked during func" not in text
    assert fake_app.started and fake_app.stopped


def test_with_app_not_quiet_does_not_suppress(tmp_path, monkeypatch):
    fake_app = _FakeApp()
    monkeypatch.setattr(
        App, "from_config_path", classmethod(lambda cls, path: fake_app)
    )

    async def func(app):
        os.write(2, b"visible output\n")
        return 0

    def _run_it():
        asyncio.run(_with_app("unused-config.toml", func, quiet=False))

    text = _capture_fd2(tmp_path, _run_it)
    assert "visible output" in text
