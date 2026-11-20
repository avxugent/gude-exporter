"""Shared fixtures: a stub Gude device serving a canned statusjsn.js over real HTTP."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# Shaped per Gude's docs (see prober.py): two outlets, one line meter, one outlet meter.
SAMPLE_STATUS = {
    "outputs": [
        {"name": "Power Port 1", "state": 1, "type": 1, "batch": [], "wdog": []},
        {"name": "Power Port 2", "state": 0, "type": 1, "batch": [], "wdog": []},
        {"name": "GPIO 1", "state": 1, "type": 2, "batch": [], "wdog": []},
    ],
    "misc": {"product": "Expert Power Control 8035", "serial": "ABC123", "version_fw": "1.2.3"},
    "sensor_descr": [
        {
            "type": 1,
            "num": 1,
            "fields": [
                {"name": "Voltage", "unit": "V", "decPrecision": 1},
                {"name": "Frequency", "unit": "Hz", "decPrecision": 2},
            ],
            "properties": [{"id": "L1", "name": "Line 1"}],
        },
        {
            "type": 8,
            "num": 2,
            "fields": [{"name": "Active Power", "unit": "W", "decPrecision": 1}],
            "properties": [{"id": "1", "name": "Outlet 1"}, {"id": "2", "name": "Outlet 2"}],
        },
    ],
    "sensor_values": [
        {"type": 8, "num": 2, "values": [[{"v": 12.5}], [{"v": 0}]]},  # deliberately reordered
        {"type": 1, "num": 1, "values": [[{"v": 230.1}, {"v": 50.0}]]},
    ],
}


class StubDevice:
    """Programmable stub: set .status / .body / .body_bytes, inspect .requests."""

    def __init__(self) -> None:
        self.status = 200
        self.body: object = SAMPLE_STATUS
        self.body_bytes: bytes | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.port = 0


@pytest.fixture
def stub_device():
    dev = StubDevice()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            dev.requests.append((self.path, dict(self.headers)))
            payload = (
                dev.body_bytes if dev.body_bytes is not None else json.dumps(dev.body).encode()
            )
            self.send_response(dev.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    dev.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield dev
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
