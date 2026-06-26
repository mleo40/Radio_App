"""Core package: transport-agnostic message model, router, store, groups & filters."""

from __future__ import annotations

from .compliance import ComplianceDecision, ComplianceGuard
from .filters import FilterAction, FilterEngine, FilterRule
from .groups import Group, GroupRegistry
from .message import AddressType, DeliveryStatus, UnifiedMessage
from .router import Router
from .selector import SelectionMode, TransportSelector
from .station import Station
from .store import MessageStore

__all__ = [
    "AddressType",
    "DeliveryStatus",
    "UnifiedMessage",
    "Router",
    "MessageStore",
    "Group",
    "GroupRegistry",
    "FilterEngine",
    "FilterRule",
    "FilterAction",
    "SelectionMode",
    "TransportSelector",
    "Station",
    "ComplianceGuard",
    "ComplianceDecision",
]

