"""load_config, query-param device building and credential resolution."""

from __future__ import annotations

import pytest

from gude_exporter import __main__ as m


def _write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return str(p)


def test_defaults_when_file_empty(tmp_path):
    cfg = m.load_config(_write(tmp_path, ""))
    assert cfg["listen_port"] == 9880 and cfg["devices"] == [] and cfg["log_level"] == "INFO"


def test_device_defaults_applied(tmp_path):
    cfg = m.load_config(_write(tmp_path, "devices:\n  - name: a\n    host: h\n"))
    d = cfg["devices"][0]
    assert d["scheme"] == "http" and d["port"] is None and d["timeout"] == 10.0
    assert d["verify_tls"] is False


@pytest.mark.parametrize("body", ["devices:\n  - host: h\n", "devices:\n  - name: a\n"])
def test_missing_required_device_fields(tmp_path, body):
    with pytest.raises(ValueError):
        m.load_config(_write(tmp_path, body))


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        m.load_config("/nonexistent/config.yaml")


def test_example_config_loads():
    cfg = m.load_config("config.example.yaml")
    assert cfg["listen_port"] == 9880


def test_device_from_query_params_defaults():
    d = m._device_from_query_params({"host": ["h"]})
    assert d["name"] == "h" and d["port"] is None and d["scheme"] == "http"
    assert d["timeout"] == 10.0 and d["verify_tls"] is False


def test_device_from_query_params_full():
    d = m._device_from_query_params(
        {"host": ["h"], "name": ["n"], "port": ["8080"], "scheme": ["https"],
         "timeout": ["2.5"], "username": ["u"], "password": ["p"], "verify_tls": [" TRUE "]}
    )
    assert (d["name"], d["port"], d["scheme"], d["timeout"]) == ("n", 8080, "https", 2.5)
    assert d["verify_tls"] is True and d["username"] == "u" and d["password"] == "p"


@pytest.mark.parametrize("bad", [{"port": ["abc"]}, {"timeout": ["x"]}])
def test_device_from_query_params_bad_numbers(bad):
    with pytest.raises(ValueError):
        m._device_from_query_params({"host": ["h"], **bad})


def test_resolve_credentials_prefers_openbao(monkeypatch):
    monkeypatch.setattr(m.bao, "get_credentials", lambda host: ("bu", "bp"))
    orig = {"name": "n", "host": "h", "username": "u", "password": "p"}
    (out,) = m._resolve_credentials([orig])
    assert (out["username"], out["password"]) == ("bu", "bp")
    assert orig["username"] == "u"  # input not mutated


def test_resolve_credentials_falls_back(monkeypatch):
    monkeypatch.setattr(m.bao, "get_credentials", lambda host: None)
    (out,) = m._resolve_credentials([{"name": "n", "host": "h", "username": "u", "password": "p"}])
    assert (out["username"], out["password"]) == ("u", "p")
