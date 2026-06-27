"""Canned templates: loading from config and CLI."""
from __future__ import annotations

import pytest

from radio_app.cli import main
from radio_app.core.templates import Templates


@pytest.fixture()
def tmpl_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[storage]\ndatabase = ":memory:"\n\n'
        "[templates]\n"
        'welfare = "Welfare check — all OK"\n'
        'net = "Net starting now"\n'
        'qsy40 = "QSY to 40m in 5 minutes"\n'
    )
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    return cfg


def test_templates_load(tmpl_cfg):
    from radio_app.config import Config
    cfg = Config.load(tmpl_cfg)
    t = Templates.from_config(cfg)
    assert t.names() == ["net", "qsy40", "welfare"]
    assert t.get("welfare") == "Welfare check — all OK"
    assert t.get("missing") is None


def test_templates_all(tmpl_cfg):
    from radio_app.config import Config
    cfg = Config.load(tmpl_cfg)
    t = Templates.from_config(cfg)
    d = t.all()
    assert "net" in d and "qsy40" in d and "welfare" in d


def test_templates_empty_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[storage]\ndatabase = ":memory:"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    from radio_app.config import Config
    t = Templates.from_config(Config.load(cfg))
    assert t.names() == []
    assert t.get("anything") is None


def test_cli_templates_list(tmpl_cfg, capsys):
    assert main(["templates", "list"]) == 0
    out = capsys.readouterr().out
    assert "welfare" in out
    assert "Welfare check" in out


def test_cli_templates_list_default_action(tmpl_cfg, capsys):
    assert main(["templates"]) == 0
    assert "welfare" in capsys.readouterr().out


def test_cli_templates_list_empty(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[storage]\ndatabase = ":memory:"\n')
    monkeypatch.setenv("RADIO_APP_CONFIG", str(cfg))
    assert main(["templates"]) == 0
    assert "no templates" in capsys.readouterr().out.lower()


def test_cli_templates_send_not_found(tmpl_cfg, capsys):
    assert main(["templates", "send", "missing", "--to", "W1AW"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_templates_send_requires_target(tmpl_cfg, capsys):
    assert main(["templates", "send", "welfare"]) == 2
    assert "--to or --group" in capsys.readouterr().err
