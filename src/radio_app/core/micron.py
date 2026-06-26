"""Minimal, read-only micron renderer for viewing NomadNet pages.

Micron is NomadNet's lightweight markup (``.mu``). This module converts a
*subset* of it — everything needed to *read* a page — into Textual console
markup (for the TUI) and plain text (for the CLI). It deliberately implements
**no** input fields, forms or submission: links may still carry literal
``var=value`` request data (which is what lets us read *dynamic* pages that
branch on request variables), but on-page input widgets are skipped.

Supported:
* sections / headings via leading ``>`` (and de-indent ``<``)
* dividers (a line beginning with ``-``)
* comments (``#`` at start of line)
* inline styles: bold ```!``, italic ```*``, underline ```_``
* colours: ```Frgb`` / ```f`` (foreground), ```Brgb`` / ```b`` (background)
* reset-all (two backticks) and escaped literal backtick (``\\```)
* links ``\u0060[label\u0060url\u0060field1=val|field2=val]`` -> numbered, navigable

Not supported (read-only by design): ```<...>`` input fields are skipped,
alignment hints are consumed, literal/pre blocks pass through unstyled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Default page requested when an address omits an explicit path.
DEFAULT_PAGE = "/page/index.mu"

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


@dataclass
class MicronLink:
    """A navigable link parsed from a page (read-only: no input fields)."""

    path: str
    dest: str | None = None          # destination hash hex; None = same node
    fields: dict[str, str] = field(default_factory=dict)
    label: str = ""

    def resolve_dest(self, base_dest: str | None) -> str | None:
        """Resolve the link's destination against the current page's node.

        Micron link URLs frequently identify the destination by a **short hex
        prefix** rather than the full 32-char hash (it's shorter for the page
        author to type, and unambiguous on the author's own network). When we
        try to dial that prefix globally we can hit "ambiguous prefix" errors
        as soon as two announced nodes happen to share those leading hex
        digits.

        Rule of thumb that matches author intent: if the link's ``dest`` is a
        prefix of the current page's full ``base_dest`` (case-insensitive, hex
        only), treat the link as self-referential and dial the base node. This
        covers the common case of pages linking to their own siblings via a
        short alias. Falls back to ``self.dest or base_dest`` otherwise.
        """
        if not self.dest:
            return base_dest
        if not base_dest:
            return self.dest
        d = self.dest.strip().lower()
        b = base_dest.strip().lower()
        if d and b and len(d) < len(b) and b.startswith(d) and all(
            c in "0123456789abcdef" for c in d
        ):
            return base_dest
        return self.dest


@dataclass
class RenderedPage:
    markup: str                       # Textual console markup
    plain: str                        # plain text (CLI)
    links: list[MicronLink]


# -- colour helpers ----------------------------------------------------------


def _expand_colour(triplet: str) -> str | None:
    """Expand a 3-hex-digit micron colour (e.g. ``f80``) to ``#rrggbb``."""
    if len(triplet) != 3 or not _HEX_RE.match(triplet):
        return None
    return "#" + "".join(f"{int(c, 16) * 17:02x}" for c in triplet)


def _escape(text: str) -> str:
    """Escape Textual markup control characters in literal text."""
    return text.replace("\\", "\\\\").replace("[", "\\[")


def _styled(text: str, state: dict) -> str:
    if not text:
        return ""
    body = _escape(text)
    tags: list[str] = []
    if state["bold"]:
        tags.append("b")
    if state["italic"]:
        tags.append("i")
    if state["underline"]:
        tags.append("u")
    if state["fg"]:
        tags.append(state["fg"])
    if state["bg"]:
        tags.append(f"on {state['bg']}")
    if not tags:
        return body
    return f"[{' '.join(tags)}]{body}[/]"


# -- link parsing ------------------------------------------------------------


def _parse_url(url: str, base_dest: str | None) -> tuple[str | None, str]:
    """Split a micron link URL into (dest_hex|None, path)."""
    if ":" in url:
        dh, path = url.split(":", 1)
        dest = dh.strip() or base_dest
    elif url.startswith("/"):
        dest, path = base_dest, url
    else:
        # bare name -> a page on the same node
        dest, path = base_dest, "/page/" + url
    return dest, (path or DEFAULT_PAGE)


