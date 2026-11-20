"""The exporter's HTTP surface, served by the real handler on an ephemeral port."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from gude_exporter import __main__ as m


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(m.bao, "get_credentials", lambda host: None)
    servers = []

    def start(devices=()):
        cfg = {**m._DEFAULT_CONFIG, "devices": list(devices)}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), m._make_handler(cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - local test server
            return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def test_healthz(serve):
    code, body, _ = _get(serve() + "/healthz")
    assert (code, body) == (200, "OK")


def test_unknown_path_404(serve):
    assert _get(serve() + "/nope")[0] == 404


def test_metrics_are_self_metrics(serve):
    code, body, _ = _get(serve() + "/metrics")
    assert code == 200 and "gude_exporter_probe_requests_total" in body
    assert "gude_outlet_state" not in body


def test_probe_without_host_or_devices_is_400(serve):
    assert _get(serve() + "/probe")[0] == 400


def test_probe_bad_param_is_400(serve):
    assert _get(serve() + "/probe?host=h&port=abc")[0] == 400


def test_probe_url_param_mode(serve, stub_device):
    url = serve() + f"/probe?host=127.0.0.1&port={stub_device.port}&name=pdu&username=u&password=p"
    code, body, headers = _get(url)
    assert code == 200
    assert 'probe_success{device="pdu",host="127.0.0.1"} 1.0' in body
    assert "no-store" in headers["Cache-Control"]
    assert stub_device.requests[0][1]["Authorization"].startswith("Basic ")


def test_probe_config_mode(serve, stub_device):
    dev = {"name": "cfg", "host": "127.0.0.1", "port": stub_device.port, "scheme": "http",
           "timeout": 3, "username": "u", "password": "p", "verify_tls": False}
    code, body, _ = _get(serve([dev]) + "/probe")
    assert code == 200 and 'probe_success{device="cfg"' in body


def test_probe_unreachable_device_still_200_with_success_zero(serve):
    code, body, _ = _get(serve() + "/probe?host=127.0.0.1&port=1&timeout=2")
    assert code == 200 and "probe_success" in body and "} 0.0" in body


def test_index_escapes_device_fields(serve):
    dev = {"name": "<script>alert(1)</script>", "host": "h&x", "scheme": "http", "port": None}
    code, body, _ = _get(serve([dev]) + "/")
    assert code == 200
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body and "h&amp;x" in body
