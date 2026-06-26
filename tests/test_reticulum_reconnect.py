"""Tests for the Reticulum transport's external-only / auto-reconnect behavior.

The transport must never start its own RNS instance — it only attaches to an
external rnsd shared instance — and when rnsd isn't available yet it must keep
retrying in the background so it comes online automatically once rnsd appears.

These tests drive the orchestration (start → schedule retry → attach → stop)
without a real RNS by stubbing the single blocking ``_attempt_connect`` step.
"""

from __future__ import annotations

import asyncio

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


