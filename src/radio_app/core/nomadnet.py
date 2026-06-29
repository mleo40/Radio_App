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
from dataclasses import dataclass, field
from datetime import datetime

from .nomad_cache import NomadPageCache

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
    #: True when ``content`` was served from the offline cache rather than live.
    from_cache: bool = False
    #: When a cached page was originally fetched (only set when ``from_cache``).
    fetched_at: datetime | None = None


@dataclass
class FavoritesSyncResult:
    """Outcome of :meth:`NomadnetBrowser.sync_favorites`.

    ``ok``/``failed`` count individual *pages* fetched (a node's index plus any
    followed same-node links); ``skipped`` counts node favorites that couldn't be
    attempted (e.g. the stack was offline). ``pages`` records a per-page
    ``(label, status)`` for surfacing in the CLI/TUI.
    """

    ok: int = 0
    failed: int = 0
    skipped: int = 0
    pages: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.ok + self.failed + self.skipped


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

    def __init__(self, transport=None, cache: NomadPageCache | None = None) -> None:
        # Optional ReticulumTransport: used only to gate availability. RNS state
        # is process-global once Reticulum() has been initialised, so we talk to
        # RNS.Transport / RNS.Link directly.
        self._transport = transport
        # Optional offline page cache. Successful fetches are upserted here and
        # served back when the stack is offline or a live fetch fails.
        self._cache = cache

    @property
    def available(self) -> bool:
        if not _HAVE_RNS:
            return False
        if self._transport is not None:
            return bool(self._transport.running)
        return True

    async def reachable(self) -> bool:
        """True iff a live link is actually usable right now.

        ``available`` only reflects whether the transport *thinks* it is running;
        it stays ``True`` even if rnsd dies mid-session (the transport doesn't
        proactively notice the shared instance vanished). The transport's
        ``check_reachable`` probe is the authoritative signal — when rnsd dies its
        local client interface tears down, so this flips to ``False`` and callers
        can fall straight back to cached pages instead of waiting on a doomed
        live fetch.
        """
        if not self.available:
            return False
        check = getattr(self._transport, "check_reachable", None)
        if check is None:
            return True
        try:
            status = await check()
        except Exception:  # noqa: BLE001 - a flaky probe shouldn't block browsing
            return True
        return getattr(status, "value", str(status)) == "ok"

    async def fetch(
        self,
        dest_hex: str,
        path: str = "/page/index.mu",
        *,
        field_data: dict | None = None,
        timeout: float = 20.0,
        prefer_cache: bool = False,
        cache_first: bool = False,
        allow_cache: bool = True,
    ) -> PageResult:
        """Fetch one page, with transparent offline caching.

        ``field_data`` carries request vars (dynamic pages); such pages are never
        cached or served from cache, since a snapshot of one variable combination
        would be misleading.

        Cache modes (most to least aggressive about avoiding the network):

        - ``prefer_cache`` (or Reticulum offline): serve a cached snapshot without
          touching the network; error if there is no cached copy.
        - ``cache_first``: serve a cached snapshot if one exists (instant, no
          traffic); otherwise fall through to a live fetch. This is the default
          browsing mode — it keeps round-trips off the air for pages already seen.
        - neither: always fetch live; on success cache it, and on failure
          ``allow_cache`` lets a stale snapshot stand in (flagged with its age).
        """
        cacheable = self._cache is not None and not field_data

        # Serve from cache up-front when explicitly asked or when offline.
        if cacheable and (prefer_cache or not self.available):
            hit = self._cache.get(dest_hex, path)
            if hit is not None:
                return self._cached_result(hit)
            if prefer_cache:
                return PageResult(
                    False,
                    error="no cached copy of this page",
                    dest=dest_hex,
                    path=path,
                )

        # Cache-first: a cached snapshot wins (no traffic); otherwise go live.
        if cacheable and cache_first:
            hit = self._cache.get(dest_hex, path)
            if hit is not None:
                return self._cached_result(hit)

        result = await self._fetch_live(
            dest_hex, path, field_data=field_data, timeout=timeout
        )

        if not cacheable:
            return result
        if result.ok:
            self._cache.put(result.dest or dest_hex, path, result.content)
            return result
        if allow_cache:
            hit = self._cache.get(result.dest or dest_hex, path)
            if hit is not None:
                return self._cached_result(hit)
        return result

    @staticmethod
    def _cached_result(page) -> PageResult:
        return PageResult(
            True,
            content=page.content,
            dest=page.dest,
            path=page.path,
            from_cache=True,
            fetched_at=page.fetched_at,
        )

    async def sync_favorites(
        self,
        favorites,
        *,
        path: str = "/page/index.mu",
        timeout: float = 20.0,
        follow_links: bool = False,
    ) -> FavoritesSyncResult:
        """Refresh the offline cache for every ``kind == "node"`` favorite.

        Iterates the given favorites (an iterable of objects with ``id``/``kind``
        attributes, e.g. ``Favorites.all()``), fetches each node's ``path`` live,
        and caches the result. Non-node favorites are ignored. When the stack is
        offline every node favorite is reported as ``skipped`` (we can't refresh
        without a live link). With ``follow_links`` we additionally cache the
        same-node ``/page/*.mu`` links found on the index, one level deep — off by
        default since each link is another round-trip (expensive over LoRa).
        """
        result = FavoritesSyncResult()
        if self._cache is None:
            return result
        nodes = [f for f in favorites if getattr(f, "kind", "") == "node"]
        if not self.available:
            for fav in nodes:
                result.skipped += 1
                result.pages.append((fav.id, "skipped (offline)"))
            return result
        for fav in nodes:
            res = await self._fetch_live(fav.id, path, timeout=timeout)
            label = fav.display if hasattr(fav, "display") else fav.id
            if res.ok:
                self._cache.put(res.dest or fav.id, path, res.content)
                result.ok += 1
                result.pages.append((label, "ok"))
                if follow_links:
                    await self._sync_same_node_links(res, timeout, result)
            else:
                result.failed += 1
                result.pages.append((label, res.error or "failed"))
        return result

    async def _sync_same_node_links(
        self, index: PageResult, timeout: float, result: FavoritesSyncResult
    ) -> None:
        """Cache same-node ``/page/*.mu`` links on an already-fetched index page."""
        from .micron import render_micron

        base = index.dest
        try:
            rendered = render_micron(index.content, base_dest=base)
        except Exception:  # noqa: BLE001 - a malformed page just yields no links
            return
        base_l = (base or "").lower()
        seen: set[str] = {index.path}
        for link in rendered.links:
            lpath = link.path or ""
            if not lpath.startswith("/page/") or not lpath.endswith(".mu"):
                continue
            if lpath in seen:
                continue
            dest = link.resolve_dest(base)
            if (dest or "").lower() != base_l:
                continue  # only mirror pages on the same node
            seen.add(lpath)
            sub = await self._fetch_live(dest, lpath, timeout=timeout)
            tag = f"{(dest or '')[:12]}:{lpath}"
            if sub.ok:
                self._cache.put(sub.dest or dest, lpath, sub.content)
                result.ok += 1
                result.pages.append((tag, "ok"))
            else:
                result.failed += 1
                result.pages.append((tag, sub.error or "failed"))

    async def _fetch_live(
        self,
        dest_hex: str,
        path: str = "/page/index.mu",
        *,
        field_data: dict | None = None,
        timeout: float = 20.0,
    ) -> PageResult:
        """Fetch one page over the wire. ``field_data`` carries request vars."""
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


