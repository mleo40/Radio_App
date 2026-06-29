"""Canned message templates from [templates] in config.toml.

Add entries under [templates] in your config:

    [templates]
    welfare = "Welfare check — all OK"
    net     = "Net starting now, please check in"
    qsy40   = "QSY to 40m in 5 minutes"
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


class Templates:
    """Read-only view over the [templates] config section."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = {str(k): str(v) for k, v in data.items()}

    @classmethod
    def from_config(cls, cfg: Config) -> Templates:
        raw = cfg.data.get("templates", {})
        return cls(raw if isinstance(raw, dict) else {})

    def all(self) -> dict[str, str]:
        return dict(self._data)

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def names(self) -> list[str]:
        return sorted(self._data)
