"""Single-file configuration.

All user configuration lives in one TOML file. It is plain text (hand-editable)
and is the exact file a future GUI reads and writes. This module loads it, exposes
typed access, and can write it back atomically so the GUI and the text file never
diverge.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-reuse-stubs]
from typing import Any

try:
    import tomli_w
except ModuleNotFoundError:  # pragma: no cover - writing is optional
    tomli_w = None  # type: ignore[assignment]

_ENV_VAR = "RADIO_APP_CONFIG"
_DEFAULT_DIRNAME = "radio_app"
_DEFAULT_FILENAME = "config.toml"


def default_config_path() -> Path:
    """Resolve the config path: env override, else XDG-style user config dir."""
    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / _DEFAULT_DIRNAME / _DEFAULT_FILENAME


_DEFAULTS: dict[str, Any] = {
    "general": {
        "display_name": "Anonymous",
        "default_mode": "auto",
        "history_retention_days": 0,
    },
    "station": {
        # Amateur-radio operator identity. REQUIRED for HF transports (you must
        # identify with your callsign on the air); DELIBERATELY withheld from the
        # Reticulum transport, which uses an anonymous cryptographic identity.
        # The radio itself is driven by the transport app (e.g. JS8Call), so no
        # rig/CAT configuration lives here.
        "callsign": "",
        "grid_square": "",
    },
    "compliance": {
        # Amateur regulations generally PROHIBIT transmitting messages encrypted to
        # obscure their meaning on the air. The app refuses to send encrypted
        # payloads over HF transports unless this is explicitly enabled AND the
        # user confirms at send time. Leave false unless you are certain it is
        # lawful in your jurisdiction and service.
        "allow_encrypted_on_hf": False,
    },
    "storage": {
        "database": "conversations.db",
        # Single directory where ALL downloadable content is saved locally
        # (Winlink attachments, Reticulum/LXMF file attachments, etc.). Asked
        # for during first-run setup and used by every mode. Empty = default
        # XDG data dir (…/radio_app/downloads).
        "download_dir": "",
    },
    "transports": {},
    "groups": {},
    "subscriptions": {"groups": [], "show_unsubscribed": False},
    "filters": [],
    "templates": {},
    "position": {},
    "power": {
        "warn_threshold": 20,  # percent; show red below this level
    },
    "ui": {
        # Textual theme/palette selected from the command palette, persisted here.
        "theme": "",
        # Landing surface shown at startup. One of: "" (first configured mode),
        # a transport name (e.g. "meshcore"/"js8call"/"reticulum"), "nomadnet",
        # "watch", "health", or "favorites".
        "home": "",
        # Max number of rows the Watch live feed retains in memory. The feed is
        # NOT persisted history (that lives in the database); this is just the
        # in-memory scrollback, bounded so a long session on a busy band can't
        # grow without limit. 0 = unbounded (not recommended on small devices).
        "watch_buffer_limit": 1000,
    },
}


class Config:
    """Loaded configuration with typed accessors and write-back support."""

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self._data = data
        self.path = path

    # -- loading / saving -----------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        resolved = Path(path).expanduser() if path else default_config_path()
        data = _deep_merge(_DEFAULTS, {})
        if resolved.exists():
            with resolved.open("rb") as fh:
                data = _deep_merge(_DEFAULTS, tomllib.load(fh))
        return cls(data, resolved)

    def save(self) -> None:
        """Write the config back to disk atomically (used by the GUI)."""
        if tomli_w is None:
            raise RuntimeError(
                "Writing config requires 'tomli-w' (pip install tomli-w)."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            tomli_w.dump(self._data, fh)
        tmp.replace(self.path)

    # -- raw access -----------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        self._data.setdefault(section, {})[key] = value

    # -- typed views ----------------------------------------------------------

    @property
    def general(self) -> dict[str, Any]:
        return self._data.get("general", {})

    @property
    def station(self) -> dict[str, Any]:
        return self._data.get("station", {})


    @property
    def compliance(self) -> dict[str, Any]:
        return self._data.get("compliance", {})

    @property
    def storage(self) -> dict[str, Any]:
        return self._data.get("storage", {})

    @property
    def transports(self) -> dict[str, dict[str, Any]]:
        return self._data.get("transports", {})

    @property
    def groups(self) -> dict[str, dict[str, Any]]:
        return self._data.get("groups", {})

    @property
    def subscriptions(self) -> dict[str, Any]:
        return self._data.get("subscriptions", {})

    @property
    def filters(self) -> list[dict[str, Any]]:
        return self._data.get("filters", [])

    @property
    def ui(self) -> dict[str, Any]:
        return self._data.get("ui", {})

    @property
    def templates(self) -> dict[str, str]:
        raw = self._data.get("templates", {})
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    @property
    def position(self) -> dict:
        return self._data.get("position", {})

    @property
    def display_name(self) -> str:
        return self.general.get("display_name", "Anonymous")

    @property
    def default_mode(self) -> str:
        return self.general.get("default_mode", "auto")

    def database_path(self) -> Path:
        db = self.storage.get("database", "conversations.db")
        db_path = Path(db).expanduser()
        if db_path.is_absolute():
            return db_path
        return self.path.parent / db_path

    def download_dir(self) -> Path:
        """Central directory for ALL downloaded content, used by every mode.

        Resolves ``[storage].download_dir`` (asked during setup): expands ``~``,
        treats a relative path as relative to the config dir, and falls back to
        the XDG data home (``…/radio_app/downloads``) when unset. This is the one
        place every transport saves attachments/files, so downloads from any mode
        land together.
        """
        configured = str(self.storage.get("download_dir", "") or "").strip()
        if configured:
            p = Path(os.path.expandvars(configured)).expanduser()
            return p if p.is_absolute() else (self.path.parent / p)
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
        return root / _DEFAULT_DIRNAME / "downloads"

    def enabled_transports(self) -> dict[str, dict[str, Any]]:
        """Return only transports whose block has ``enabled = true``."""
        return {
            name: cfg
            for name, cfg in self.transports.items()
            if cfg.get("enabled", False)
        }

    def is_station_configured(self) -> bool:
        """True once the operator has provided a callsign."""
        return bool(self.station.get("callsign", "").strip())

    def allow_encrypted_on_hf(self) -> bool:
        return bool(self.compliance.get("allow_encrypted_on_hf", False))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto a copy of ``base``."""
    result = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

