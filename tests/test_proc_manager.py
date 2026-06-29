"""Tests for ProcManager — on-demand process lifecycle manager.

All async tests are synchronous wrappers that use ``asyncio.run``; the
project does not install pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from radio_app.core.proc_manager import ProcManager, TransportDef, _TRANSPORT_DEFS
from radio_app.core.radio_interlock import RadioInterlock
from radio_app.transports.base import ReachabilityStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(launch_cmd: str = "", launch_wait_s: float = 0.1):
    """Return a minimal Config-like mock."""
    cfg = MagicMock()
    transport_cfg: dict = {}
    if launch_cmd:
        transport_cfg["launch_cmd"] = launch_cmd
    transport_cfg["launch_wait_s"] = launch_wait_s
    cfg.transports = MagicMock()
    cfg.transports.get = lambda name, default=None: (
        transport_cfg if name in _TRANSPORT_DEFS else (default or {})
    )
    cfg.save = MagicMock()
    cfg.set = MagicMock()
    return cfg


def _make_transport(reachable: bool = True, uses_shared_radio: bool = True):
    t = MagicMock()
    t.name = "js8call"
    t.start = AsyncMock()
    t.stop = AsyncMock()
    cap = MagicMock()
    cap.uses_shared_radio = uses_shared_radio
    t.capabilities.return_value = cap
    t.check_reachable = AsyncMock(
        return_value=ReachabilityStatus.OK if reachable else ReachabilityStatus.DOWN
    )
    return t


def _make_pm(launch_cmd: str = "", launch_wait_s: float = 0.1) -> ProcManager:
    cfg = _make_config(launch_cmd=launch_cmd, launch_wait_s=launch_wait_s)
    il = RadioInterlock(["js8call", "wsjt_x"])
    return ProcManager(cfg, il)


async def _mock_create_subprocess(*args, **kwargs):
    """Replacement for asyncio.create_subprocess_exec that returns a fake process."""
    proc = MagicMock()
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.wait = AsyncMock()
    return proc


# ---------------------------------------------------------------------------
# Transport table
# ---------------------------------------------------------------------------

class TestTransportDefs:
    def test_known_transports_contains_major(self):
        known = ProcManager.known_transports()
        for name in ("js8call", "wsjt_x", "winlink"):
            assert name in known

    def test_definition_returns_correct_type(self):
        pm = ProcManager(MagicMock(), MagicMock())
        defn = pm.definition("js8call")
        assert isinstance(defn, TransportDef)
        assert defn.process_name == "js8call"
        assert defn.contends_radio is True

    def test_definition_winlink_not_contend(self):
        pm = ProcManager(MagicMock(), MagicMock())
        defn = pm.definition("winlink")
        assert defn.contends_radio is False

    def test_definition_unknown_returns_none(self):
        pm = ProcManager(MagicMock(), MagicMock())
        assert pm.definition("not_a_transport") is None


# ---------------------------------------------------------------------------
# is_running (OS process table)
# ---------------------------------------------------------------------------

class TestIsRunning:
    def test_true_when_pgrep_returns_zero(self):
        pm = _make_pm()
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert pm.is_running("js8call") is True

    def test_false_when_pgrep_returns_nonzero(self):
        pm = _make_pm()
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert pm.is_running("js8call") is False

    def test_false_when_pgrep_not_found(self):
        pm = _make_pm()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert pm.is_running("js8call") is False

    def test_false_for_unknown_transport(self):
        pm = _make_pm()
        assert pm.is_running("unknown_transport") is False

    def test_false_on_timeout(self):
        pm = _make_pm()
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("pgrep", 1),
        ):
            assert pm.is_running("js8call") is False


# ---------------------------------------------------------------------------
# we_own
# ---------------------------------------------------------------------------

class TestWeOwn:
    def test_false_initially(self):
        pm = _make_pm()
        assert pm.we_own("js8call") is False

    def test_true_for_live_owned_proc(self):
        pm = _make_pm()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        pm._owned["js8call"] = mock_proc
        assert pm.we_own("js8call") is True

    def test_false_after_proc_exits(self):
        pm = _make_pm()
        mock_proc = MagicMock()
        mock_proc.returncode = 0   # exited
        pm._owned["js8call"] = mock_proc
        assert pm.we_own("js8call") is False


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStart:
    def test_happy_path(self):
        """start() spawns process, waits for reachability, calls transport.start()."""
        pm = _make_pm(launch_wait_s=0.2)
        transport = _make_transport(reachable=True)

        async def run():
            with (
                patch("subprocess.run", return_value=MagicMock(returncode=1)),
                patch("asyncio.create_subprocess_exec", side_effect=_mock_create_subprocess),
            ):
                return await pm.start("js8call", transport)

        ok = asyncio.run(run())
        assert ok is True
        transport.start.assert_awaited_once()
        assert pm.we_own("js8call")

    def test_skip_spawn_if_running_externally(self):
        """If process is already up (not owned), skip spawn and reconnect."""
        pm = _make_pm(launch_wait_s=0.1)
        transport = _make_transport(reachable=True)

        async def run():
            with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                return await pm.start("js8call", transport)

        ok = asyncio.run(run())
        assert ok is True
        transport.start.assert_awaited_once()
        # We must NOT claim ownership of an externally-launched process
        assert not pm.we_own("js8call")

    def test_returns_false_on_binary_not_found(self):
        pm = _make_pm(launch_wait_s=0.1)
        transport = _make_transport()

        async def run():
            with (
                patch("subprocess.run", return_value=MagicMock(returncode=1)),
                patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
            ):
                return await pm.start("js8call", transport)

        ok = asyncio.run(run())
        assert ok is False
        transport.start.assert_not_awaited()

    def test_returns_false_on_reachability_timeout(self):
        pm = _make_pm(launch_wait_s=0.2)
        transport = _make_transport(reachable=False)  # never comes up

        async def run():
            with (
                patch("subprocess.run", return_value=MagicMock(returncode=1)),
                patch("asyncio.create_subprocess_exec", side_effect=_mock_create_subprocess),
            ):
                return await pm.start("js8call", transport)

        ok = asyncio.run(run())
        assert ok is False
        transport.start.assert_not_awaited()

    def test_returns_false_for_unknown_transport(self):
        pm = _make_pm()
        transport = _make_transport()

        async def run():
            return await pm.start("unknown", transport)

        ok = asyncio.run(run())
        assert ok is False

    def test_stops_owned_radio_contenders_before_spawn(self):
        """Owned radio-contending processes must stop before a new radio transport starts."""
        il = RadioInterlock(["js8call", "wsjt_x"])
        cfg = _make_config(launch_wait_s=0.2)
        pm = ProcManager(cfg, il)

        wsjt_proc = MagicMock()
        wsjt_proc.returncode = None
        wsjt_proc.terminate = MagicMock()
        wsjt_proc.wait = AsyncMock()
        pm._owned["wsjt_x"] = wsjt_proc

        transport_js8 = _make_transport(reachable=True, uses_shared_radio=True)

        async def run():
            with (
                patch("subprocess.run", return_value=MagicMock(returncode=1)),
                patch("asyncio.create_subprocess_exec", side_effect=_mock_create_subprocess),
            ):
                return await pm.start("js8call", transport_js8)

        ok = asyncio.run(run())
        assert ok is True
        wsjt_proc.terminate.assert_called_once()
        assert "wsjt_x" not in pm._owned

    def test_transfers_interlock_on_radio_transport(self):
        """Interlock holder must be updated to the new transport."""
        il = RadioInterlock(["js8call", "wsjt_x"])
        il.acquire("wsjt_x")
        cfg = _make_config(launch_wait_s=0.2)
        pm = ProcManager(cfg, il)

        transport_js8 = _make_transport(reachable=True, uses_shared_radio=True)

        async def run():
            with (
                patch("subprocess.run", return_value=MagicMock(returncode=1)),
                patch("asyncio.create_subprocess_exec", side_effect=_mock_create_subprocess),
            ):
                return await pm.start("js8call", transport_js8)

        ok = asyncio.run(run())
        assert ok is True
        assert il.holder == "js8call"

    def test_prompt_fn_called_for_unconfigured_transport(self):
        """prompt_fn must be invoked when no launch_cmd is set."""
        pm = _make_pm(launch_cmd="")
        transport = _make_transport(reachable=True)
        prompted: list[tuple] = []

        async def my_prompt(name: str, default: str) -> str | None:
            prompted.append((name, default))
            return None  # accept default

        async def run():
            with (
                patch("subprocess.run", return_value=MagicMock(returncode=1)),
                patch("asyncio.create_subprocess_exec", side_effect=_mock_create_subprocess),
            ):
                return await pm.start("js8call", transport, prompt_fn=my_prompt)

        ok = asyncio.run(run())
        assert ok is True
        assert len(prompted) == 1
        assert prompted[0][0] == "js8call"


# ---------------------------------------------------------------------------
# stop() / stop_all()
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_is_noop_when_not_owned(self):
        pm = _make_pm()
        asyncio.run(pm.stop("js8call"))  # should not raise

    def test_stop_terminates_owned_process(self):
        pm = _make_pm()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()
        pm._owned["js8call"] = mock_proc

        asyncio.run(pm.stop("js8call"))

        mock_proc.terminate.assert_called_once()
        assert "js8call" not in pm._owned

    def test_stop_calls_transport_stop_before_terminate(self):
        """transport.stop() must be called before the OS signal."""
        pm = _make_pm()
        call_order: list[str] = []

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock(
            side_effect=lambda: call_order.append("terminate")
        )
        mock_proc.wait = AsyncMock()
        pm._owned["js8call"] = mock_proc

        transport = _make_transport()
        transport.stop = AsyncMock(
            side_effect=lambda: call_order.append("transport_stop")
        )

        asyncio.run(pm.stop("js8call", transport))

        assert call_order[0] == "transport_stop"
        assert "terminate" in call_order

    def test_stop_all_clears_all_owned(self):
        pm = _make_pm()
        for name in ("js8call", "wsjt_x"):
            proc = MagicMock()
            proc.returncode = None
            proc.terminate = MagicMock()
            proc.wait = AsyncMock()
            pm._owned[name] = proc

        asyncio.run(pm.stop_all())
        assert pm._owned == {}