def _parse_fields(raw: str) -> dict[str, str]:
    """Parse ``a=1|b=2`` link request data. Bare names (input fields) skipped."""
    fields: dict[str, str] = {}
    for part in raw.split("|"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            fields[key] = value
    return fields


def make_link(inner: str, base_dest: str | None) -> MicronLink:
    """Build a :class:`MicronLink` from the inside of ``\u0060[...]``."""
    parts = inner.split("`")
    label = parts[0]
    url = parts[1] if len(parts) > 1 and parts[1] else label
    fields = _parse_fields(parts[2]) if len(parts) > 2 else {}
    dest, path = _parse_url(url, base_dest)
    return MicronLink(path=path, dest=dest, fields=fields, label=label)


def parse_address(
    spec: str, base_dest: str | None = None
) -> tuple[str | None, str, dict[str, str]]:
    """Parse a user/CLI address ``<hash>[:/page/x.mu]`` -> (dest, path, fields).

    Accepts ``<hash>``, ``<hash>:/page/x.mu``, ``:/page/x.mu`` (same node) and a
    bare ``/page/x.mu`` path. Request fields can be appended as ``|a=1|b=2``.
    """
    spec = spec.strip()
    fields: dict[str, str] = {}
    if "|" in spec:
        spec, raw = spec.split("|", 1)
        fields = _parse_fields(raw)
        spec = spec.strip()
    if ":" in spec:
        dh, path = spec.split(":", 1)
        dest = dh.strip() or base_dest
    elif "/" in spec:
        dest = base_dest
        path = spec if spec.startswith("/") else "/" + spec
    else:
        dest = spec or base_dest
        path = DEFAULT_PAGE
    return dest, (path or DEFAULT_PAGE), fields


# -- inline parser -----------------------------------------------------------


def _parse_inline(
    s: str, state: dict, links: list[MicronLink], base_dest: str | None
) -> tuple[str, str]:
    mk: list[str] = []
    pl: list[str] = []
    buf: list[str] = []
    i, n = 0, len(s)

    def flush() -> None:
        if buf:
            text = "".join(buf)
            pl.append(text)
            mk.append(_styled(text, state))
            buf.clear()

    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            buf.append(s[i + 1])
            i += 2
            continue
        if c != "`":
            buf.append(c)
            i += 1
            continue
        # control sequence
        nxt = s[i + 1] if i + 1 < n else ""
        if nxt == "`":  # reset all
            flush()
            state.update(bold=False, italic=False, underline=False, fg=None, bg=None)
            i += 2
        elif nxt == "!":
            flush()
            state["bold"] = not state["bold"]
            i += 2
        elif nxt == "*":
            flush()
            state["italic"] = not state["italic"]
            i += 2
        elif nxt == "_":
            flush()
            state["underline"] = not state["underline"]
            i += 2
        elif nxt == "F":
            flush()
            state["fg"] = _expand_colour(s[i + 2 : i + 5])
            i += 5
        elif nxt == "f":
            flush()
            state["fg"] = None
            i += 2
        elif nxt == "B":
            flush()
            state["bg"] = _expand_colour(s[i + 2 : i + 5])
            i += 5
        elif nxt == "b":
            flush()
            state["bg"] = None
            i += 2
        elif nxt == "[":
            end = s.find("]", i + 2)
            if end == -1:
                buf.append(s[i:])
                break
            flush()
            link = make_link(s[i + 2 : end], base_dest)
            links.append(link)
            idx = len(links)
            label = link.label or link.path
            mk.append(f"[u #00afff]{_escape(label)}[/][#5f8700]\\[{idx}][/]")
            pl.append(f"{label}[{idx}]")
            i = end + 1
        elif nxt == "<":  # input field - skipped (read-only)
            end = s.find(">", i + 2)
            i = (end + 1) if end != -1 else (i + 2)
        elif nxt in ("c", "l", "r", "a", "="):  # alignment / literal - consume
            i += 2
        else:  # unknown code - drop the marker
            i += 2
    flush()
    return "".join(mk), "".join(pl)


# -- public renderer ---------------------------------------------------------


def render_micron(
    text: str, *, base_dest: str | None = None, width: int = 60
) -> RenderedPage:
    """Render micron ``text`` to Textual markup + plain text + link list."""
    links: list[MicronLink] = []
    mk_lines: list[str] = []
    pl_lines: list[str] = []
    state = {"bold": False, "italic": False, "underline": False, "fg": None, "bg": None}
    literal = False

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw

        if line.strip() == "`=":  # literal/pre block toggle
            literal = not literal
            continue
        if literal:
            mk_lines.append(_escape(line))
            pl_lines.append(line)
            continue
        if line.startswith("#"):  # comment
            continue

        # leading section markers control indent depth
        depth = 0
        heading = False
        while line.startswith(">"):
            depth += 1
            heading = True
            line = line[1:]
        while line.startswith("<"):
            depth = max(0, depth - 1)
            line = line[1:]
        indent = "  " * depth

        # divider: a line beginning with '-' (optional fill char after it)
        stripped = line.strip()
        if stripped and set(stripped) == {"-"}:
            mk_lines.append("[#5f5f5f]" + "\u2500" * width + "[/]")
            pl_lines.append("\u2500" * width)
            continue

        mk, pl = _parse_inline(line, state, links, base_dest)
        if heading:
            mk = f"[b #ffd700]{mk}[/]"
        mk_lines.append(indent + mk)
        pl_lines.append(indent + pl)

    return RenderedPage(
        markup="\n".join(mk_lines),
        plain="\n".join(pl_lines),
        links=links,
    )

