"""Winlink transport tests using a fake Pat HTTP server (no Pat, no radio).

A tiny stdlib HTTP server stands in for Pat's API so we can exercise the adapter
end-to-end: outbox POST, inbox polling -> UnifiedMessage, the /api/connect
session trigger, reachability, capabilities and connect-URL building.
"""

from __future__ import annotations

import asyncio
import email
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
        self.outbox: list[dict] = []
        self.sent: list[dict] = []
        self.posted: list[dict] = []
        self.connects: list[str] = []
        self.bodies: dict[str, str] = {}
        # (mid, attachment-name) -> raw bytes for inbound attachment downloads.
        self.att_bytes: dict[tuple[str, str], bytes] = {}
        self._mid_seq = 0
        # When True, a successful connect forwards (empties) the outbox into the
        # sent box, simulating Pat delivering queued mail to the CMS/RMS.
        self.forward_on_connect = True
        # Connect URLs whose scheme prefix appears here return HTTP 500 (Pat
        # "Session failure"), letting tests simulate a path that won't connect.
        self.fail_schemes: list[str] = []
        # -- Winlink forms (browserless flow) --------------------------------
        # forminstance cookie -> built form message (set by POST /api/form).
        self.form_data: dict[str, dict] = {}
        self.forms_version = "1.2.3.4"
        self.forms_action = "update"
        self.template_text = "Preview <var City> over winlink"
        self.forms_catalog = {
            "name": "Standard Forms",
            "version": "1.2.3.4",
            "form_count": 2,
            "forms": [],
            "folders": [
                {"name": "ICS", "form_count": 1, "forms": [
                    {"name": "ICS213", "template_path": "ICS/ICS213.txt"},
                ], "folders": []},
                {"name": "Welfare", "form_count": 1, "forms": [
                    {"name": "Radiogram", "template_path": "Welfare/Rgram.txt"},
                ], "folders": []},
            ],
        }

    def next_mid(self) -> str:
        self._mid_seq += 1
        return f"OUT{self._mid_seq}"


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

        def _raw(self, data: bytes, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _text(self, text: str, code=200):
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _cookie(self, name: str) -> str | None:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
            return None

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                self._json({"connected": False, "active_listeners": []})
            elif path == "/api/formcatalog":
                self._json(state.forms_catalog)
            elif path == "/api/template":
                self._text(state.template_text)
            elif path == "/api/form":
                built = state.form_data.get(self._cookie("forminstance") or "")
                if built is None:
                    self._json({}, code=404)
                else:
                    self._json(built)
            elif path == "/api/mailbox/in":
                self._json([{"MID": m["MID"], "From": m["From"],
                             "To": m["To"], "Subject": m["Subject"]}
                            for m in state.inbox])
            elif path == "/api/mailbox/out":
                self._json([{"MID": m["MID"], "To": m.get("To"),
                             "Subject": m.get("Subject", ""),
                             "Files": m.get("Files", [])}
                            for m in state.outbox])
            elif path.startswith("/api/mailbox/in/"):
                rest = path[len("/api/mailbox/in/"):]
                parts = rest.split("/")
                if len(parts) == 2:
                    mid, name = parts[0], urllib.parse.unquote(parts[1])
                    data = state.att_bytes.get((mid, name))
                    if data is None:
                        self._raw(b"", code=404)
                    else:
                        self._raw(data)
                    return
                mid = parts[0]
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
                    if state.forward_on_connect:
                        state.sent.extend(state.outbox)
                        state.outbox.clear()
                    self._json({"NumReceived": 0})
            else:
                self._json({}, code=404)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            path = self.path.split("?", 1)[0]
            if path == "/api/form":
                # Build a form: store the computed message under the cookie key.
                key = self._cookie("forminstance") or ""
                try:
                    payload = json.loads(raw.decode() or "{}")
                except ValueError:
                    payload = {}
                responses = payload.get("responses", {})
                qs = urllib.parse.parse_qs(
                    self.path.split("?", 1)[1] if "?" in self.path else ""
                )
                template = qs.get("template", [""])[0]
                name = template.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "form"
                state.form_data[key] = {
                    "msg_to": "",
                    "msg_cc": "",
                    "msg_subject": f"{name} report",
                    "msg_body": "City: " + str(responses.get("city", "")),
                    "_attachment": f"RMS_Express_Form_{name}.xml",
                }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<script>window.close()</script>")
                return
            if path == "/api/formsUpdate":
                self._json({
                    "newestVersion": state.forms_version,
                    "action": state.forms_action,
                })
                return
            if self.path == "/api/mailbox/out":
                mid = state.next_mid()
                if ctype.startswith("multipart/"):
                    fields, fnames, adata = _parse_multipart(ctype, raw)
                    state.posted.append({**fields, "files": fnames})
                    state.outbox.append({
                        "MID": mid,
                        "To": [{"Addr": fields.get("to", "")}],
                        "Subject": fields.get("subject", ""),
                        "Files": [{"Name": n} for n in fnames],
                    })
                else:
                    fields = {
                        k: v[0]
                        for k, v in urllib.parse.parse_qs(raw.decode()).items()
                    }
                    # A form-instance cookie means Pat re-attaches the form XML.
                    key = self._cookie("forminstance")
                    files = []
                    if key and key in state.form_data:
                        files = [{"Name": state.form_data[key]["_attachment"]}]
                        fields["forminstance"] = key
                    state.posted.append(fields)
                    state.outbox.append({
                        "MID": mid,
                        "To": [{"Addr": fields.get("to", "")}],
                        "Subject": fields.get("subject", ""),
                        "Files": files,
                    })
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b"Message posted")
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def _parse_multipart(content_type: str, raw: bytes):
    """Parse a multipart/form-data body into (fields, file-names, file-bytes)."""
    parsed = email.message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
    )
    fields: dict[str, str] = {}
    fnames: list[str] = []
    adata: dict[str, bytes] = {}
    for part in parsed.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            fnames.append(filename)
            adata[filename] = payload
        else:
            name = part.get_param("name", header="content-disposition")
            if name:
                fields[name] = payload.decode("utf-8", "replace")
    return fields, fnames, adata



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


