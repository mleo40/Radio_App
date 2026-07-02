"""Tests for the on-demand JS8Call band scan.

Covers:
- BandResult.avg_snr, BandScanReport.best()
- _is_heartbeat_ack() matching
- run_band_scan() happy path (mocked transport/router) + interlock-blocked
- Router.add_ui_callback / remove_ui_callback
- radioapp bandscan CLI guards
- /bandscan TUI command (usage, not-running, radio-busy, accept/decline modal)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from radio_app.core.band_scan import (
    BandResult,
    BandScanReport,
    _is_heartbeat_ack,
    run_band_scan,
)
from radio_app.core.message import UnifiedMessage
from radio_app.core.radio_interlock import RadioInterlock


def _msg(transport="js8call", content="HEARTBEAT SNR -5", **metadata):
    return UnifiedMessage(
        sender="W1TEST", content=content, transport=transport, metadata=metadata
    )


# ---------------------------------------------------------------------------
# BandResult / BandScanReport
# ---------------------------------------------------------------------------

def test_band_result_avg_snr_empty():
    assert BandResult(band="40m").avg_snr is None


def test_band_result_avg_snr():
    r = BandResult(band="40m", snrs=[-5, -3, -7])
    assert r.avg_snr == pytest.approx(-5.0)


def test_report_best_by_heard_count():
    a = BandResult(band="80m", heard_count=1, snrs=[-2])
    b = BandResult(band="40m", heard_count=3, snrs=[-10])
    report = BandScanReport(results=[a, b], original_band="20m")
    assert report.best() is b


def test_report_best_tiebreak_by_snr():
    a = BandResult(band="80m", heard_count=2, snrs=[-10, -10])
    b = BandResult(band="40m", heard_count=2, snrs=[-2, -2])
    report = BandScanReport(results=[a, b], original_band=None)
    assert report.best() is b


def test_report_best_none_when_silent():
    a = BandResult(band="80m", heard_count=0)
    report = BandScanReport(results=[a], original_band=None)
    assert report.best() is None


# ---------------------------------------------------------------------------
# _is_heartbeat_ack()
# ---------------------------------------------------------------------------

def test_is_heartbeat_ack_matches():
    msg = _msg(js8_command="SNR", snr_report=-5)
    assert _is_heartbeat_ack(msg) is True


def test_is_heartbeat_ack_rejects_other_transport():
    msg = _msg(transport="reticulum", js8_command="SNR", snr_report=-5)
    assert _is_heartbeat_ack(msg) is False


def test_is_heartbeat_ack_rejects_missing_snr_report():
    msg = _msg(content="HEARTBEAT SNR?", js8_command="SNR?")
    assert _is_heartbeat_ack(msg) is False


def test_is_heartbeat_ack_rejects_non_heartbeat_snr():
    """An ordinary /cmd SNR? exchange between other stations isn't our scan."""
    msg = _msg(content="W1AW SNR -3", js8_command="SNR", snr_report=-3)
    assert _is_heartbeat_ack(msg) is False


def test_is_heartbeat_ack_rejects_wrong_command():
    msg = _msg(content="HEARTBEAT GRID FN31", js8_command="GRID")
    assert _is_heartbeat_ack(msg) is False


# ---------------------------------------------------------------------------
# run_band_scan()
# ---------------------------------------------------------------------------

class _FakeRouter:
    """Feeds each add_ui_callback() call a preset batch of messages immediately.

    Batches are consumed in call order, matching one batch per scanned band.
    """

    def __init__(self, batches: list[list[UnifiedMessage]]):
        self._batches = list(batches)
        self._call_index = 0
        self.active_callbacks: list = []

    def add_ui_callback(self, callback) -> None:
        self.active_callbacks.append(callback)
        if self._call_index < len(self._batches):
            for msg in self._batches[self._call_index]:
                callback(msg, None)
        self._call_index += 1

    def remove_ui_callback(self, callback) -> None:
        if callback in self.active_callbacks:
            self.active_callbacks.remove(callback)


def _mock_transport(current_band="20m"):
    t = MagicMock()
    t.current_band.return_value = current_band
    t.set_dial_freq = AsyncMock(return_value=True)
    t.send_heartbeat = AsyncMock(return_value=True)
    return t


