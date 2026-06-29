"""CLI ``radioapp config get|set`` — per-item config editing."""

from __future__ import annotations

import tomllib

import pytest

from radio_app.cli import main


@pytest.fixture()
def cfg_env(tmp_path, monkeypatch):
    """Point the CLI at a fresh config file via the env override."""
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    return cfg


def _read(cfg):
    with cfg.open("rb") as fh:
        return tomllib.load(fh)


def test_config_set_string_and_get(cfg_env, capsys):
    assert main(["config", "set", "storage.download_dir", "/tmp/dls"]) == 0
    assert _read(cfg_env)["storage"]["download_dir"] == "/tmp/dls"
    capsys.readouterr()
    assert main(["config", "get", "storage.download_dir"]) == 0
    assert capsys.readouterr().out.strip() == "/tmp/dls"


def test_config_set_smart_types_bool_and_int(cfg_env):
    assert main(["config", "set", "transports.winlink.enabled", "true"]) == 0
    assert main(["config", "set", "transports.winlink.port", "8080"]) == 0
    data = _read(cfg_env)
    assert data["transports"]["winlink"]["enabled"] is True
    assert data["transports"]["winlink"]["port"] == 8080


def test_config_set_json_list(cfg_env):
    assert (
        main([
            "config", "set", "transports.winlink.connect_order",
            '["telnet", "ardop"]', "--json",
        ])
        == 0
    )
    assert _read(cfg_env)["transports"]["winlink"]["connect_order"] == [
        "telnet",
        "ardop",
    ]


def test_config_set_string_flag_forces_literal(cfg_env):
    # Without --string this would become the integer 123; --string keeps it text.
    assert main(["config", "set", "general.display_name", "123", "--string"]) == 0
    assert _read(cfg_env)["general"]["display_name"] == "123"


def test_config_set_creates_nested_tables(cfg_env):
    assert main(["config", "set", "transports.meshcore.host", "10.0.0.5"]) == 0
    assert _read(cfg_env)["transports"]["meshcore"]["host"] == "10.0.0.5"


def test_config_get_missing_key_returns_1(cfg_env, capsys):
    assert main(["config", "get", "nope.missing"]) == 1
    assert "not set" in capsys.readouterr().err


def test_config_set_requires_dotted_key(cfg_env, capsys):
    assert main(["config", "set", "loosekey", "x"]) == 2
    assert "dotted" in capsys.readouterr().err


def test_config_set_invalid_json_reports_error(cfg_env, capsys):
    assert main(["config", "set", "a.b", "[bad", "--json"]) == 2
    assert "JSON" in capsys.readouterr().err

