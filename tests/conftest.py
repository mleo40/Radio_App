"""Shared pytest fixtures for the Radio_App test suite.

**Why this exists:** ``Transport.__init_subclass__`` registers *every* subclass
into the process-global ``TRANSPORT_REGISTRY`` by its ``name``. A few test
modules define throwaway transports that reuse a built-in name (e.g.
``test_groups_membership`` defines a fake ``name = "js8call"`` with
``max_message_size=0``). Because pytest imports all test modules during
collection, that fake silently *overwrites* the real ``JS8CallTransport`` in the
global registry — and the override then leaks into every later test that builds
an app (it gets the fake js8call, with wrong capabilities).

The autouse fixture below pins the built-in transport names back to their real
classes around each test, restoring whatever was there afterwards so a test's own
local override (used within that test) stays local. This keeps capability-driven
behaviour — composer limits, the radio interlock, etc. — deterministic regardless
of test execution order.
"""

from __future__ import annotations

import pytest

from radio_app.transports.base import TRANSPORT_REGISTRY
from radio_app.transports.js8call_transport import JS8CallTransport
from radio_app.transports.meshcore_transport import MeshCoreTransport
from radio_app.transports.reticulum_transport import ReticulumTransport
from radio_app.transports.winlink_transport import WinlinkTransport

_CANONICAL = {
    "js8call": JS8CallTransport,
    "winlink": WinlinkTransport,
    "reticulum": ReticulumTransport,
    "meshcore": MeshCoreTransport,
}


@pytest.fixture(autouse=True)
def _canonical_transport_registry():
    """Ensure the built-in transport names map to their real classes per test."""
    saved = {name: TRANSPORT_REGISTRY.get(name) for name in _CANONICAL}
    TRANSPORT_REGISTRY.update(_CANONICAL)
    try:
        yield
    finally:
        for name, cls in saved.items():
            if cls is None:
                TRANSPORT_REGISTRY.pop(name, None)
            else:
                TRANSPORT_REGISTRY[name] = cls