def test_run_band_scan_happy_path():
    t = _mock_transport(current_band="20m")
    router = _FakeRouter(
        batches=[
            [_msg(content="HEARTBEAT SNR -5", js8_command="SNR", snr_report=-5)],
            [],  # 40m: silent
        ]
    )
    interlock = RadioInterlock(contenders=["js8call"])

    report = asyncio.run(
        run_band_scan(t, router, interlock, ["80m", "40m"], dwell_s=0, grid="FN31")
    )

    assert report is not None
    assert report.original_band == "20m"
    assert [r.band for r in report.results] == ["80m", "40m"]
    assert report.results[0].heard_count == 1
    assert report.results[0].snrs == [-5]
    assert report.results[1].heard_count == 0
    assert report.best().band == "80m"
    # Radio released after the scan, not left held.
    assert interlock.holder is None
    # set_dial_freq called once per band; send_heartbeat once per band.
    assert t.set_dial_freq.await_count == 2
    assert t.send_heartbeat.await_count == 2
    # Listener cleaned up after each band, none left registered.
    assert router.active_callbacks == []


def test_run_band_scan_blocked_by_interlock():
    t = _mock_transport()
    router = _FakeRouter(batches=[])
    interlock = RadioInterlock(contenders=["js8call", "winlink"])
    interlock.acquire("winlink")

    logged = []
    report = asyncio.run(
        run_band_scan(
            t, router, interlock, ["80m"], dwell_s=0, on_progress=logged.append
        )
    )

    assert report is None
    assert any("radio busy" in line.lower() for line in logged)
    t.set_dial_freq.assert_not_awaited()


def test_run_band_scan_unknown_band_skipped():
    t = _mock_transport()
    router = _FakeRouter(batches=[[]])
    interlock = RadioInterlock(contenders=["js8call"])

    logged = []
    report = asyncio.run(
        run_band_scan(
            t, router, interlock, ["notaband", "80m"], dwell_s=0,
            on_progress=logged.append,
        )
    )

    assert report is not None
    assert [r.band for r in report.results] == ["80m"]
    assert any("unknown band" in line.lower() for line in logged)


# ---------------------------------------------------------------------------
# Router.add_ui_callback / remove_ui_callback
# ---------------------------------------------------------------------------

def test_router_remove_ui_callback():
    from radio_app.core.filters import FilterEngine
    from radio_app.core.groups import GroupRegistry
    from radio_app.core.router import Router
    from radio_app.core.store import MessageStore

    def _caps():
        c = MagicMock()
        c.supports_groups = False
        c.supports_chunking = False
        c.max_message_size = 0
        c.requires_identity = False
        c.supports_encryption = False
        c.is_hf = False
        return c

    mock_t = MagicMock()
    mock_t.name = "js8call"
    mock_t.capabilities.return_value = _caps()
    mock_t.on_receive = MagicMock()

    router = Router(
        transports=[mock_t],
        store=MagicMock(spec=MessageStore),
        groups=GroupRegistry(),
        filters=FilterEngine([], GroupRegistry()),
    )
    seen = []
    cb = seen.append
    router.add_ui_callback(cb)
    assert cb in router._ui_callbacks  # noqa: SLF001
    router.remove_ui_callback(cb)
    assert cb not in router._ui_callbacks  # noqa: SLF001
    router.remove_ui_callback(cb)  # no-op, doesn't raise


# ---------------------------------------------------------------------------
# CLI: radioapp bandscan
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path, extra=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[general]\n[logging]\nfile = \"\"\n[station]\ncallsign = \"W1T\"\n" + extra
    )
    return cfg


