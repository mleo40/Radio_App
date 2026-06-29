"""Smoke-test the Winlink adapter against a built-in fake Pat (no Pat needed).

Spins up a tiny in-process HTTP server that mimics the slice of Pat's API the
adapter uses, then drives the whole flow end to end:

    start -> send (queues in outbox) -> a message "arrives" at Pat ->
    connect (forwards the outbox AND receives the new mail)

printing what happened, including the delivery receipt (queued -> delivered) and
the received message. On your machine with a real `pat http` running, the same
WinlinkTransport talks to Pat instead of this stub.
"""

import asyncio
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from radio_app.core.message import UnifiedMessage
from radio_app.transports.winlink_transport import WinlinkTransport


class FakePat:
    def __init__(self):
        self.inbox, self.outbox, self.sent, self.bodies = [], [], [], {}
        self._seq = 0

    def next_mid(self):
        self._seq += 1
        return f"OUT{self._seq}"


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
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
                self._json({"connected": False})
            elif path == "/api/mailbox/in":
                self._json([{"MID": m["MID"], "From": m["From"], "To": m["To"],
                             "Subject": m["Subject"]} for m in state.inbox])
            elif path == "/api/mailbox/out":
                self._json([{"MID": m["MID"], "To": m["To"],
                             "Subject": m["Subject"]} for m in state.outbox])
            elif path.startswith("/api/mailbox/in/"):
                mid = path.rsplit("/", 1)[-1]
                m = next((x for x in state.inbox if x["MID"] == mid), None)
                self._json({**m, "Body": state.bodies.get(mid, "")} if m else {},
                           code=200 if m else 404)
            elif path == "/api/connect":
                state.sent.extend(state.outbox)   # forward = deliver
                state.outbox.clear()
                self._json({"NumReceived": len(state.inbox)})
            else:
                self._json({}, code=404)

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(
                int(self.headers.get("Content-Length", 0))
            ).decode()
            if self.path == "/api/mailbox/out":
                f = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                state.outbox.append({"MID": state.next_mid(),
                                     "To": [{"Addr": f.get("to", "")}],
                                     "Subject": f.get("subject", "")})
                self.send_response(201)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

    return H


async def main():
    state = FakePat()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    url = f"http://{host}:{port}"

    received = []

    async def on_receive(msg):
        received.append(msg)

    t = WinlinkTransport(
        {"pat_url": url, "callsign": "N0CALL", "connect": "telnet"}
    )
    t.on_receive(on_receive)

    await t.start()
    print("pat url        :", url)
    print("reachable      :", (await t.check_reachable()).value)
    print("config warnings:", t.validate_config() or "none")

    # 1) Send -> queues in Pat's outbox.
    await t.send(UnifiedMessage.direct(
        "N0CALL", "W1AW", "Hello over Winlink",
        metadata={"subject": "Net check-in"},
    ))
    print("\nqueued outbox  :", [m["Subject"] for m in state.outbox])

    # 2) A message "arrives" at Pat, waiting for the next session.
    state.inbox.append({"MID": "IN1", "From": {"Addr": "W1AW"},
                        "To": [{"Addr": "N0CALL"}], "Subject": "Re: Net check-in"})
    state.bodies["IN1"] = "Got you 59. 73!"

    # 3) Run a session: forwards the outbox (delivery) AND receives new mail.
    print("session got    :", await t.connect_now(), "message(s)")
    await asyncio.sleep(0.1)
    await t.stop()

    print("\n-- events delivered to the app --")
    for m in received:
        if m.metadata.get("kind") == "delivery":
            print(f"  DELIVERY  {m.metadata['ref_msg_id'][:8]}.. -> "
                  f"{m.metadata['recipient']} = {m.metadata['status']}")
        else:
            print(f"  INBOUND   {m.sender}: "
                  f"\u201c{m.metadata.get('subject')}\u201d {m.content!r}")
    server.shutdown()


asyncio.run(main())

