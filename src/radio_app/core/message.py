"""The transport-agnostic message model.

Everything the user sees is a :class:`UnifiedMessage`. Each transport adapter is
responsible for translating between its native wire format and this type, so the
router, store and UI never need to know which medium carried a message.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class AddressType(str, Enum):
    """How a message is addressed, independent of any transport."""

    DIRECT = "direct"        # one specific identity
    BROADCAST = "broadcast"  # everyone (e.g. JS8 @ALLCALL)
    GROUP = "group"          # a named collective (e.g. @EMS, @EMSNE)


class DeliveryStatus(str, Enum):
    """Lifecycle of an outbound (or observed inbound) message."""

    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"   # confirmed by transport/app-level ACK
    FAILED = "failed"
    RECEIVED = "received"     # inbound


@dataclass
class UnifiedMessage:
    """A single message, normalized across every transport.

    Attributes:
        sender: Logical identity of the originator (not transport-specific).
        content: The human-readable message body.
        address_type: DIRECT, BROADCAST or GROUP.
        recipient: Target identity when ``address_type`` is DIRECT.
        group: Group name (without ``@``) when ``address_type`` is GROUP.
        msg_id: Stable unique id used for de-duplication across paths.
        timestamp: UTC creation/receive time.
        transport: Name of the transport that carried the message (informational).
        status: Delivery lifecycle state.
        metadata: Transport-specific extras (SNR, hops, frequency, ...).
    """

    sender: str
    content: str
    address_type: AddressType = AddressType.DIRECT
    recipient: str | None = None
    group: str | None = None
    msg_id: str = field(default_factory=_new_id)
    timestamp: datetime = field(default_factory=_utcnow)
    transport: str = ""
    status: DeliveryStatus = DeliveryStatus.PENDING
    encrypt: bool = False  # operator intent to send an encrypted/obscured payload
    metadata: dict = field(default_factory=dict)

    # -- convenience constructors --------------------------------------------

    @classmethod
    def direct(cls, sender: str, recipient: str, content: str, **kw) -> UnifiedMessage:
        return cls(
            sender=sender,
            content=content,
            address_type=AddressType.DIRECT,
            recipient=recipient,
            **kw,
        )

    @classmethod
    def to_group(cls, sender: str, group: str, content: str, **kw) -> UnifiedMessage:
        return cls(
            sender=sender,
            content=content,
            address_type=AddressType.GROUP,
            group=group.lstrip("@"),
            **kw,
        )

    @classmethod
    def broadcast(cls, sender: str, content: str, **kw) -> UnifiedMessage:
        return cls(
            sender=sender,
            content=content,
            address_type=AddressType.BROADCAST,
            **kw,
        )

    # -- helpers --------------------------------------------------------------

    @property
    def thread_key(self) -> str:
        """Identifier of the conversation this message belongs to."""
        if self.address_type is AddressType.GROUP and self.group:
            return f"@{self.group}"
        if self.address_type is AddressType.BROADCAST:
            return "@ALLCALL"
        # Direct: the conversation is with the *other* party. For received
        # messages that's the sender; for outbound it's the recipient.
        if self.status is DeliveryStatus.RECEIVED:
            return self.sender
        return self.recipient or self.sender

    @property
    def size(self) -> int:
        """Approximate wire size of the body in bytes."""
        return len(self.content.encode("utf-8"))

    @property
    def groups(self) -> list[str]:
        """Operator-declared groups this message was aggregated into.

        Stamped by the router from the :class:`GroupRegistry` (by sender
        membership or tag). Empty unless cross-mode grouping matched.
        """
        g = self.metadata.get("groups")
        return list(g) if isinstance(g, list) else []

    # -- attachments ----------------------------------------------------------
    # Attachments ride in ``metadata`` using a single cross-transport convention
    # (shared by Winlink multipart email and Reticulum LXMF file fields):
    #   metadata["attach"]       -> outbound: local file paths to send
    #   metadata["attachments"]  -> display names carried by the message
    #   metadata["attachments_saved"] -> inbound: where received files were saved

    @property
    def attach_paths(self) -> list[str]:
        """Local file paths queued to be sent as attachments (outbound)."""
        v = self.metadata.get("attach")
        return [str(p) for p in v] if isinstance(v, list) else []

    @property
    def attachment_names(self) -> list[str]:
        """Display names of attachments carried by this message."""
        v = self.metadata.get("attachments")
        return [str(p) for p in v] if isinstance(v, list) else []

    @property
    def saved_attachments(self) -> list[str]:
        """Filesystem paths where received attachments were saved (inbound)."""
        v = self.metadata.get("attachments_saved")
        return [str(p) for p in v] if isinstance(v, list) else []

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "sender": self.sender,
            "content": self.content,
            "address_type": self.address_type.value,
            "recipient": self.recipient,
            "group": self.group,
            "timestamp": self.timestamp.isoformat(),
            "transport": self.transport,
            "status": self.status.value,
            "encrypt": self.encrypt,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> UnifiedMessage:
        return cls(
            sender=data["sender"],
            content=data["content"],
            address_type=AddressType(data.get("address_type", "direct")),
            recipient=data.get("recipient"),
            group=data.get("group"),
            msg_id=data.get("msg_id", _new_id()),
            timestamp=datetime.fromisoformat(data["timestamp"])
            if data.get("timestamp")
            else _utcnow(),
            transport=data.get("transport", ""),
            status=DeliveryStatus(data.get("status", "pending")),
            encrypt=bool(data.get("encrypt", False)),
            metadata=data.get("metadata", {}) or {},
        )