def test_cli_bandscan_no_bands(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(tmp_path)
    rc = cli_main(["--config", str(cfg), "bandscan", ""])
    assert rc == 2
    assert "no bands" in capsys.readouterr().err.lower()


def test_cli_bandscan_js8call_not_running(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(
        tmp_path, "[transports.js8call]\nenabled = true\nport = 2442\n"
    )
    rc = cli_main(["--config", str(cfg), "bandscan", "80m,40m"])
    assert rc == 1
    assert "not enabled/running" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# TUI: /bandscan command
# ---------------------------------------------------------------------------

pytest.importorskip("textual")
from textual.widgets import RichLog  # noqa: E402

import radio_app.core.band_scan as band_scan_module  # noqa: E402
from radio_app.ui.tui import BandScanResultScreen, RadioTUI  # noqa: E402

_TUI_CONFIG = """\
[general]
display_name = "T"
[logging]
file = ""
[station]
callsign = "W1TEST"
grid_square = "FN31"
[transports.js8call]
enabled = true
port = 2442
"""


@pytest.fixture()
def tui_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_TUI_CONFIG)
    return str(p)


def _log_text(app: RadioTUI) -> str:
    return "\n".join(s.text for s in app.query_one("#messages", RichLog).lines)


def test_tui_bandscan_usage(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/bandscan")
            await pilot.pause()
            assert "usage" in _log_text(app).lower()

    asyncio.run(run())


def test_tui_bandscan_not_running(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/bandscan 80m,40m 5")
            await pilot.pause(0.2)
            assert "not running" in _log_text(app).lower()

    asyncio.run(run())


def test_tui_bandscan_radio_busy(tui_config, monkeypatch):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            t = next(x for x in app.core.transports if x.name == "js8call")
            monkeypatch.setattr(type(t), "running", property(lambda self: True))
            # Force another (unrelated) contender to already hold the radio.
            il = app.core.radio_interlock
            il._contenders.add("winlink")  # noqa: SLF001
            il._holder = "winlink"  # noqa: SLF001
            await app._handle_command("/bandscan 80m 5")
            await pilot.pause(0.2)
            assert "blocked" in _log_text(app).lower()

    asyncio.run(run())


def test_tui_bandscan_accept_switches_band(tui_config, monkeypatch):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            t = next(x for x in app.core.transports if x.name == "js8call")
            monkeypatch.setattr(type(t), "running", property(lambda self: True))
            set_freq = AsyncMock(return_value=True)
            monkeypatch.setattr(t, "set_dial_freq", set_freq)

            fake_report = band_scan_module.BandScanReport(
                results=[
                    band_scan_module.BandResult(band="80m", heard_count=1, snrs=[-5]),
                    band_scan_module.BandResult(band="40m", heard_count=0),
                ],
                original_band="20m",
            )
            fake_run = AsyncMock(return_value=fake_report)
            monkeypatch.setattr(band_scan_module, "run_band_scan", fake_run)

            notifications: list[str] = []
            monkeypatch.setattr(
                app, "notify", lambda msg, **kw: notifications.append(msg)
            )

            await app._handle_command("/bandscan 80m,40m 5")
            await pilot.pause()
            # A toast fires regardless of which view the operator is on when
            # the scan finishes — not just the modal, which only shows here
            # because a good band was actually found.
            assert any("80m" in n for n in notifications)
            # Modal is up, offering to switch to the best band (80m).
            assert isinstance(app.screen, BandScanResultScreen)
            app.screen.dismiss(True)
            await pilot.pause()

            set_freq.assert_awaited()
            assert "switched to 80m" in _log_text(app).lower()

    asyncio.run(run())


def test_tui_bandscan_no_results_shows_toast(tui_config, monkeypatch):
    """The 'nothing heard' path has no modal to interrupt with — must toast."""
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            t = next(x for x in app.core.transports if x.name == "js8call")
            monkeypatch.setattr(type(t), "running", property(lambda self: True))
            set_freq = AsyncMock(return_value=True)
            monkeypatch.setattr(t, "set_dial_freq", set_freq)

            fake_report = band_scan_module.BandScanReport(
                results=[
                    band_scan_module.BandResult(band="80m", heard_count=0),
                    band_scan_module.BandResult(band="40m", heard_count=0),
                ],
                original_band="20m",
            )
            fake_run = AsyncMock(return_value=fake_report)
            monkeypatch.setattr(band_scan_module, "run_band_scan", fake_run)

            notifications: list[tuple[str, dict]] = []
            monkeypatch.setattr(
                app, "notify", lambda msg, **kw: notifications.append((msg, kw))
            )

            await app._handle_command("/bandscan 80m,40m 5")
            await pilot.pause()

            # No modal in this path -- the toast is the only completion signal.
            assert not isinstance(app.screen, BandScanResultScreen)
            assert len(notifications) == 1
            msg, kw = notifications[0]
            assert "no replies heard" in msg.lower()
            assert kw.get("severity") == "warning"
            # Original band is restored automatically since there's nothing
            # to choose between.
            from radio_app.transports.js8call_transport import dial_for_band
            set_freq.assert_awaited_with(dial_for_band("20m"))

    asyncio.run(run())


def test_tui_bandscan_decline_restores_original_band(tui_config, monkeypatch):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            t = next(x for x in app.core.transports if x.name == "js8call")
            monkeypatch.setattr(type(t), "running", property(lambda self: True))
            set_freq = AsyncMock(return_value=True)
            monkeypatch.setattr(t, "set_dial_freq", set_freq)

            fake_report = band_scan_module.BandScanReport(
                results=[
                    band_scan_module.BandResult(band="80m", heard_count=1, snrs=[-5]),
                ],
                original_band="20m",
            )
            fake_run = AsyncMock(return_value=fake_report)
            monkeypatch.setattr(band_scan_module, "run_band_scan", fake_run)

            await app._handle_command("/bandscan 80m 5")
            await pilot.pause()
            assert isinstance(app.screen, BandScanResultScreen)
            app.screen.dismiss(False)
            await pilot.pause()

            from radio_app.transports.js8call_transport import dial_for_band
            set_freq.assert_awaited_with(dial_for_band("20m"))

    asyncio.run(run())
