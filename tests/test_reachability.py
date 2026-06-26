"""Tests for the transport reachability contract (Milestone 1).

These exercise the passive ``check_reachable`` probe and the ``surface``
descriptor without any radio hardware: a throwaway TCP server stands in for a
JS8Call / Mercury control endpoint so we can assert OK vs DOWN.
"""

from __future__ import annotations

import asyncio

import pytest

from radio_app.transports.base import (
    ReachabilityStatus,
    Transport,
    TransportCapabilities,
    probe_tcp,
)
from radio_app.transports.js8call_transport import JS8CallTransport
from radio_app.transports.mercury_transport import MercuryTransport
from radio_app.transports.meshcore_transport import MeshCoreTransport


def _meshcore_tcp(**cfg):
    """MeshCoreTransport pinned to the TCP backend (used by socket probes)."""
    return MeshCoreTransport({"connection": "tcp", **cfg})


class _BareTransport(Transport):
    name = "bare-test"

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities()

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg) -> bool:  # noqa: ANN001
        return True


def test_default_check_reachable_follows_running():
    t = _BareTransport()
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.DOWN
    asyncio.run(t.start())
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.OK


def test_default_surface_is_chat():
    assert _BareTransport().surface == "chat"
    assert JS8CallTransport({}).surface == "chat"
    assert MeshCoreTransport({}).surface == "chat"


def test_probe_tcp_down_on_closed_port():
    # Port 1 is privileged and effectively never accepting in CI/dev.
    status = asyncio.run(probe_tcp("127.0.0.1", 1, timeout=0.5))
    assert status is ReachabilityStatus.DOWN


@pytest.mark.parametrize("cls", [JS8CallTransport, MercuryTransport])
def test_socket_transport_reachable_against_live_server(cls):
    async def run() -> ReachabilityStatus:
        async def _handle(reader, writer):  # noqa: ANN001
            writer.close()

        server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = cls({"host": "127.0.0.1", "port": port})
        try:
            return await transport.check_reachable()
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(run()) is ReachabilityStatus.OK


@pytest.mark.parametrize("cls", [JS8CallTransport, MercuryTransport])
def test_socket_transport_down_when_no_server(cls):
    transport = cls({"host": "127.0.0.1", "port": 1})
    assert asyncio.run(transport.check_reachable()) is ReachabilityStatus.DOWN


def test_meshcore_tcp_reachable_against_live_server():
    async def run() -> ReachabilityStatus:
        async def _handle(reader, writer):  # noqa: ANN001
            writer.close()

        server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = _meshcore_tcp(host="127.0.0.1", tcp_port=port)
        try:
            return await transport.check_reachable()
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(run()) is ReachabilityStatus.OK


def test_meshcore_tcp_down_when_no_server():
    transport = _meshcore_tcp(host="127.0.0.1", tcp_port=1)
    assert asyncio.run(transport.check_reachable()) is ReachabilityStatus.DOWN


def test_meshcore_serial_reachable_reflects_device_path(tmp_path):
    """Serial backend: OK when the device path exists, DOWN otherwise (passive)."""
    import radio_app.transports.meshcore_transport as mc

    fake_dev = tmp_path / "ttyFAKE"
    fake_dev.write_text("")  # path exists -> reachable
    t_ok = mc.MeshCoreTransport({"connection": "serial", "port": str(fake_dev)})
    assert asyncio.run(t_ok.check_reachable()) is ReachabilityStatus.OK

    t_down = mc.MeshCoreTransport(
        {"connection": "serial", "port": str(tmp_path / "nope")}
    )
    assert asyncio.run(t_down.check_reachable()) is ReachabilityStatus.DOWN


def test_meshcore_disabled_without_library(monkeypatch):
    """start() leaves the transport down when the meshcore lib is unavailable."""
    import radio_app.transports.meshcore_transport as mc

    monkeypatch.setattr(mc, "_HAVE_MESHCORE", False)
    transport = mc.MeshCoreTransport({"connection": "tcp", "tcp_port": 1})
    asyncio.run(transport.start())
    assert transport.running is False


