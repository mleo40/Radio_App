# Feature Request Queue

Tracked, not-yet-implemented feature requests. Newest at the top.

## Interactive Winlink form composer (CLI/TUI)

**Requested:** 2026-06-26
**Status:** Queued
**Area:** `cli.py`, `ui/tui.py`, `transports/winlink_transport.py`

The Winlink-forms building blocks exist and are tested — `list_forms()`,
`update_forms()`, `get_form_template()` and `compose_form()` (which drives Pat's
browserless build → outbox flow) — but `compose_form()` has **no user entry
point**: a user can list and update forms but cannot actually fill in and send
one. Only `winlink forms` / `winlink forms-update` are wired.

**Sketch of work:**
1. CLI `radioapp winlink compose-form <template> [--field k=v ...] [--to ...]`
   (or read a JSON/TOML responses file) → calls `compose_form()` and queues it.
2. TUI: a forms picker + a generated field form (templates expose their fields via
   `get_form_template()` / the catalog) → submit calls `compose_form()`.
3. Surface the built `{to, subject, body}` for review before it hits the outbox.

## Offline NomadNet page cache + "sync favorites now"

**Requested:** 2026-06-26
**Status:** Queued
**Area:** `core/nomadnet.py`, `core/store.py` (or new `core/nomad_cache.py`),
`core/favorites.py`, `ui/tui.py`, `cli.py`

Sync NomadNet pages locally so favorite nodes are viewable offline, with a
"sync favorites now" action to pull the latest once Reticulum is back up. The
pieces already exist: `NomadnetBrowser.fetch()` returns a `PageResult` with the
decoded micron `content` (a clean seam to cache), favorites already tag NomadNet
servers via `Favorite.kind == "node"`, the TUI NomadNet surface has node lists +
an F4 favorites filter + an `f`/"Save node" binding, and the Reticulum transport
now auto-reconnects (so a sync can fire when the stack comes online).

**Sketch of work:**
1. **Page cache** — a small `nomad_pages(dest, path, content, fetched_at, ok)`
   table (PK `(dest, path)`) in the existing SQLite DB, or a dedicated
   `core/nomad_cache.py`.
2. **Wire caching into `fetch()`** — upsert `content` on every successful fetch;
   add an `allow_cache`/`prefer_cache` flag so that when offline (or a live fetch
   fails) it serves the cached page, clearly flagged with its `fetched_at` age.
3. **`sync_favorites()`** — iterate `favorites.all()` filtered to `kind == "node"`,
   fetch each node's `/page/index.mu` (optionally follow same-node `/page/*.mu`
   links one level deep), cache the results, return `(ok, failed, skipped)`.
4. **Surfaces** — TUI "Sync favorites now" button/action in the NomadNet view
   (optionally auto-run on reconnect, opt-in via config); CLI `radioapp nomad
   sync`, plus a transparent cache fallback for `browse`/the viewer when the link
   can't be opened (e.g. `--offline`).

**Caveats / scope decisions:**
- **Dynamic pages** (rendered from `field_data` `var=value`) only cache a single
  snapshot — label them as such; static `/page/*.mu` pages are the real win.
- **Link-following depth** — default to index-only (cheap); deeper mirroring
  multiplies link round-trips, expensive over LoRa, so make it opt-in.
- **Staleness** — cached views must visibly show their `fetched_at` age so a
  cached page is never mistaken for live.
- **No change-detection** — NomadNet can't signal that a page changed, so sync is
  a manual/periodic pull; "latest" means "as of the last sync."

## Past-chats history view + searchable message archive

**Requested:** 2026-06-26
**Status:** Queued
**Area:** `core/store.py`, `cli.py`, `ui/tui.py`

Give users a way to browse and search their full conversation history offline.
The foundation already exists: every message from every transport is normalized
to a `UnifiedMessage` and persisted in one SQLite table (`conversations.db` →
`messages`), with `sender, recipient, group, content, transport, status,
metadata, timestamp` indexed by `(thread_key, timestamp)`. This is a
read/query feature, not a data-model change.

**Possibilities (rough easiest → most involved):**
1. **CLI history/search** — `radioapp history [--thread <key>] [--mode <transport>]
   [--since/--until] [--limit N]` and `radioapp search "<text>" [--from <sender>]
   [--mode] [--since ...]`. Thin layer over `MessageStore`; immediately useful
   offline.
2. **Full-text search (FTS5)** — add a SQLite FTS5 virtual table mirroring
   `content` (+ sender/group), synced via triggers/on-insert, for fast ranked
   queries with `snippet()` highlighting. FTS5 ships with Python's `sqlite3` (no
   new dependency).
3. **TUI history/search surface** — a search box (`/search <text>` or a `Ctrl+F`
   palette) listing matches with mode + timestamp that jump into the thread at the
   hit; plus a cross-mode "All chats" archive view (the thread list scopes by
   transport — this is the superset) with mode/date/group/unread filters and
   lazy-loaded scroll-back for huge threads.
4. **Richer filters** — query by captured `metadata` (SNR, hops, frequency,
   delivery status), date-bucketed "jump to date", per-contact stats.
5. **Export/backup** — `radioapp export --thread <key> --format {json,txt,md,
   maildir}` and a global `export --all`.
6. **Retention/housekeeping** — optional age/size-based pruning, per-thread
   "keep forever" pins, and VACUUM so history doesn't bloat small devices.

**Suggested sequencing:** start with #1 (CLI history+search) over `MessageStore`,
add #2 (FTS5) under the same query API as volume grows, then surface it in the
TUI (#3).

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