# -- attachments (outbound multipart + inbound download) ----------------------

def test_outbound_attachment_uses_multipart(fake_pat, tmp_path):
    state, url = fake_pat
    f = tmp_path / "note.txt"
    f.write_text("attach me")

    async def run():
        t = _transport(url)
        await t.start()
        msg = UnifiedMessage.direct(
            "N0CALL", "W1AW", "see attached",
            metadata={"subject": "Files", "attach": [str(f)]},
        )
        ok = await t.send(msg)
        await t.stop()
        return ok, msg

    ok, msg = asyncio.run(run())
    assert ok is True
    posted = state.posted[0]
    # The fake recorded the multipart fields + file names.
    assert posted["to"] == "W1AW"
    assert posted["subject"] == "Files"
    assert posted["files"] == ["note.txt"]
    # The sent attachment name is reflected on the message for the UI.
    assert msg.metadata["attachments"] == ["note.txt"]


def test_save_attachments_downloads_inbound_files(fake_pat, tmp_path):
    state, url = fake_pat
    state.inbox.append({
        "MID": "MAIL1",
        "From": {"Addr": "W1AW"},
        "To": [{"Addr": "N0CALL"}],
        "Subject": "doc",
        "Files": [{"Name": "report.pdf"}],
    })
    state.bodies["MAIL1"] = "body"
    state.att_bytes[("MAIL1", "report.pdf")] = b"%PDF-1.4 fake"

    async def run():
        t = _transport(url)
        await t.start()
        saved = await t.save_attachments("MAIL1", str(tmp_path / "dl"))
        await t.stop()
        return saved

    saved = asyncio.run(run())
    assert len(saved) == 1
    assert saved[0].endswith("report.pdf")
    with open(saved[0], "rb") as fh:
        assert fh.read() == b"%PDF-1.4 fake"


# -- delivery reconciliation (queued -> delivered) ----------------------------

def test_delivery_receipt_emitted_after_session_forwards_outbox(fake_pat):
    state, url = fake_pat

    async def run():
        t = _transport(url, connect="telnet")
        received = _capture(t)
        await t.start()
        await asyncio.sleep(0.1)  # prime inbox
        msg = UnifiedMessage.direct("N0CALL", "W1AW", "hello")
        await t.send(msg)          # queued in outbox, correlated
        await t.connect_now()      # forwards outbox -> emits a delivery receipt
        await t.stop()
        return received, msg.msg_id

    received, sent_id = asyncio.run(run())
    receipts = [m for m in received if m.metadata.get("kind") == "delivery"]
    assert len(receipts) == 1
    assert receipts[0].metadata["ref_msg_id"] == sent_id
    assert receipts[0].metadata["status"] == "delivered"
    assert receipts[0].metadata["recipient"] == "W1AW"


