"""Winlink transport tests using a fake Pat HTTP server (no Pat, no radio).

A tiny stdlib HTTP server stands in for Pat's API so we can exercise the adapter
end-to-end: outbox POST, inbox polling -> UnifiedMessage, the /api/connect
session trigger, reachability, capabilities and connect-URL building.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from radio_app.core.message import AddressType, DeliveryStatus, UnifiedMessage
from radio_app.transports.base import ReachabilityStatus
from radio_app.transports.winlink_transport import (
    CONNECT_METHODS,
    WinlinkTransport,
)


class _FakePat:
    """In-memory Pat API state shared with the request handler."""

    def __init__(self) -> None:
        self.inbox: list[dict] = []
        self.posted: list[dict] = []
        self.connects: list[str] = []
        self.bodies: dict[str, str] = {}
        # Connect URLs whose scheme prefix appears here return HTTP 500 (Pat
        # "Session failure"), letting tests simulate a path that won't connect.
        self.fail_schemes: list[str] = []


def _make_handler(state: _FakePat):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test output
            pass

        def _json(self, obj, code=200):
            payload = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                self._json({"connected": False, "active_listeners": []})
            elif path == "/api/mailbox/in":
                self._json([{"MID": m["MID"], "From": m["From"],
                             "To": m["To"], "Subject": m["Subject"]}
                            for m in state.inbox])
            elif path.startswith("/api/mailbox/in/"):
                mid = path.rsplit("/", 1)[-1]
                msg = next((m for m in state.inbox if m["MID"] == mid), None)
                if msg is None:
                    self._json({}, code=404)
                else:
                    self._json({**msg, "Body": state.bodies.get(mid, "")})
            elif path == "/api/connect":
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1])
                url = qs.get("url", [""])[0]
                state.connects.append(url)
                if any(url.startswith(p) for p in state.fail_schemes):
                    self._json({"NumReceived": 0}, code=500)
                else:
                    self._json({"NumReceived": 0})
            else:
                self._json({}, code=404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode()
            if self.path == "/api/mailbox/out":
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                state.posted.append(fields)
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b"Message posted")
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


@pytest.fixture()
def fake_pat():
    state = _FakePat()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    url = f"http://{host}:{port}"
    try:
        yield state, url
    finally:
        server.shutdown()
        server.server_close()


def _transport(url, **extra):
    cfg = {"pat_url": url, "callsign": "N0CALL", **extra}
    return WinlinkTransport(cfg)


def _capture(t):
    received: list[UnifiedMessage] = []

    async def _cb(msg):
        received.append(msg)

    t.on_receive(_cb)
    return received


# -- registration & capabilities ---------------------------------------------

def test_winlink_is_registered():
    from radio_app.transports.base import TRANSPORT_REGISTRY
    assert TRANSPORT_REGISTRY.get("winlink") is WinlinkTransport


def test_capabilities_are_email_class():
    caps = _transport("http://x").capabilities()
    assert caps.supports_addressing is True
    assert caps.supports_broadcast is False
    assert caps.supports_groups is False
    assert caps.prohibits_encryption is True
    assert caps.address_scheme == "email"


def test_telnet_method_reports_internet_no_modem():
    caps = _transport("http://x", connect="telnet").capabilities()
    assert caps.needs_internet is True


def test_rf_method_reports_no_internet():
    caps = _transport("http://x", connect="varahf").capabilities()
    assert caps.needs_internet is False


# -- connect URL building -----------------------------------------------------

def test_connect_url_telnet_default():
    t = _transport("http://x", connect="telnet")
    assert t.build_connect_url() == "telnet://"


def test_connect_url_rf_with_gateway():
    t = _transport("http://x", connect="varahf", gateway="KW1U")
    assert t.build_connect_url() == "varahf://KW1U"


def test_connect_url_raw_override_wins():
    t = _transport("http://x", connect="telnet", gateway="KW1U",
                   connect_url="ardop://N0XYZ?freq=7100")
    assert t.build_connect_url() == "ardop://N0XYZ?freq=7100"


def test_unknown_method_falls_back_to_telnet():
    t = _transport("http://x", connect="does-not-exist")
    assert t._method.scheme == "telnet"


def test_mercury_is_reachable_via_varahf_method():
    # Mercury is a VARA-compatible TNC -> reached through the varahf method.
    assert "varahf" in CONNECT_METHODS


# -- reachability -------------------------------------------------------------

def test_check_reachable_ok_when_pat_answers(fake_pat):
    state, url = fake_pat
    t = _transport(url)
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.OK


def test_check_reachable_down_when_pat_absent():
    t = _transport("http://127.0.0.1:1")  # nothing listening
    assert asyncio.run(t.check_reachable()) is ReachabilityStatus.DOWN


# -- send ---------------------------------------------------------------------

def test_send_posts_to_outbox(fake_pat):
    state, url = fake_pat

    async def run():
        t = _transport(url)
        await t.start()
        msg = UnifiedMessage.direct("N0CALL", "W1AW", "hello over winlink")
        ok = await t.send(msg)
        await t.stop()
        return ok, msg

    ok, msg = asyncio.run(run())
    assert ok is True
    assert msg.status is DeliveryStatus.SENT
    assert len(state.posted) == 1
    posted = state.posted[0]
    assert posted["to"] == "W1AW"
    assert posted["body"] == "hello over winlink"
    assert posted["subject"] == "hello over winlink"
    assert "date" in posted  # RFC3339 date is required by Pat


def test_send_uses_explicit_subject_from_metadata(fake_pat):
    state, url = fake_pat

    async def run():
        t = _transport(url)
        await t.start()
        msg = UnifiedMessage.direct(
            "N0CALL", "W1AW", "body text", metadata={"subject": "Net Report"}
        )
        await t.send(msg)
        await t.stop()

    asyncio.run(run())
    assert state.posted[0]["subject"] == "Net Report"


def test_send_rejects_group_messages(fake_pat):
    state, url = fake_pat

    async def run():
        t = _transport(url)
        await t.start()
        msg = UnifiedMessage.to_group("N0CALL", "TTP", "hi all")
        ok = await t.send(msg)
        await t.stop()
        return ok

    assert asyncio.run(run()) is False
    assert state.posted == []


def test_send_auto_connect_triggers_session(fake_pat):
    state, url = fake_pat

    async def run():
        t = _transport(url, auto_connect=True, connect="telnet")
        await t.start()
        await t.send(UnifiedMessage.direct("N0CALL", "W1AW", "ping"))
        await t.stop()

    asyncio.run(run())
    assert state.connects == ["telnet://"]


# -- inbound polling ----------------------------------------------------------

def test_inbox_poll_emits_new_message(fake_pat):
    state, url = fake_pat
    state.inbox.append({
        "MID": "ABC123",
        "From": {"Addr": "W1AW"},
        "To": [{"Addr": "N0CALL"}],
        "Subject": "Greetings",
    })
    state.bodies["ABC123"] = "Welcome to Winlink"

    async def run():
        t = _transport(url, poll_interval=5)
        received = _capture(t)
        await t.start()
        # First sweep primes (records existing MID without replay); force a
        # second sweep after adding a NEW message.
        await asyncio.sleep(0.2)
        state.inbox.append({
            "MID": "NEW999",
            "From": {"Addr": "K2ABC"},
            "To": [{"Addr": "N0CALL"}],
            "Subject": "Fresh",
        })
        state.bodies["NEW999"] = "Just arrived"
        await t._sweep_inbox()
        await t.stop()
        return received

    received = asyncio.run(run())
    mids = {m.metadata.get("mid") for m in received}
    assert "NEW999" in mids
    assert "ABC123" not in mids  # pre-existing history is not replayed
    fresh = next(m for m in received if m.metadata.get("mid") == "NEW999")
    assert fresh.sender == "K2ABC"
    assert fresh.content == "Just arrived"
    assert fresh.status is DeliveryStatus.RECEIVED
    assert fresh.address_type is AddressType.DIRECT
    assert fresh.metadata["subject"] == "Fresh"


def test_inbox_maps_attachments_into_metadata(fake_pat):
    state, url = fake_pat

    async def run():
        t = _transport(url)
        received = _capture(t)
        await t.start()
        await asyncio.sleep(0.1)  # prime
        state.inbox.append({
            "MID": "ATT1",
            "From": {"Addr": "W1AW"},
            "To": [{"Addr": "N0CALL"}],
            "Subject": "With file",
            "Files": [{"Name": "form.txt"}, {"Name": "photo.jpg"}],
        })
        state.bodies["ATT1"] = "see attached"
        await t._sweep_inbox()
        await t.stop()
        return received

    received = asyncio.run(run())
    msg = next(m for m in received if m.metadata.get("mid") == "ATT1")
    assert msg.metadata["attachments"] == ["form.txt", "photo.jpg"]


# -- auto fallback ------------------------------------------------------------

def test_auto_falls_back_to_second_method(fake_pat):
    state, url = fake_pat
    port = int(url.rsplit(":", 1)[1])
    state.fail_schemes = ["telnet"]  # internet "down": telnet returns 500

    async def run():
        # vara_port points at the (open) fake Pat so the varahf probe passes.
        t = _transport(url, connect="auto",
                       connect_order=["telnet", "varahf"], vara_port=port)
        await t.start()
        received = await t.connect_now()
        await t.stop()
        return received

    received = asyncio.run(run())
    assert received == 0
    assert state.connects == ["telnet://", "varahf://"]  # tried in order


def test_auto_skips_unreachable_rf_then_uses_telnet(fake_pat):
    state, url = fake_pat

    async def run():
        # vara_port closed (1) -> varahf probe DOWN -> skipped; telnet succeeds.
        t = _transport(url, connect="auto",
                       connect_order=["varahf", "telnet"], vara_port=1)
        await t.start()
        await t.connect_now()
        await t.stop()

    asyncio.run(run())
    assert state.connects == ["telnet://"]  # varahf never dialed (probe failed)


def test_auto_raises_when_all_paths_fail(fake_pat):
    state, url = fake_pat
    state.fail_schemes = ["telnet"]

    async def run():
        t = _transport(url, connect="auto", connect_order=["telnet"])
        await t.start()
        try:
            await t.connect_now()
            return "no-raise"
        except RuntimeError:
            return "raised"
        finally:
            await t.stop()

    assert asyncio.run(run()) == "raised"


def test_auto_capabilities_not_internet_only(fake_pat):
    state, url = fake_pat
    caps = _transport(url, connect="auto").capabilities()
    assert caps.needs_internet is False  # RF fallback exists


def test_connect_summary_describes_chain(fake_pat):
    state, url = fake_pat
    t = _transport(url, connect="auto", connect_order=["telnet", "varahf"])
    assert t.connect_summary() == "auto: telnet \u2192 varahf"


def test_path_status_reports_modem_up_down(fake_pat):
    state, url = fake_pat
    port = int(url.rsplit(":", 1)[1])

    async def run():
        # telnet (no probe) -> n/a; varahf probe at the open fake-Pat port -> up;
        # ardop at a closed port -> down.
        t = _transport(url, connect="auto",
                       connect_order=["telnet", "varahf", "ardop"],
                       vara_port=port, ardop_port=1)
        return await t.path_status()

    paths = {p["name"]: p for p in asyncio.run(run())}
    assert paths["telnet"]["reachable"] is None
    assert paths["varahf"]["reachable"] is True
    assert paths["ardop"]["reachable"] is False



