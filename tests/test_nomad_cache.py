"""Tests for the offline NomadNet page cache and browser cache wiring."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from radio_app.core import nomadnet as nomadnet_mod
from radio_app.core.nomad_cache import NomadPageCache
from radio_app.core.nomadnet import NomadnetBrowser, PageResult


@pytest.fixture()
def cache(tmp_path):
    c = NomadPageCache(tmp_path / "conversations.db")
    yield c
    c.close()


# -- cache store ------------------------------------------------------------


def test_put_then_get_roundtrips(cache):
    cache.put("ab" * 16, "/page/index.mu", "# Hello")
    hit = cache.get("ab" * 16, "/page/index.mu")
    assert hit is not None
    assert hit.content == "# Hello"
    assert hit.ok is True
    assert hit.age_seconds >= 0


def test_put_upserts_latest_snapshot(cache):
    dest = "cd" * 16
    cache.put(dest, "/page/index.mu", "old")
    cache.put(dest, "/page/index.mu", "new")
    hit = cache.get(dest, "/page/index.mu")
    assert hit.content == "new"
    assert len(cache.all()) == 1


def test_get_miss_returns_none(cache):
    assert cache.get("ff" * 16, "/page/index.mu") is None


def test_get_tolerates_unique_prefix(cache):
    dest = "ab" * 16
    cache.put(dest, "/page/index.mu", "content")
    hit = cache.get(dest[:12], "/page/index.mu")
    assert hit is not None
    assert hit.content == "content"


def test_get_ambiguous_prefix_returns_none(cache):
    cache.put("abcd" + "00" * 14, "/page/index.mu", "one")
    cache.put("abce" + "11" * 14, "/page/index.mu", "two")
    # "abc" prefixes both -> ambiguous -> no match.
    assert cache.get("abc", "/page/index.mu") is None


def test_dest_stored_lowercase(cache):
    cache.put("AB" * 16, "/page/x.mu", "v")
    assert cache.get("ab" * 16, "/page/x.mu") is not None


def test_age_human_buckets(cache):
    cache.put("ab" * 16, "/p.mu", "v")
    page = cache.get("ab" * 16, "/p.mu")
    page.fetched_at = datetime.now(UTC) - timedelta(hours=3)
    assert page.age_human == "3h"
    page.fetched_at = datetime.now(UTC) - timedelta(days=2)
    assert page.age_human == "2d"


# -- browser wiring ---------------------------------------------------------


class _Transport:
    running = True


def _online_browser(cache, monkeypatch, live: PageResult):
    monkeypatch.setattr(nomadnet_mod, "_HAVE_RNS", True)
    browser = NomadnetBrowser(_Transport(), cache=cache)

    async def fake_live(
        dest_hex, path="/page/index.mu", *, field_data=None, timeout=20.0
    ):
        return live

    monkeypatch.setattr(browser, "_fetch_live", fake_live)
    return browser


def test_successful_fetch_is_cached(cache, monkeypatch):
    dest = "ab" * 16
    live = PageResult(True, content="# Live", dest=dest, path="/page/index.mu")
    browser = _online_browser(cache, monkeypatch, live)

    res = asyncio.run(browser.fetch(dest, "/page/index.mu"))
    assert res.ok and not res.from_cache
    assert cache.get(dest, "/page/index.mu").content == "# Live"


def test_failed_fetch_falls_back_to_cache(cache, monkeypatch):
    dest = "ab" * 16
    cache.put(dest, "/page/index.mu", "# Cached")
    live = PageResult(False, error="timed out", dest=dest, path="/page/index.mu")
    browser = _online_browser(cache, monkeypatch, live)

    res = asyncio.run(browser.fetch(dest, "/page/index.mu"))
    assert res.ok and res.from_cache
    assert res.content == "# Cached"
    assert res.fetched_at is not None


def test_failed_fetch_without_cache_returns_error(cache, monkeypatch):
    dest = "ab" * 16
    live = PageResult(False, error="timed out", dest=dest, path="/page/index.mu")
    browser = _online_browser(cache, monkeypatch, live)

    res = asyncio.run(browser.fetch(dest, "/page/index.mu"))
    assert not res.ok and not res.from_cache


def test_prefer_cache_skips_network(cache, monkeypatch):
    dest = "ab" * 16
    cache.put(dest, "/page/index.mu", "# Cached")
    browser = NomadnetBrowser(_Transport(), cache=cache)

    async def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("network should not be touched")

    monkeypatch.setattr(browser, "_fetch_live", boom)
    res = asyncio.run(browser.fetch(dest, "/page/index.mu", prefer_cache=True))
    assert res.ok and res.from_cache and res.content == "# Cached"


def test_prefer_cache_miss_reports_no_copy(cache, monkeypatch):
    browser = NomadnetBrowser(_Transport(), cache=cache)
    res = asyncio.run(browser.fetch("ab" * 16, "/page/index.mu", prefer_cache=True))
    assert not res.ok
    assert "no cached copy" in res.error


def test_offline_serves_cache(cache, monkeypatch):
    # _HAVE_RNS False -> available False -> offline path.
    monkeypatch.setattr(nomadnet_mod, "_HAVE_RNS", False)
    dest = "ab" * 16
    cache.put(dest, "/page/index.mu", "# Cached")
    browser = NomadnetBrowser(None, cache=cache)
    res = asyncio.run(browser.fetch(dest, "/page/index.mu"))
    assert res.ok and res.from_cache


def test_dynamic_pages_not_cached(cache, monkeypatch):
    dest = "ab" * 16
    live = PageResult(True, content="dynamic", dest=dest, path="/page/index.mu")
    browser = _online_browser(cache, monkeypatch, live)

    res = asyncio.run(
        browser.fetch(dest, "/page/index.mu", field_data={"name": "x"})
    )
    assert res.ok and not res.from_cache
    assert cache.get(dest, "/page/index.mu") is None


def test_cache_first_serves_cache_without_network(cache, monkeypatch):
    dest = "ab" * 16
    cache.put(dest, "/page/index.mu", "# Cached")
    browser = NomadnetBrowser(_Transport(), cache=cache)

    async def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("cache-first should not touch the network on a hit")

    monkeypatch.setattr(browser, "_fetch_live", boom)
    res = asyncio.run(browser.fetch(dest, "/page/index.mu", cache_first=True))
    assert res.ok and res.from_cache and res.content == "# Cached"


def test_cache_first_miss_falls_through_to_live(cache, monkeypatch):
    dest = "ab" * 16
    live = PageResult(True, content="# Live", dest=dest, path="/page/index.mu")
    browser = _online_browser(cache, monkeypatch, live)

    # No cached copy yet -> cache-first must go live, then cache the result.
    res = asyncio.run(browser.fetch(dest, "/page/index.mu", cache_first=True))
    assert res.ok and not res.from_cache and res.content == "# Live"
    assert cache.get(dest, "/page/index.mu").content == "# Live"


def test_force_live_bypasses_cache(cache, monkeypatch):
    dest = "ab" * 16
    cache.put(dest, "/page/index.mu", "# Stale")
    live = PageResult(True, content="# Fresh", dest=dest, path="/page/index.mu")
    browser = _online_browser(cache, monkeypatch, live)

    # Neither prefer_cache nor cache_first -> always fetch live (the "Get live"
    # button path), even though a cached copy exists.
    res = asyncio.run(browser.fetch(dest, "/page/index.mu"))
    assert res.ok and not res.from_cache and res.content == "# Fresh"
    assert cache.get(dest, "/page/index.mu").content == "# Fresh"


# -- sync_favorites ---------------------------------------------------------


def _browser_with_responder(cache, monkeypatch, responder):
    monkeypatch.setattr(nomadnet_mod, "_HAVE_RNS", True)
    browser = NomadnetBrowser(_Transport(), cache=cache)

    async def fake_live(dest, path="/page/index.mu", *, field_data=None, timeout=20.0):
        return responder(dest, path)

    monkeypatch.setattr(browser, "_fetch_live", fake_live)
    return browser


def _ok_responder(dest, path):
    return PageResult(True, content=f"# {dest[:4]}", dest=dest, path=path)


def test_sync_caches_node_favorites(cache, monkeypatch):
    from radio_app.core.favorites import Favorite

    favs = [Favorite(id="ab" * 16, kind="node"), Favorite(id="cd" * 16, kind="node")]
    browser = _browser_with_responder(cache, monkeypatch, _ok_responder)

    res = asyncio.run(browser.sync_favorites(favs))
    assert (res.ok, res.failed, res.skipped) == (2, 0, 0)
    assert cache.get("ab" * 16, "/page/index.mu").content == "# abab"
    assert cache.get("cd" * 16, "/page/index.mu").content == "# cdcd"


def test_sync_ignores_non_node_favorites(cache, monkeypatch):
    from radio_app.core.favorites import Favorite

    favs = [
        Favorite(id="ab" * 16, kind="node"),
        Favorite(id="KC1QKM", kind="callsign"),
        Favorite(id="cd" * 16, kind="peer"),
    ]
    browser = _browser_with_responder(cache, monkeypatch, _ok_responder)

    res = asyncio.run(browser.sync_favorites(favs))
    assert res.total == 1 and res.ok == 1  # only the node favorite


def test_sync_offline_skips_all(cache, monkeypatch):
    from radio_app.core.favorites import Favorite

    monkeypatch.setattr(nomadnet_mod, "_HAVE_RNS", False)
    browser = NomadnetBrowser(None, cache=cache)
    favs = [Favorite(id="ab" * 16, kind="node")]

    res = asyncio.run(browser.sync_favorites(favs))
    assert (res.ok, res.failed, res.skipped) == (0, 0, 1)


def test_sync_counts_failures_and_does_not_cache(cache, monkeypatch):
    from radio_app.core.favorites import Favorite

    favs = [Favorite(id="ab" * 16, kind="node")]
    browser = _browser_with_responder(
        cache,
        monkeypatch,
        lambda dest, path: PageResult(False, error="timed out", dest=dest, path=path),
    )

    res = asyncio.run(browser.sync_favorites(favs))
    assert (res.ok, res.failed) == (0, 1)
    assert cache.get("ab" * 16, "/page/index.mu") is None


def test_sync_follow_links_caches_same_node_only(cache, monkeypatch):
    from radio_app.core import micron as micron_mod
    from radio_app.core.favorites import Favorite
    from radio_app.core.micron import MicronLink, RenderedPage

    base = "ab" * 16
    pages = {
        (base, "/page/index.mu"): "INDEX",
        (base, "/page/about.mu"): "ABOUT",
    }

    def responder(dest, path):
        content = pages.get((dest, path))
        if content is None:
            return PageResult(False, error="404", dest=dest, path=path)
        return PageResult(True, content=content, dest=dest, path=path)

    browser = _browser_with_responder(cache, monkeypatch, responder)

    def fake_render(content, base_dest=None):
        if content != "INDEX":
            return RenderedPage(markup="", plain="", links=[])
        return RenderedPage(
            markup="",
            plain="",
            links=[
                MicronLink(path="/page/about.mu", dest=None),       # same node
                MicronLink(path="/page/other.mu", dest="ff" * 16),  # other node
                MicronLink(path="https://example", dest=None),      # not a page
            ],
        )

    monkeypatch.setattr(micron_mod, "render_micron", fake_render)

    res = asyncio.run(browser.sync_favorites([Favorite(id=base, kind="node")],
                                             follow_links=True))
    assert cache.get(base, "/page/index.mu").content == "INDEX"
    assert cache.get(base, "/page/about.mu").content == "ABOUT"
    # A link to a *different* node must not be mirrored.
    assert cache.get("ff" * 16, "/page/other.mu") is None
    assert res.ok == 2  # index + the same-node link


# -- reachable() ------------------------------------------------------------


class _ProbeTransport:
    """Transport whose 'running' flag and check_reachable can disagree."""

    def __init__(self, running=True, status="ok"):
        self.running = running
        self._status = status

    async def check_reachable(self):
        return self._status


def test_reachable_false_when_unavailable(cache, monkeypatch):
    monkeypatch.setattr(nomadnet_mod, "_HAVE_RNS", True)
    browser = NomadnetBrowser(_ProbeTransport(running=False), cache=cache)
    assert asyncio.run(browser.reachable()) is False


def test_reachable_uses_check_reachable_when_running(cache, monkeypatch):
    monkeypatch.setattr(nomadnet_mod, "_HAVE_RNS", True)
    # Transport thinks it is running, but the live probe says down (rnsd died).
    browser = NomadnetBrowser(
        _ProbeTransport(running=True, status="down"), cache=cache
    )
    assert asyncio.run(browser.reachable()) is False


def test_reachable_true_when_probe_ok(cache, monkeypatch):
    monkeypatch.setattr(nomadnet_mod, "_HAVE_RNS", True)
    browser = NomadnetBrowser(
        _ProbeTransport(running=True, status="ok"), cache=cache
    )
    assert asyncio.run(browser.reachable()) is True




