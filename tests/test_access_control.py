"""allowed_clients / allowed_hosts / allowed_ports: parsing, matching and enforcement."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from gude_exporter import __main__ as m


def _cfg(tmp_path, body=""):
    p = tmp_path / "c.yaml"
    p.write_text(body)
    return m.load_config(str(p))


# ---- config parsing ------------------------------------------------------


def test_defaults_are_open(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg["allowed_clients"] == [] and cfg["allowed_hosts"] == []
    assert cfg["allowed_ports"] == [80, 443]


def test_allowed_clients_from_file(tmp_path):
    cfg = _cfg(tmp_path, "allowed_clients:\n  - 10.1.2.3\n  - 10.1.0.0/24\n  - '2001:db8::/32'\n")
    nets = [str(n) for n in cfg["allowed_clients"]]
    assert nets == ["10.1.2.3/32", "10.1.0.0/24", "2001:db8::/32"]


def test_allowed_clients_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GUDE_ALLOWED_CLIENTS", "192.0.2.1, 198.51.100.0/24 ,")
    cfg = _cfg(tmp_path, "allowed_clients: [10.0.0.1]\n")
    assert [str(n) for n in cfg["allowed_clients"]] == ["192.0.2.1/32", "198.51.100.0/24"]


def test_bad_allowed_clients_entry_fails_at_startup(tmp_path):
    with pytest.raises(ValueError, match="allowed_clients"):
        _cfg(tmp_path, "allowed_clients: [not-an-ip]\n")


def test_bad_allowed_hosts_regex_fails_at_startup(tmp_path):
    with pytest.raises(ValueError, match="allowed_hosts"):
        _cfg(tmp_path, "allowed_hosts: ['(unclosed']\n")


def test_allowed_ports_coerced_to_int(tmp_path):
    assert _cfg(tmp_path, "allowed_ports: ['8080', 443]\n")["allowed_ports"] == [8080, 443]


def test_example_config_still_loads():
    assert m.load_config("config.example.yaml")["listen_port"] == 9880


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("pwr1.ugent.be", True),
        ("pwr-rack3.private.ugent.be", True),
        ("pwr0409001.private.ugent.be", True),
        ("pwr.ugent.be", True),
        ("pwr1.ugent.be.evil.com", False),
        ("xpwr1.ugent.be", False),
        ("pwr1.other.ugent.be", False),
        ("pwr1.evil.be", False),
        ("matrix1.ugent.be", False),
        ("10.0.0.1", False),
        ("pwr1.ugent.be/x", False),
    ],
)
def test_shipped_example_pattern(host, ok):
    cfg = m.load_config("config.example.yaml")
    assert cfg["allowed_hosts"], "example config should ship an allowed_hosts default"
    assert m._target_allowed(cfg, _dev(host)) is ok


# ---- matching ------------------------------------------------------------


def test_client_allowed(tmp_path):
    cfg = _cfg(tmp_path, "allowed_clients: [10.1.0.0/24, '2001:db8::/32']\n")
    assert m._client_allowed(cfg, "10.1.0.77")
    assert m._client_allowed(cfg, "::ffff:10.1.0.77")  # dual-stack mapped IPv4
    assert m._client_allowed(cfg, "2001:db8::1")
    assert m._client_allowed(cfg, "fe80::1%eth0") is False
    assert not m._client_allowed(cfg, "10.2.0.1")
    assert not m._client_allowed(cfg, "garbage")


def test_client_allowed_when_unrestricted(tmp_path):
    assert m._client_allowed(_cfg(tmp_path), "203.0.113.9")


def _dev(host, port=None, scheme="http"):
    return {"host": host, "port": port, "scheme": scheme}


def test_target_allowed(tmp_path):
    cfg = _cfg(tmp_path, "allowed_hosts: ['pdu\\d+\\.example\\.org']\n")
    assert m._target_allowed(cfg, _dev("pdu12.example.org"))  # implicit port 80
    assert m._target_allowed(cfg, _dev("pdu12.example.org", scheme="https"))  # implicit 443
    assert not m._target_allowed(cfg, _dev("pdu12.example.org", 22))
    assert not m._target_allowed(cfg, _dev("pdu12.example.org.evil.com"))  # fullmatch, no prefix
    assert not m._target_allowed(cfg, _dev("evilpdu12.example.org"))
    assert not m._target_allowed(cfg, _dev("169.254.169.254"))


def test_target_ports_configurable(tmp_path):
    cfg = _cfg(tmp_path, "allowed_hosts: ['h']\nallowed_ports: [8080]\n")
    assert m._target_allowed(cfg, _dev("h", 8080))
    assert not m._target_allowed(cfg, _dev("h"))  # implicit 80 no longer allowed


def test_target_allowed_when_unrestricted(tmp_path):
    assert m._target_allowed(_cfg(tmp_path), _dev("anything", 1234))


# ---- enforcement over real HTTP -----------------------------------------


@pytest.fixture
def serve(monkeypatch):
    calls = []

    def fake_creds(host):
        calls.append(host)

    monkeypatch.setattr(m.bao, "get_credentials", fake_creds)
    servers = []

    def start(tmp_path, body):
        cfg = _cfg(tmp_path, body)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), m._make_handler(cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    start.bao_calls = calls
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _code(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - local test server
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("path", ["/", "/metrics", "/probe?host=127.0.0.1&port=1&timeout=1"])
def test_client_outside_allowlist_gets_403(serve, tmp_path, path):
    base = serve(tmp_path, "allowed_clients: [203.0.113.0/24]\n")
    assert _code(base + path) == 403
    assert serve.bao_calls == []  # rejected before any credential lookup


def test_healthz_open_to_any_client(serve, tmp_path):
    base = serve(tmp_path, "allowed_clients: [203.0.113.0/24]\n")
    assert _code(base + "/healthz") == 200


def test_client_inside_allowlist_ok(serve, tmp_path):
    base = serve(tmp_path, "allowed_clients: [127.0.0.0/8]\n")
    assert _code(base + "/metrics") == 200


def test_url_mode_target_outside_allowlist_gets_403_without_credential_lookup(serve, tmp_path):
    base = serve(tmp_path, "allowed_hosts: ['pdu\\d+\\.example\\.org']\n")
    assert _code(base + "/probe?host=evil.example.com") == 403
    assert _code(base + "/probe?host=pdu1.example.org&port=22") == 403
    assert _code(base + "/probe?host=pdu1.example.org.evil.com") == 403
    assert serve.bao_calls == []  # OpenBao credentials never looked up for rejected targets


def test_url_mode_allowed_target_proceeds(serve, tmp_path):
    base = serve(tmp_path, "allowed_hosts: ['127\\.0\\.0\\.1']\nallowed_ports: [1]\n")
    # port 1 is closed -> probe fails but the request itself is accepted (200, probe_success 0)
    assert _code(base + "/probe?host=127.0.0.1&port=1&timeout=1") == 200
    assert serve.bao_calls == ["127.0.0.1"]


def test_config_devices_bypass_target_restriction(serve, tmp_path):
    body = (
        "allowed_hosts: ['only\\.this\\.host']\n"
        "devices:\n  - name: d\n    host: 127.0.0.1\n    port: 1\n    timeout: 1\n"
    )
    base = serve(tmp_path, body)
    assert _code(base + "/probe") == 200
