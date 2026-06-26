# Feature Request Queue

Tracked, not-yet-implemented feature requests. Newest at the top.

## Reticulum file transfer (attachments)

**Requested:** 2026-06-26
**Status:** Queued
**Area:** `transports/reticulum_transport.py`, `core/message.py`, `ui/tui.py`

Add the ability to send files over Reticulum, the way `/attach` already works
for Winlink. Today file attachment is hard-gated to Winlink
(`tui.py` ~L3916: `if self.active_transport == "winlink" and self._winlink_attach`)
and the Reticulum `send()` path only forwards `msg.content` (text) into the
`LXMF.LXMessage` — it never touches `metadata["attach"]`.

LXMF/RNS natively support this (LXMF `fields` + Resource-based chunked transfer),
and `capabilities()` already advertises `max_message_size=1_000_000`, so the
plumbing exists but is unwired.

**Sketch of work:**
1. Give `UnifiedMessage` a structured attachment representation (or formalize the
   existing `metadata["attach"]` convention).
2. In `ReticulumTransport.send`, read the queued paths and populate
   `lxm.fields[LXMF.FIELD_FILE_ATTACHMENTS] = [[name, data], ...]`, forcing the
   `DIRECT`/Resource method for large payloads (RNS handles chunked transfer).
3. Handle inbound `fields` attachments in the LXMF delivery callback (save +
   surface them in the UI).
4. Un-gate `/attach` in the TUI so it isn't Winlink-only — ideally drive it from a
   new `supports_attachments` capability flag instead of a hardcoded transport
   name.

**Notes:** Group/broadcast Reticulum sends are a single packet capped at 383 bytes
(`_GROUP_PAYLOAD_MAX`), so file transfer would be DIRECT-only initially.

