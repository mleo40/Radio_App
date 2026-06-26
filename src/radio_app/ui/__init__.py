"""User-interface frontends (TUI now; GUI later). All are thin layers over the core."""

from __future__ import annotations

__all__ = ["run_tui"]


def run_tui(config_path: str | None = None) -> None:
    """Lazily import and launch the Textual TUI (keeps textual an optional dep)."""
    try:
        from .tui import run
    except ModuleNotFoundError as exc:  # textual not installed
        raise SystemExit(
            "The TUI requires textual. Install it with:  pip install 'radio-app[tui]'"
        ) from exc
    run(config_path)

