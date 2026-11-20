# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Prometheus exporter for [Gude](https://gude-systems.com/) Expert Power Control (EPC) PDUs —
outlet on/off state plus every sensor reading the device reports (per-outlet power metering,
incoming-line power metering, attached temperature/humidity probes). Targets the EPC 8035 series
(8-outlet switched and outlet-metered PDU) but is generic across Gude's EPC line by design — see
"Why the sensor metric is generic" below.

**Not verified against a live device.** Unlike this fleet's `lw3-exporter` (verified against a real
Lightware unit with a throwaway client before any production code was written) or `extronsis-exporter`
(scrapes an HTML element whose shape was directly inspected), this exporter was built entirely from
Gude's published documentation:
[EPC_HTTP_Interface](https://wiki.gude-systems.com/EPC_HTTP_Interface),
[HTTP_JSON_Sensor_Data](https://wiki.gude-systems.com/HTTP_JSON_Sensor_Data), and
[API_Overview_eng](https://wiki.gude-systems.com/API_Overview_eng). Those pages document the
envelope (`outputs`, `sensor_descr`, `sensor_values`) and one concrete `outputs` example with
field-level confidence, but do **not** publish a fixed field list for the power-meter sensor types —
that's a deliberate protocol design on Gude's part (see below), not a documentation gap this repo
worked around by guessing. **If you get access to a real EPC-8035, the highest-value thing to do
next is hit `/probe` against it and diff the response against what `prober.py` expects** — start
there before trusting anything else in this file about exact field names.

Sibling exporters in this fleet: `pearl-exporter` (Sanic + HTTP API, Epiphan Pearl),
`extronsis-exporter` (stdlib http.server, HTML scraping, Extron), `pjlink-exporter` (stdlib
http.server, raw TCP socket, cross-vendor projector protocol), and `lw3-exporter` (stdlib
http.server, WebSocket, Lightware). This repo follows `lw3-exporter`'s architecture most closely
(package layout, config/URL dual-mode `/probe`, stdlib server, fresh-connection-per-probe prober,
`bao.py` OpenBao lookup returning `(username, password)`) since Gude's interface — like LW3 — is a
single authenticated endpoint returning structured data, not an HTML page to scrape (extronsis) or
a bespoke line protocol (pjlink).

## Commands

```bash
# Install runtime deps (PyYAML, prometheus-client, hvac)
pip install -r requirements.txt

# Run locally
cp config.example.yaml config.yaml   # edit devices
python -m gude_exporter -c config.yaml

# CLI flags override config file values
python -m gude_exporter -c config.yaml --host 0.0.0.0 --port 9880 --log-level DEBUG

# Docker
docker build -t gude-exporter .
docker compose up -d
```

```bash
pip install -r requirements-dev.txt
ruff check .                    # lint (+ bandit security rules), config in pyproject.toml
pytest --cov                    # tests/ — stub HTTP server serving a canned statusjsn.js
scripts/smoke-test.sh           # real exporter process vs. a stub device; also checks SIGTERM
pip-audit -r requirements.txt
```

Tests live in `tests/`; `conftest.py` has the `stub_device` fixture (a real local HTTP server
whose response is set per test). The canned response is shaped from Gude's docs, **not** captured
from a real EPC-8035, so passing tests don't prove field names are right — see the top of this file.

CI (`.github/workflows/`): `ci.yml` (ruff, pytest matrix 3.10–3.14, smoke test, pip-audit, Trivy
image scan; also runs weekly and is reused by `docker.yml` so images are only published from a
green build), `codeql.yml`, `dependency-review.yml`, plus `dependabot.yml`. `docker.yml` builds and
pushes the multiarch image to Docker Hub on push to `main`.

## Architecture

Four files under `gude_exporter/`, each with a single responsibility:

- **`prober.py`** — `probe_device()`: issues one `GET /statusjsn.js?components=<bitmask>` request
  per probe (HTTP Basic auth via header, not URL-embedded — keeps credentials out of access logs),
  parses the JSON response into a `ProbeResult`. The `components` bitmask requests exactly four
  sections: `outputs` (1), `misc` (16), `sensor_values` (16384), `sensor_descr` (65536) — see
  `_COMPONENTS`. Probe succeeds once the response parses as JSON containing at least one of
  `outputs`/`sensor_descr`; a device with zero configured outlets or zero sensors still counts as a
  successful probe, an empty/malformed body doesn't. No connection pooling — fresh `http.client`
  connection per probe, matching `lw3-exporter`/`pjlink-exporter`.

- **`collector.py`** — `GudeCollector(prometheus_client.registry.Collector)`: turns `ProbeResult`s
  into Prometheus metric families (`probe_success`, `probe_duration_seconds`, `gude_outlet_state`,
  `gude_sensor_value`, `gude_device` info). `collect()` runs fresh on every scrape (no caching),
  probing every configured device sequentially. Per-device failures don't abort the whole scrape.
  Purely a protocol-result-to-metric mapper; credential resolution lives in `__main__.py`, not
  here — kept this way so the collector stays testable without touching `bao.py`.

- **`bao.py`** — OpenBao `(username, password)` lookup, `get_credentials(hostname)`. Copied from
  `lw3-exporter`'s `bao.py` (itself copied from `pearl-exporter`'s) since Gude's status interface
  needs a username, like LW3 and unlike PJLink's single shared password. Returns `None` on any
  failure — never logs secret values.

- **`__main__.py`** — config loading (`load_config`, YAML + defaults merge), credential resolution
  (`_resolve_credentials`: OpenBao first per device by hostname, falling back to the config
  file's/URL's `username`/`password` fields), the HTTP handler (`_make_handler` closes over the
  loaded `cfg` dict), and `main()`/CLI argument parsing. Routes: `/` (HTML index), `/probe`,
  `/metrics`, `/healthz`.

### The two probing modes (both always active, can combine)

- **Config-file mode**: plain `GET /probe` scrapes every device listed under `devices:` in the
  YAML config.
- **URL-parameter mode**: `GET /probe?host=...&name=...&username=...&password=...` scrapes a
  single device described entirely by query params — no config-file entry needed.
  `_device_from_query_params()` in `__main__.py` builds the device dict; only `host` is required,
  everything else falls back to `_DEVICE_DEFAULTS`.

If a request supplies `host` as a query param, URL-parameter mode wins for that request even if
`devices:` is also configured — the two modes are per-request, not mutually exclusive globally.

### `/metrics` vs `/probe` — do not confuse these

- `/probe` returns **device** metrics (`probe_*`, `gude_*`), built from a fresh `CollectorRegistry`
  + `GudeCollector` per request.
- `/metrics` returns the **exporter's own** self-metrics (`gude_exporter_probe_requests_total`,
  `gude_exporter_probe_duration_seconds`, plus default process/platform collectors) from
  `prometheus_client`'s global `REGISTRY`. It does not touch any device.

### Why the sensor metric is generic (`gude_sensor_value`, not `gude_outlet_voltage_volts` etc.)

Gude's own JSON envelope (`sensor_descr` + `sensor_values`) is *itself* self-describing — every
reading carries its own field name and unit in the response (`fields: [{"name": "Voltage", "unit":
"V", ...}]`), because the field set genuinely varies by sensor type and product (a 3-phase line
meter reports fields a single-phase outlet meter doesn't; Gude's docs list over a dozen sensor
`type` IDs — line meters, outlet meters, DC meters, temperature/humidity probes, digital inputs —
each with its own field shape that isn't published as a fixed table). Given that, and given this
exporter has no live device to confirm exact field spellings against (see top of this file),
`collector.py` exposes one generic `gude_sensor_value` metric labeled with whatever `field`/`unit`
the device itself reports, rather than a canonical per-field metric name guessed from partial
documentation. This mirrors `lw3-exporter`'s `_discover_temperatures` philosophy (discover the
tree at probe time instead of hardcoding a path that varies by product) but applies it to *every*
sensor reading, not just temperature, because Gude's interface makes the whole sensor space
self-describing, not just one subtree.

If you later confirm live field names (e.g. `field="Voltage"`, `field="Current"`, `field="Active
Power"` for `sensor_type="outlet_power_meter"`) and want typed per-field metrics
(`gude_outlet_voltage_volts` etc.) instead of relying on the `field`/`unit` labels, that's a
`collector.py`-only change — `prober.py`'s `SensorReading` already carries everything needed
(`field`, `unit`, `value`, plus `sensor_type`/`type_label`/`instance_id`/`instance_name`); add a
name-matching table in `collector.py` and keep `gude_sensor_value` as the fallback for anything
that doesn't match, the same hybrid approach `snmp_exporter`/vendor-specific exporters commonly use
for a self-describing wire format.

### `misc` / `gude_device_info` is best-effort, on purpose

Gude's docs describe the `misc` component (bit 16) only as "firmware version, bootloader version,
etc." — no field-name table. `_parse_misc()` in `prober.py` tries a short list of plausible key
names (`product`/`model`/`devname`/..., `serial`/`sernum`/..., `version_fw`/`firmware`/...) and
silently includes whatever matches, keying off `_MISC_KEY_CANDIDATES`. This is expected to need
correction once tested against a real device's actual JSON keys — check that dict first if
`gude_device_info` comes back empty against a real unit.

### Adding a new metric

1. If it's outlet-switching related, extend `_parse_outputs()` / `OutletReading` in `prober.py`.
2. If it's a new sensor field, it likely needs **no prober change at all** — `_parse_sensors()`
   already walks every `sensor_descr`/`sensor_values` block generically and `gude_sensor_value`
   already exposes every field. Only add a canonical mapping in `collector.py` if you want a typed
   metric name instead of relying on the `field`/`unit` labels (see previous section).
3. If it's a new top-level `statusjsn.js` component (e.g. `port_summary`, bit 256), add its bit to
   `_COMPONENTS`, parse it in `prober.py`, add a field on `ProbeResult`, and yield a new
   `GaugeMetricFamily`/`InfoMetricFamily` in `GudeCollector.collect()` (plus the matching
   empty-family stub in `describe()`).
4. Update the metrics table in `README.md`.

`InfoMetricFamily(name, ...)` renders as `<name>_info` in the exposition format — the family is
registered as `"gude_device"` in `collector.py` but shows up as `gude_device_info` in scrape output.

## Configuration

`config.yaml` is gitignored (contains internal hostnames and a plaintext credential fallback if
OpenBao isn't set up) — `config.example.yaml` is the tracked template and also what gets baked
into the Docker image as the built-in default (`/etc/gude-exporter/config.yaml`, overridable via
the `GUDE_CONFIG` env var or by bind-mounting over that path). The `devices:` list is optional at
every level — an exporter with zero configured devices is valid and simply requires every
`/probe` call to pass `host` (and credentials, one way or another).

`scheme` (`http`/`https`, default `http`) and `port` (default `None`, resolved to 80 or 443 based
on `scheme` in `prober.py`) are per-device, unlike `lw3-exporter` which is WebSocket/HTTPS-only —
Gude's docs show every example over plain HTTP, and that's expected to be the norm on an isolated
PDU management VLAN, but `https` is there for sites that need it. `verify_tls` (default `False`)
only takes effect when `scheme: https`.

## Protocol reference — read this before touching `prober.py`

Everything below is sourced from Gude's own wiki, not confirmed live — see the top of this file.

- **Endpoint**: `GET /statusjsn.js?components=<decimal bitmask>` over HTTP (default) or HTTPS.
  Auth: HTTP Basic, either embedded in the URL (`http://user:pass@host/...`, Gude's own doc
  examples) or as an `Authorization: Basic <base64>` header (also documented; this exporter uses
  the header form to avoid credentials in access logs).
- **`components` bitmask** (sum the bits you want; this exporter always requests `1 | 16 | 16384 |
  65536 = 81937`):
    - `1` — `outputs`: per-outlet switch state.
    - `16` — `misc`: firmware/bootloader/product info (field names not published — best-effort).
    - `16384` — `sensor_values`: actual readings.
    - `65536` — `sensor_descr`: field/instance descriptions for those readings.
    - (Other documented bits — `inputs`, `dns_cache`, `ethernet`, `events`, `port_summary`,
      `hardware`, and several GSM-specific ones for cellular-equipped models — aren't requested;
      not relevant to a fixed-line PDU like the EPC 8035.)
- **`outputs` shape** (confirmed with a verbatim example in Gude's docs):
  `[{"name": "Power Port 1", "state": 1, "type": 1, "batch": [...], "wdog": [...]}, ...]`. `state`
  is 0/1; `type` is `1` (Powerport) or `2` (GPIO); array position (0-based in JSON, 1-based once
  this exporter labels it) is the outlet number, matching the `p=` parameter Gude's own switching
  CGI (`/ov.html`, **not called by this exporter** — read-only) uses.
- **`sensor_descr` / `sensor_values` shape**: two parallel top-level arrays, one block per sensor
  `type` present on the device:
  ```
  sensor_descr[i]  = {"type": <int>, "num": <int>,
                       "fields":     [{"name": ..., "unit": ..., "decPrecision": ...}, ...],
                       "properties": [{"id": ..., "name": ...}, ...]}       # one entry per instance
  sensor_values[i] = {"type": <int>, "num": <int>,
                       "values": [[{"v": ...}, ...], ...]}                  # instance -> field -> reading
  ```
  Match blocks between the two arrays by `type` (not array position — `_parse_sensors()` builds a
  `type -> block` map rather than assuming matching order, since that's not explicitly guaranteed).
  Within a block, `properties[j]` names instance `j` and `values[j][k]` is that instance's reading
  for `fields[k]`.
- **Sensor `type` IDs** (`_SENSOR_TYPE_LABELS` in `prober.py`, from Gude's docs): `1` line power
  meter, `7` digital inputs, `8` outlet power meter, `9` line power meter w/ residual current, `12`
  Bender RCMB module, `15` line power meter (3-phase), `16` neutral current (3-phase), `17` DC
  outlet meter, `20` system data, `51`/`52`/`53` temperature / temp+humidity / temp+humidity+pressure,
  `101` bank (eFuses port-groups, EPC 8291 only), `102` DC power source (EPC 8291 only). The EPC
  8035 is expected to report at least `1` (incoming line) and `8` (the 8 metered outlets); the
  others are there for free if a compatible probe is attached or a different EPC model is pointed
  at this same exporter.
- **Outlet power meter fields** (per Gude's docs, not a live example): current (A), voltage (V),
  phase angle, power factor, frequency (Hz), active power (W), apparent power (VA), reactive power
  (VAr) — plus **two energy counters per outlet** (one free-running, one user-resettable), exact
  field names unconfirmed. This is exactly why `gude_sensor_value` reads field names from the
  response instead of assuming this list's wording — treat it as "what to expect roughly," not "the
  literal `field` label values you'll see."