def test_no_delivery_receipt_when_message_stays_queued(fake_pat):
    state, url = fake_pat
    state.forward_on_connect = False  # session connects but forwards nothing

    async def run():
        t = _transport(url, connect="telnet")
        received = _capture(t)
        await t.start()
        await asyncio.sleep(0.1)
        await t.send(UnifiedMessage.direct("N0CALL", "W1AW", "hello"))
        await t.connect_now()
        await t.stop()
        return received

    received = asyncio.run(run())
    assert [m for m in received if m.metadata.get("kind") == "delivery"] == []


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


# -- config validation (no network) -------------------------------------------

def test_validate_config_clean_when_sensible():
    t = _transport("http://127.0.0.1:8080", connect="auto")
    assert t.validate_config() == []


def test_validate_config_flags_unknown_method():
    t = _transport("http://127.0.0.1:8080", connect="bogus")
    warnings = t.validate_config()
    assert any("not a known method" in w for w in warnings)


def test_validate_config_flags_unknown_connect_order_entry():
    t = _transport(
        "http://127.0.0.1:8080", connect="auto",
        connect_order=["telnet", "nope"],
    )
    assert any("unknown method(s): nope" in w for w in t.validate_config())


def test_validate_config_flags_malformed_url_and_empty_callsign():
    t = WinlinkTransport({"pat_url": "not-a-url", "callsign": ""})
    warnings = t.validate_config()
    assert any("pat_url looks malformed" in w for w in warnings)
    assert any("callsign is empty" in w for w in warnings)


def test_validate_config_flags_bad_port():
    t = _transport("http://127.0.0.1:8080", vara_port="not-a-number")
    assert any("vara_port is not a valid TCP port" in w for w in t.validate_config())


# -- Winlink standard forms ---------------------------------------------------

def test_list_forms_flattens_catalog(fake_pat):
    state, url = fake_pat
    forms = asyncio.run(_transport(url).list_forms())
    # Two forms, each tagged with its folder; sorted by folder then name.
    paths = [(f["folder"], f["name"], f["path"]) for f in forms]
    assert ("ICS", "ICS213", "ICS/ICS213.txt") in paths
    assert ("Welfare", "Radiogram", "Welfare/Rgram.txt") in paths
    assert paths == sorted(paths)


def test_list_forms_empty_when_none_installed(fake_pat):
    state, url = fake_pat
    state.forms_catalog = {"name": "Standard Forms", "forms": [], "folders": []}
    assert asyncio.run(_transport(url).list_forms()) == []


def test_update_forms_reports_version_and_action(fake_pat):
    state, url = fake_pat
    result = asyncio.run(_transport(url).update_forms())
    assert result == {"version": "1.2.3.4", "action": "update"}


def test_get_form_template_returns_text(fake_pat):
    state, url = fake_pat
    text = asyncio.run(_transport(url).get_form_template("ICS/ICS213.txt"))
    assert "Preview" in text and "over winlink" in text


def test_compose_form_builds_and_queues_with_attachment(fake_pat):
    state, url = fake_pat

    async def run():
        t = _transport(url)
        await t.start()
        built = await t.compose_form(
            "ICS/ICS213.txt", {"city": "Boston"}, to="W1AW"
        )
        await t.stop()
        return built

    built = asyncio.run(run())
    assert built is not None
    assert built["to"] == "W1AW"               # override applied
    assert built["subject"] == "ICS213 report"  # from the built form
    assert built["body"] == "City: Boston"      # responses fed through
    # The message was queued in the outbox WITH the form XML attachment, proving
    # the forminstance cookie correlated the server-side attachment.
    assert len(state.outbox) == 1
    files = state.outbox[0]["Files"]
    assert files and files[0]["Name"] == "RMS_Express_Form_ICS213.xml"
    assert state.posted[-1]["to"] == "W1AW"


def test_compose_form_returns_none_when_not_running(fake_pat):
    state, url = fake_pat
    built = asyncio.run(_transport(url).compose_form("ICS/ICS213.txt"))
    assert built is None
    assert state.outbox == []



