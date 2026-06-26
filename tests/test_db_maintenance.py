"""Tests for database maintenance (stats / vacuum / prune) and the page cache."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.core.nomad_cache import NomadPageCache
from radio_app.core.store import MessageStore


def _msg(i: int) -> UnifiedMessage:
    return UnifiedMessage(
        sender=f"W{i}",
        content="hello world " * 5,
        transport="js8call",
        address_type=AddressType.DIRECT,
        recipient="me",
        msg_id=f"m{i}",
    )


@pytest.fixture()
def store(tmp_path):
    s = MessageStore(tmp_path / "conversations.db")
    yield s
    s.close()


@pytest.fixture()
def cache(tmp_path):
    c = NomadPageCache(tmp_path / "conversations.db")
    yield c
    c.close()


# -- message store ----------------------------------------------------------


def test_store_stats_counts_and_size(store):
    for i in range(3):
        store.save(_msg(i), thread_key=f"W{i}")
    s = store.stats()
    assert s["messages"] == 3
    assert s["threads"] == 3
    assert s["size_bytes"] > 0
    assert s["oldest"] and s["newest"]


def test_store_stats_empty(store):
    s = store.stats()
    assert s["messages"] == 0
    assert s["threads"] == 0
    assert s["oldest"] is None


def test_store_vacuum_runs_and_reclaims(store):
    for i in range(50):
        store.save(_msg(i), thread_key=f"W{i}")
    # Delete most, then vacuum should not error and returns bytes freed >= 0.
    for i in range(50):
        store.delete_thread(f"W{i}")
    freed = store.vacuum()
    assert isinstance(freed, int)
    assert freed >= 0
    # Store remains usable after vacuum.
    store.save(_msg(99), thread_key="W99")
    assert store.stats()["messages"] == 1


# -- nomad page cache -------------------------------------------------------


def test_cache_stats(cache):
    cache.put("ab" * 16, "/page/index.mu", "abcdef")
    cache.put("cd" * 16, "/page/x.mu", "hello")
    st = cache.stats()
    assert st["pages"] == 2
    assert st["content_bytes"] == len("abcdef") + len("hello")


def test_cache_prune_removes_old_pages(cache):
    cache.put("ab" * 16, "/page/index.mu", "new")
    cache.put("cd" * 16, "/page/old.mu", "old")
    # Backdate one page well past the prune window.
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    cache._conn.execute(
        "UPDATE nomad_pages SET fetched_at = ? WHERE dest = ?",
        (old, "cd" * 16),
    )
    cache._conn.commit()
    removed = cache.prune(30)
    assert removed == 1
    assert cache.get("ab" * 16, "/page/index.mu") is not None
    assert cache.get("cd" * 16, "/page/old.mu") is None


def test_cache_prune_zero_is_noop(cache):
    cache.put("ab" * 16, "/page/index.mu", "x")
    assert cache.prune(0) == 0
    assert cache.stats()["pages"] == 1


def test_cache_clear(cache):
    cache.put("ab" * 16, "/page/index.mu", "x")
    cache.put("cd" * 16, "/page/y.mu", "y")
    assert cache.clear() == 2
    assert cache.stats()["pages"] == 0

