"""Read-only NomadNet page fetcher over Reticulum.

Fetches a page from a NomadNet node (an ``RNS.Destination`` with aspects
``nomadnetwork.node``) by opening an ``RNS.Link`` and issuing a ``request`` for
the page path. Passing ``field_data`` as the request ``data`` is what enables
reading *dynamic* pages: the node's server-side script receives those request
variables and returns micron generated from them.

This is strictly read-only: we never host a node, publish pages, or submit
on-page input forms. The only "write" on the wire is the request itself (the
path plus optional ``var=value`` data carried by a link).

RNS runs its own threads, so link/request callbacks are marshalled back onto
the caller's asyncio loop via a Future.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import RNS  # type: ignore

    _HAVE_RNS = True
except ModuleNotFoundError:
    RNS = None  # type: ignore
    _HAVE_RNS = False

# NomadNet node destination aspects.
_NODE_APP = "nomadnetwork"
_NODE_ASPECT = "node"

# RNS destination hashes are 16 bytes (32 hex chars).
_FULL_HASH_HEX = 32
_HEXSET = set("0123456789abcdef")


def _match_prefix(
    spec: str, candidates: set[str]
) -> tuple[str | None, str | None]:
    """Resolve a hex ``spec`` against full-hash ``candidates``.

    Returns ``(full_hash, error)``. A full 32-char spec is returned as-is. A
    shorter spec must uniquely prefix exactly one candidate; ambiguity or no
    match yields ``(None, error_or_None)`` (``error`` is ``None`` for "not found
    yet" so callers can keep waiting for announces).
    """
    spec = spec.strip().lower()
    if not spec or any(c not in _HEXSET for c in spec):
        return None, f"not a hex node hash: {spec or '(empty)'}"
    if len(spec) > _FULL_HASH_HEX:
        return None, "node hash too long (expected 32 hex chars)"
    if len(spec) == _FULL_HASH_HEX:
        return spec, None
    matches = {c for c in candidates if c.startswith(spec)}
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, (
            f"ambiguous prefix '{spec}' matches {len(matches)} nodes; "
            "use the full 32-char hash"
        )
    return matches.pop(), None



@dataclass
class PageResult:
    ok: bool
    content: str = ""     # decoded micron source
    error: str = ""
    dest: str = ""
    path: str = ""


def _decode(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, bytes):
        return resp.decode("utf-8", errors="replace")
    if isinstance(resp, str):
        return resp
    try:
        return bytes(resp).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return str(resp)


class NomadnetBrowser:
    """Fetches NomadNet pages over the already-running Reticulum stack."""

    def __init__(self, transport=None) -> None:
        # Optional ReticulumTransport: used only to gate availability. RNS state
        # is process-global once Reticulum() has been initialised, so we talk to
        # RNS.Transport / RNS.Link directly.
        self._transport = transport

    @property
    def available(self) -> bool:
        if not _HAVE_RNS:
            return False
        if self._transport is not None:
            return bool(self._transport.running)
        return True

    async def fetch(
        self,
        dest_hex: str,
        path: str = "/page/index.mu",
        *,
        field_data: dict | None = None,
        timeout: float = 20.0,
    ) -> PageResult:
        """Fetch one page. ``field_data`` carries request vars (dynamic pages)."""
        if not self.available:
            return PageResult(
                False,
                error="Reticulum transport is not running",
                dest=dest_hex,
                path=path,
            )

        # Accept short prefixes (e.g. copied from a node list) by resolving them
        # against discovered nodes + the RNS path table.
        full_hex, err = await self._resolve(dest_hex, timeout)
        if err:
            return PageResult(False, error=err, dest=dest_hex, path=path)
        if not full_hex:
            return PageResult(
                False,
                error=(
                    f"unknown node '{dest_hex}'. Use the full 32-char hash, or run "
                    "'radioapp nodes --wait 30' to discover it first."
                ),
                dest=dest_hex,
                path=path,
            )
        dest_hex = full_hex

        try:
            dest_hash = bytes.fromhex(dest_hex)
        except ValueError:
            return PageResult(False, error=f"invalid node hash: {dest_hex}", path=path)

        if not await self._ensure_path(dest_hash, timeout):
            return PageResult(
                False,
                error="no path to node (has it announced?)",
                dest=dest_hex,
                path=path,
            )
        identity = RNS.Identity.recall(dest_hash)
        if identity is None:
            return PageResult(
                False, error="node identity not yet known", dest=dest_hex, path=path
            )

        dest = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            _NODE_APP,
            _NODE_ASPECT,
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def resolve(result: PageResult) -> None:
            def _set() -> None:
                if not fut.done():
                    fut.set_result(result)
            loop.call_soon_threadsafe(_set)

        def on_response(receipt) -> None:
            resolve(
                PageResult(
                    True,
                    content=_decode(getattr(receipt, "response", None)),
                    dest=dest_hex,
                    path=path,
                )
            )

        def on_failed(_receipt) -> None:
            resolve(
                PageResult(
                    False,
                    error="request failed/timed out",
                    dest=dest_hex,
                    path=path,
                )
            )

        def on_established(link) -> None:
            try:
                link.request(
                    path,
                    data=field_data,
                    response_callback=on_response,
                    failed_callback=on_failed,
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001
                resolve(
                    PageResult(
                        False,
                        error=f"request error: {exc}",
                        dest=dest_hex,
                        path=path,
                    )
                )

        def on_closed(_link) -> None:
            resolve(
                PageResult(
                    False,
                    error="link closed before a response",
                    dest=dest_hex,
                    path=path,
                )
            )

        try:
            RNS.Link(
                dest,
                established_callback=on_established,
                closed_callback=on_closed,
            )
        except Exception as exc:  # noqa: BLE001
            return PageResult(
                False, error=f"could not open link: {exc}", dest=dest_hex, path=path
            )

        try:
            return await asyncio.wait_for(fut, timeout=timeout + 5)
        except TimeoutError:
            return PageResult(
                False, error="timed out waiting for page", dest=dest_hex, path=path
            )

    async def _ensure_path(self, dest_hash: bytes, timeout: float) -> bool:
        if RNS.Transport.has_path(dest_hash):
            return True
        RNS.Transport.request_path(dest_hash)
        deadline = time.time() + min(timeout, 15.0)
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if RNS.Transport.has_path(dest_hash):
                return True
        return RNS.Transport.has_path(dest_hash)

    def _candidate_hashes(self) -> set[str]:
        """Full destination hashes we know about (nodes + RNS path table)."""
        candidates: set[str] = set()
        if self._transport is not None:
            try:
                for node in self._transport.known_nodes():
                    if node.get("dest"):
                        candidates.add(node["dest"].lower())
            except Exception:  # noqa: BLE001
                pass
        if _HAVE_RNS:
            try:
                table = getattr(RNS.Transport, "destination_table", {}) or {}
                for dh in list(table.keys()):
                    try:
                        candidates.add(dh.hex().lower())
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                pass
        return candidates

    async def _resolve(
        self, spec: str, timeout: float
    ) -> tuple[str | None, str | None]:
        """Resolve a possibly-short hex prefix to a full destination hash.

        Polls briefly so a prefix can resolve as node announces arrive.
        """
        full, err = _match_prefix(spec, self._candidate_hashes())
        if full or err:
            return full, err
        # Not found yet and it's a prefix: wait for announces to populate.
        deadline = time.time() + min(timeout, 12.0)
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            full, err = _match_prefix(spec, self._candidate_hashes())
            if full or err:
                return full, err
        return None, None


