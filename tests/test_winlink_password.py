"""Per-session Winlink secure-login password handling.

Exercises the path where Pat asks for a password over its /ws WebSocket (it does
this only when no password is set in Pat's own config) and the app answers it for
that session via the ``on_prompt`` callback — never storing the secret.
"""

from __future__ import annotations

import asyncio
import json

from radio_app.transports import winlink_ws
from radio_app.transports.winlink_transport import WinlinkTransport


def _server_frame(payload: bytes) -> bytes:
    """An unmasked server->client text frame (as Pat sends)."""
    out = bytearray([0x81])  # FIN + text
    n = len(payload)
    if n < 126:
        out.append(n)
    else:
        out.append(126)
        out += n.to_bytes(2, "big")
    out += payload
    return bytes(out)


async def _prompting_ws_server(prompt: dict, received: list[dict]):
    """WS server that sends one Prompt then records the client's response."""
    async def handle(reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        key = ""
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip().decode()
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: "
            + winlink_ws.accept_key(key).encode() + b"\r\n\r\n"
        )
        await writer.drain()
        # Ask for the password.
        writer.write(_server_frame(json.dumps({"Prompt": prompt}).encode()))
        await writer.drain()
        # Read the client's prompt_response frame.
        data = await asyncio.wait_for(reader.read(4096), 3)
        frames, _ = winlink_ws.decode_frames(data)
        for _op, payload in frames:
            received.append(json.loads(payload.decode()))
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def test_stream_events_answers_password_prompt():
    prompt = {"id": "p1", "kind": "password", "message": "Enter secure login"}

    async def run():
        received: list[dict] = []
        server = await _prompting_ws_server(prompt, received)
        host, port = server.sockets[0].getsockname()
        events: list[dict] = []
        t = WinlinkTransport({"pat_url": f"http://{host}:{port}"})

        async def on_prompt(p: dict) -> str:
            assert p["kind"] == "password"
            return "s3cret"

        async with server:
            await t.stream_events(
                events.append, lambda: False, on_prompt=on_prompt
            )
        return received, events

    received, events = asyncio.run(run())
    # The password was sent back over the WS as a prompt_response...
    assert received == [{"prompt_response": {"id": "p1", "value": "s3cret"}}]
    # ...and the Prompt itself was NOT delivered as a normal UI event.
    assert events == []


def test_answer_password_prompt_declines_on_empty():
    """An empty/None password answer queues nothing (Pat falls back)."""
    async def run():
        t = WinlinkTransport({})
        outgoing: asyncio.Queue[str] = asyncio.Queue()

        async def on_prompt(_p):
            return None

        await t._answer_password_prompt(
            {"id": "p1", "kind": "password"}, on_prompt, outgoing
        )
        return outgoing.empty()

    assert asyncio.run(run()) is True

