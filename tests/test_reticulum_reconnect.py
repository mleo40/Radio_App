"""Tests for the Reticulum transport's external-only / auto-reconnect behavior.

The transport must never start its own RNS instance — it only attaches to an
external rnsd shared instance — and when rnsd isn't available yet it must keep
retrying in the background so it comes online automatically once rnsd appears.

These tests drive the orchestration (start → schedule retry → attach → stop)
without a real RNS by stubbing the single blocking ``_attempt_connect`` step.
"""

from __future__ import annotations

import asyncio
import signal
import types

import radio_app.transports.reticulum_transport as rt
from radio_app.transports.reticulum_transport import ReticulumTransport


def test_start_schedules_retry_when_rnsd_absent(monkeypatch):
    monkeypatch.setattr(rt, "_HAVE_RNS", True)
    t = ReticulumTransport({"reconnect_interval": 0.01})
    monkeypatch.setattr(t, "_attempt_connect", lambda: False)

    async def drive():
        await t.start()
        # rnsd absent: transport stays down but a background retry is armed.
        assert t._running is False
        assert t._reconnect_task is not None
        await t.stop()

    asyncio.run(drive())
    assert t._stopping is True
    assert t._reconnect_task is None


def test_reconnect_attaches_once_rnsd_appears(monkeypatch):
    monkeypatch.setattr(rt, "_HAVE_RNS", True)
    t = ReticulumTransport({"reconnect_interval": 0.01})
    calls = {"n": 0}

    def fake_attempt():
        calls["n"] += 1
        # rnsd shows up on the third probe.
        if calls["n"] >= 3:
            t._running = True
            return True
        return False

    monkeypatch.setattr(t, "_attempt_connect", fake_attempt)

    async def drive():
        await t.start()
        for _ in range(200):
            if t._running:
                break
            await asyncio.sleep(0.01)
        attached = t._running  # capture before stop() flips it back off
        await t.stop()
        return attached

    attached = asyncio.run(drive())
    assert attached is True
    assert calls["n"] >= 3


def test_successful_start_arms_no_retry(monkeypatch):
    monkeypatch.setattr(rt, "_HAVE_RNS", True)
    t = ReticulumTransport({"reconnect_interval": 0.01})

    def fake_attempt():
        t._running = True
        return True

    monkeypatch.setattr(t, "_attempt_connect", fake_attempt)

    async def drive():
        await t.start()
        assert t._running is True
        # Already attached on first try: no background retry task created.
        assert t._reconnect_task is None
        await t.stop()

    asyncio.run(drive())


def test_stop_cancels_pending_retry(monkeypatch):
    monkeypatch.setattr(rt, "_HAVE_RNS", True)
    t = ReticulumTransport({"reconnect_interval": 0.01})
    monkeypatch.setattr(t, "_attempt_connect", lambda: False)

    async def drive():
        await t.start()
        await asyncio.sleep(0.03)  # let the retry loop spin a couple times
        assert t._reconnect_task is not None
        await t.stop()

    asyncio.run(drive())
    assert t._stopping is True
    assert t._reconnect_task is None


def test_disabled_without_rns_does_not_retry(monkeypatch):
    # No RNS installed → transport is disabled and must NOT arm a retry loop.
    monkeypatch.setattr(rt, "_HAVE_RNS", False)
    t = ReticulumTransport({"reconnect_interval": 0.01})

    async def drive():
        await t.start()
        assert t._running is False
        assert t._reconnect_task is None

    asyncio.run(drive())


# -- the rnsd-singleton regression ------------------------------------------
#
# RNS.Reticulum.__init__ stashes its process-global singleton
# (Reticulum.__instance) BEFORE it tries to attach to rnsd, then raises
# SystemError when require_shared_instance=True and rnsd is absent. RNS never
# clears that reference, so a *second* RNS.Reticulum(...) raises OSError
# ("Attempt to reinitialise..."). That permanently wedged our reconnect loop:
# once rnsd was missing on the first probe, the transport never recovered even
# after rnsd came up. These tests pin the fix (_reset_rns_singleton).


