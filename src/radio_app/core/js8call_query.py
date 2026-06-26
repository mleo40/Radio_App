"""Query a running JS8Call instance for the operator's station identity.

JS8Call already knows the operator's callsign and grid square (you set them in
JS8Call's own settings), and it drives the radio. Radio_App is a single pane of
glass over disparate transports, so rather than asking the user to re-enter what
JS8Call already has, we ask JS8Call directly over its TCP/JSON API and let the
user override if they wish.

This is a tiny, synchronous, standard-library client used by the interactive
setup wizard. It sends ``STATION.GET_CALLSIGN`` / ``STATION.GET_GRID`` and reads
the matching ``STATION.CALLSIGN`` / ``STATION.GRID`` replies, ignoring the rest
of the event stream. If JS8Call is not running or the API is disabled it simply
returns blanks.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class StationInfo:
    callsign: str = ""
    grid: str = ""

    @property
    def any_found(self) -> bool:
        return bool(self.callsign or self.grid)


def query_station(
    host: str = "127.0.0.1", port: int = 2442, timeout: float = 4.0
) -> StationInfo:
    """Ask JS8Call for the operator callsign + grid. Blanks on any failure."""
    info = StationInfo()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            for mtype in ("STATION.GET_CALLSIGN", "STATION.GET_GRID"):
                sock.sendall((json.dumps({"type": mtype, "value": ""}) + "\n").encode())

            deadline = time.time() + timeout
            buffer = b""
            while time.time() < deadline and not (info.callsign and info.grid):
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    _absorb(line, info)
    except OSError as exc:
        log.info("JS8Call station query failed on %s:%s (%s)", host, port, exc)
    return info


def _absorb(line: bytes, info: StationInfo) -> None:
    line = line.strip()
    if not line:
        return
    try:
        event = json.loads(line.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    etype = event.get("type", "")
    value = event.get("value", "")
    if etype == "STATION.CALLSIGN" and value:
        info.callsign = str(value).strip().upper()
    elif etype == "STATION.GRID" and value:
        info.grid = str(value).strip()
    elif etype == "STATION.INFO":  # some builds bundle both here
        params = event.get("params", {}) or {}
        if not info.callsign and params.get("CALLSIGN"):
            info.callsign = str(params["CALLSIGN"]).strip().upper()
        if not info.grid and params.get("GRID"):
            info.grid = str(params["GRID"]).strip()


# -- groups ------------------------------------------------------------------

_GROUP_RE = re.compile(r"@[A-Z0-9/]{2,}")

# Broadcast pseudo-groups that JS8Call uses internally - never user groups.
_NON_GROUPS = {"ALLCALL", "ALL", "JS8", "HB", "CQ"}


def query_groups(
    host: str = "127.0.0.1", port: int = 2442, timeout: float = 4.0
) -> list[str]:
    """Ask a running JS8Call for the operator's configured ``@GROUP``s.

    JS8Call keeps the groups you've joined in its own settings and surfaces them
    over the TCP/JSON API. Builds differ in which reply carries them, so we ask a
    couple of candidate queries and harvest any ``@GROUP`` tokens from the
    replies. Returns a de-duplicated, upper-cased list of *bare* names (no
    ``@``); empty on any failure or if JS8Call's API is disabled.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        name = token.lstrip("@").strip().upper()
        if name and name not in seen and name not in _NON_GROUPS:
            seen.add(name)
            found.append(name)

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            # GET_INFO carries the station's own settings on most builds; the
            # call-activity dump can mention groups too. Both are harmless reads.
            for mtype in ("STATION.GET_INFO", "RX.GET_CALL_ACTIVITY"):
                sock.sendall(
                    (json.dumps({"type": mtype, "value": ""}) + "\n").encode()
                )
            deadline = time.time() + timeout
            buffer = b""
            while time.time() < deadline:
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    for token in _groups_in_line(line):
                        _add(token)
    except OSError as exc:
        log.info("JS8Call groups query failed on %s:%s (%s)", host, port, exc)
    return found


def _groups_in_line(line: bytes) -> list[str]:
    """Pull any ``@GROUP`` tokens out of one JSON event line (pure helper).

    Returns tokens in first-seen order, de-duplicated. Names from an explicit
    field keep their original form (a list field yields bare names; a string
    field yields ``@``-prefixed tokens); free-text mentions are ``@``-prefixed.
    """
    line = line.strip()
    if not line:
        return []
    try:
        event = json.loads(line.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    params = event.get("params", {}) or {}
    tokens: list[str] = []

    def _push(tok: str) -> None:
        tok = tok.strip()
        if tok and tok not in tokens:
            tokens.append(tok)

    # Preferred: an explicit groups field (newer builds expose one of these).
    for key in ("GROUPS", "GROUP", "STATION_GROUPS"):
        val = params.get(key)
        if isinstance(val, str):
            for t in _GROUP_RE.findall(val.upper()):
                _push(t)
        elif isinstance(val, (list, tuple)):
            for v in val:
                _push(str(v))
    # Fallback: scrape any @GROUP mentions from free-text fields.
    for key in ("INFO", "TEXT", "VALUE"):
        val = params.get(key) or event.get(key.lower())
        if isinstance(val, str):
            for t in _GROUP_RE.findall(val.upper()):
                _push(t)
    return tokens



