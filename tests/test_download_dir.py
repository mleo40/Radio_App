"""Central download directory: one location, used by all modes.

Setup asks the operator where to save downloadable content; it lives in
``[storage].download_dir`` and every transport resolves to it (unless it has its
own per-transport override).
"""

from __future__ import annotations

from pathlib import Path

from radio_app.config import Config
from radio_app.transports.reticulum_transport import ReticulumTransport


def _config(tmp_path: Path, download_dir: str | None = None) -> Config:
    cfg = Config.load(tmp_path / "config.toml")  # missing file -> defaults
    if download_dir is not None:
        cfg.set("storage", "download_dir", download_dir)
    return cfg


def test_download_dir_defaults_to_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    cfg = _config(tmp_path)
    assert cfg.download_dir() == (
        tmp_path / "home" / ".local" / "share" / "radio_app" / "downloads"
    )


def test_download_dir_honours_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _config(tmp_path)
    assert cfg.download_dir() == tmp_path / "data" / "radio_app" / "downloads"


def test_download_dir_absolute_override(tmp_path):
    target = tmp_path / "Downloads" / "radio"
    cfg = _config(tmp_path, str(target))
    assert cfg.download_dir() == target


def test_download_dir_relative_is_anchored_to_config(tmp_path):
    cfg = _config(tmp_path, "dl")
    # A relative path resolves against the config file's folder.
    assert cfg.download_dir() == cfg.path.parent / "dl"


def test_reticulum_uses_central_download_dir(tmp_path):
    central = tmp_path / "central"
    t = ReticulumTransport({})
    t.set_download_dir(str(central))
    assert t.attachments_dir() == str(central)


def test_reticulum_per_transport_override_wins(tmp_path):
    central = tmp_path / "central"
    own = tmp_path / "ret-only"
    t = ReticulumTransport({"attachments_dir": str(own)})
    t.set_download_dir(str(central))
    # An explicit per-transport override still beats the central location.
    assert t.attachments_dir() == str(own)