class _FakeReticulum:
    """Mimics RNS.Reticulum's "stash singleton then maybe raise" behaviour."""

    _Reticulum__instance = None
    _Reticulum__exit_handler_ran = False
    _Reticulum__interface_detach_ran = False
    rnsd_up = False
    constructions = 0

    def __init__(self, configdir=None, require_shared_instance=False, **_):
        # Guard first — exactly like RNS, this is what raises on a leaked singleton.
        if _FakeReticulum._Reticulum__instance is not None:
            raise OSError(
                "Attempt to reinitialise Reticulum, when it was already running"
            )
        _FakeReticulum._Reticulum__instance = self
        _FakeReticulum.constructions += 1
        # Mirror RNS: install SIGINT/SIGTERM handlers during init. This raises
        # "signal only works in main thread" off the main thread unless the
        # transport neutralises signal.signal while constructing RNS.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.is_connected_to_shared_instance = False
        if _FakeReticulum.rnsd_up:
            self.is_connected_to_shared_instance = True
        elif require_shared_instance:
            # Mirror RNS: probing binds the shared-instance listener as a side
            # effect, then aborts without releasing it (the detach is a no-op).
            _FakeRNS.Transport.listener_open = True
            raise SystemError(
                "No shared instance available, but application required it"
            )


class _FakeTransport:
    listener_open = False
    detach_calls = 0

    @staticmethod
    def detach_interfaces():
        # RNS releases the leaked shared-instance listener here.
        _FakeTransport.detach_calls += 1
        _FakeTransport.listener_open = False


class _FakeRNS:
    Reticulum = _FakeReticulum
    Transport = _FakeTransport

    @staticmethod
    def exit():
        pass

    @staticmethod
    def prettyhexrep(_b):
        return "<addr>"


def _wire_fake_rns(monkeypatch):
    _FakeReticulum._Reticulum__instance = None
    _FakeReticulum.rnsd_up = False
    _FakeReticulum.constructions = 0
    _FakeTransport.listener_open = False
    _FakeTransport.detach_calls = 0
    monkeypatch.setattr(rt, "_HAVE_RNS", True)
    monkeypatch.setattr(rt, "RNS", _FakeRNS)


def test_failed_attempt_clears_leaked_singleton(monkeypatch):
    _wire_fake_rns(monkeypatch)
    t = ReticulumTransport({"reconnect_interval": 0.01})

    assert t._attempt_connect() is False
    # The half-built singleton RNS leaked must be cleared so the next try works.
    assert _FakeReticulum._Reticulum__instance is None


def test_failed_attempt_releases_squatted_listener(monkeypatch):
    # Starting before rnsd: RNS binds the shared-instance listener while probing
    # and leaks it. We must release it so we don't squat on the socket rnsd needs
    # — otherwise rnsd connects to us ("connected to another shared local
    # instance, this is probably NOT what you want!").
    _wire_fake_rns(monkeypatch)
    t = ReticulumTransport({"reconnect_interval": 0.01})

    assert t._attempt_connect() is False
    assert _FakeTransport.listener_open is False
    assert _FakeTransport.detach_calls >= 1



def test_reconnect_recovers_after_rnsd_absent_then_appears(monkeypatch):
    _wire_fake_rns(monkeypatch)
    t = ReticulumTransport({"reconnect_interval": 0.01})
    # Skip the heavy identity/LXMF setup; we only care about the attach handshake.
    monkeypatch.setattr(t, "_setup_after_connect", lambda: None)
    t._local_destination = types.SimpleNamespace(hash=b"\x00" * 16)

    # rnsd down for the first two probes: each must fail cleanly and construct
    # a *fresh* instance (proving the singleton was reset, not reused).
    assert t._attempt_connect() is False
    assert t._attempt_connect() is False
    assert _FakeReticulum.constructions == 2

    # rnsd appears: the next probe attaches and brings the transport up. Before
    # the fix this raised OSError and stayed down forever.
    _FakeReticulum.rnsd_up = True
    assert t._attempt_connect() is True
    assert t._running is True
    assert _FakeReticulum.constructions == 3


def test_attempt_connect_works_off_main_thread(monkeypatch):
    # The reconnect loop runs the blocking connect via asyncio.to_thread, i.e.
    # off the main thread. RNS installs signal handlers during init, which
    # raises "signal only works in main thread" there unless the transport
    # neutralises signal.signal. This reproduces the reported failure.
    _wire_fake_rns(monkeypatch)
    _FakeReticulum.rnsd_up = True
    t = ReticulumTransport({"reconnect_interval": 0.01})

    # Mirror LXMF.LXMRouter: setup installs signal handlers too, which fails off
    # the main thread unless setup runs under the signal-suppression guard.
    def fake_setup():
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    monkeypatch.setattr(t, "_setup_after_connect", fake_setup)
    t._local_destination = types.SimpleNamespace(hash=b"\x00" * 16)

    async def drive():
        return await asyncio.to_thread(t._attempt_connect)

    assert asyncio.run(drive()) is True
    assert t._running is True
    # signal.signal must be restored after construction, not left as the no-op.
    assert signal.signal is not None
    assert callable(signal.getsignal)




