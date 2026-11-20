#!/usr/bin/env bash
# Smoke test: start the real exporter process against a stub Gude device and
# check the endpoints and clean shutdown. Needs only the deps from
# requirements.txt -- no real device.
#
#   scripts/smoke-test.sh            # uses `python` on PATH
#   PYTHON=.venv/bin/python scripts/smoke-test.sh
set -u

PYTHON="${PYTHON:-python}"
PORT="${PORT:-19880}"
STUB_PORT="${STUB_PORT:-19881}"
BASE="http://127.0.0.1:${PORT}"
cd "$(dirname "$0")/.."
LOG_DIR="$(mktemp -d)"
trap 'kill "$pid" "$stub" 2>/dev/null; rm -rf "$LOG_DIR"' EXIT

fail=0
check() { # check <description> <expected-http-code> <curl args...>
    local desc="$1" want="$2"
    shift 2
    local got
    got=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$@")
    if [ "$got" = "$want" ]; then
        echo "ok   $desc ($got)"
    else
        echo "FAIL $desc: expected $want, got $got"
        fail=1
    fi
}
contains() { # contains <description> <needle> <url>
    if curl -s -m 10 "$3" | grep -qF -- "$2"; then
        echo "ok   $1"
    else
        echo "FAIL $1: '$2' not found in $3"
        fail=1
    fi
}

# Stub device: serves a canned statusjsn.js.
"$PYTHON" - "$STUB_PORT" >"$LOG_DIR/stub.log" 2>&1 <<'PY' &
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
BODY = json.dumps({
    "outputs": [{"name": "Power Port 1", "state": 1, "type": 1}],
    "sensor_descr": [{"type": 8, "fields": [{"name": "Active Power", "unit": "W"}],
                      "properties": [{"id": "1", "name": "Outlet 1"}]}],
    "sensor_values": [{"type": 8, "values": [[{"v": 12.5}]]}],
}).encode()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
stub=$!

"$PYTHON" -m gude_exporter -c config.example.yaml --host 127.0.0.1 --port "$PORT" \
    >"$LOG_DIR/exporter.log" 2>&1 &
pid=$!

for _ in $(seq 1 30); do
    curl -s -m 1 -o /dev/null "$BASE/healthz" && break
    sleep 0.5
done

PROBE="$BASE/probe?host=127.0.0.1&port=${STUB_PORT}&name=stub&username=u&password=p"
check    "healthz"                     200 "$BASE/healthz"
check    "index page"                  200 "$BASE/"
check    "unknown path"                404 "$BASE/nope"
check    "self-metrics"                200 "$BASE/metrics"
check    "probe without host/devices"  400 "$BASE/probe"
check    "probe against stub device"   200 "$PROBE"
contains "probe_success is 1"          'probe_success{device="stub",host="127.0.0.1"} 1.0' "$PROBE"
contains "outlet state exported"       'gude_outlet_state{' "$PROBE"
contains "sensor value exported"       'field="Active Power"' "$PROBE"
contains "dead device reports 0"       'probe_success{device="d",host="127.0.0.1"} 0.0' \
    "$BASE/probe?host=127.0.0.1&port=1&name=d&timeout=2"

# SIGTERM must stop the server promptly.
kill -TERM "$pid"
for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
done
if kill -0 "$pid" 2>/dev/null; then
    echo "FAIL SIGTERM: server still running after 5s"
    kill -9 "$pid"
    fail=1
else
    echo "ok   SIGTERM shuts down cleanly"
fi

[ "$fail" = 0 ] || { echo "--- exporter log"; cat "$LOG_DIR/exporter.log"; }
exit "$fail"
