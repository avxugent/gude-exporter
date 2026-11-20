# Gude Exporter

[![CI](https://github.com/avxugent/gude-exporter/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/avxugent/gude-exporter/actions/workflows/ci.yml)
[![CodeQL](https://github.com/avxugent/gude-exporter/actions/workflows/github-code-scanning/codeql/badge.svg?branch=main)](https://github.com/avxugent/gude-exporter/security/code-scanning)
[![Docker build](https://github.com/avxugent/gude-exporter/actions/workflows/docker.yml/badge.svg?branch=main)](https://github.com/avxugent/gude-exporter/actions/workflows/docker.yml)
[![Docker Hub](https://img.shields.io/badge/docker%20hub-kristofkeppens%2Fgude--exporter-2496ed?logo=docker&logoColor=white)](https://hub.docker.com/r/kristofkeppens/gude-exporter)
[![Dependabot](https://img.shields.io/badge/dependabot-enabled-025e8c?logo=dependabot)](https://github.com/avxugent/gude-exporter/security/dependabot)
[![Trivy](https://img.shields.io/badge/image%20scan-trivy-1904da?logo=aqua&logoColor=white)](https://github.com/avxugent/gude-exporter/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)
![Code style: ruff](https://img.shields.io/badge/lint-ruff-261230?logo=ruff)
[![License: MIT](https://img.shields.io/github/license/avxugent/gude-exporter)](LICENSE)

A Prometheus exporter for [Gude](https://gude-systems.com/) Expert Power Control (EPC) PDUs,
written in Python — targets the EPC 8035 series (8-outlet switched and outlet-metered PDU), but
generic across Gude's EPC line since it reads the device's own self-described sensor schema rather
than a hardcoded one (see "Metrics" below).

The exporter talks to the device's built-in **HTTP/JSON status interface**
(`GET /statusjsn.js?components=<bitmask>`), documented at
[wiki.gude-systems.com/EPC_HTTP_Interface](https://wiki.gude-systems.com/EPC_HTTP_Interface) and
[wiki.gude-systems.com/HTTP_JSON_Sensor_Data](https://wiki.gude-systems.com/HTTP_JSON_Sensor_Data)
— the same interface Gude's own docs call their REST API. It's read-only: this exporter reports
metrics, it does not switch outlets.

## Usage

### Docker

#### Build

```bash
docker build -t gude-exporter .
```

#### Run

```bash
docker run -p 9880:9880 gude-exporter
```

### Manual

1.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```

2.  Copy the example config and edit it:
    ```bash
    cp config.example.yaml config.yaml
    ```

3.  Run the exporter:
    ```bash
    python -m gude_exporter -c config.yaml
    ```

    CLI flags override config file values:
    ```bash
    python -m gude_exporter -c config.yaml --host 0.0.0.0 --port 9880 --log-level DEBUG
    ```

## Credentials (OpenBao)

The exporter looks up each device's Gude username/password in [OpenBao](https://openbao.org/)
(Vault-compatible), keyed by the device hostname, so credentials don't have to live in the config
file or scrape URL. The device's status interface authenticates with plain HTTP Basic auth — the
same username/password used to log into its web UI.

### Secret layout

All devices live under one common KV **v2** path, with the device hostname as the final path
segment:

```bash
bao kv put secret/gude/192.168.1.10 username=admin password=secret
```

Here the mount is `secret`, the prefix is `gude`, and `192.168.1.10` is the device hostname.

### Configuration

Configure the exporter via environment variables (`BAO_*` take precedence, `VAULT_*` are accepted
as fallbacks so Nomad's native integration vars work directly):

| Variable | Default | Description |
|----------|---------|--------------|
| `BAO_ADDR` / `VAULT_ADDR` | — | OpenBao server address |
| `BAO_TOKEN` / `VAULT_TOKEN` | — | Token used to authenticate to OpenBao |
| `BAO_KV_MOUNT` | `secret` | KV v2 mount point |
| `BAO_PATH_PREFIX` | (empty) | Common path under the mount before the hostname |
| `BAO_CACERT` / `VAULT_CACERT` | — | Optional CA bundle for TLS verification |

If the OpenBao lookup returns nothing (not configured, secret missing, network error), the
exporter falls back to the device's `username`/`password` fields in `config.yaml`, or the matching
URL query parameters.

### Nomad

```hcl
vault {}

template {
  data        = "VAULT_TOKEN={{ env \"VAULT_TOKEN\" }}"
  destination = "secrets/bao.env"
  env         = true
}
```

## Metrics

The exporter exposes device metrics on the `/probe` endpoint.

**Config-file mode** — scrapes every device listed in `config.yaml`:

```bash
curl "http://localhost:9880/probe"
```

**URL-parameter mode** — probes a single device by query parameter, no config-file entry needed
(useful for driving many devices via Prometheus `relabel_configs`, blackbox-exporter style):

```bash
curl "http://localhost:9880/probe?host=192.168.1.10&name=pdu-rack-3&username=admin&password=secret"
```

Both modes are always available; a request with a `host` parameter always wins for that request.

### Exposed Metrics

-   `probe_success`: 1 if the device responded to a `statusjsn.js` query, 0 otherwise.
-   `probe_duration_seconds`: How long the probe took to complete.
-   `gude_outlet_state` (labels: `outlet`, `outlet_name`, `type`): 1 if the outlet is switched on, 0
    if off. `outlet` is the outlet's 1-based position (matching the device's own numbering);
    `type` is `powerport` (a switched/metered mains outlet) or `gpio`.
-   `gude_sensor_value` (labels: `sensor_type`, `instance`, `instance_name`, `field`, `unit`): every
    reading the device reports, **as the device itself describes it** — field name and unit come
    from the device's response at probe time, not from a hardcoded list. This is what carries
    per-outlet metering (voltage, current, active/apparent/reactive power, power factor, frequency,
    accumulated energy — `sensor_type="outlet_power_meter"`), incoming-line metering
    (`sensor_type="line_power_meter"`), and any attached temperature/humidity probe
    (`sensor_type="temperature"` / `"temperature_humidity"`). Example PromQL for one outlet's
    voltage: `gude_sensor_value{sensor_type="outlet_power_meter", field="Voltage", instance="3"}`.
-   `gude_device_info` (info metric): device identity (product, serial, firmware/bootloader
    version), when the device's response includes fields this exporter recognizes — see
    `CLAUDE.md` for why this one is best-effort rather than guaranteed.

All device metrics carry `device` (the configured name) and `host` labels.

### Exporter self-metrics

`/metrics` returns the exporter's own process metrics (not any device's), for monitoring the
exporter itself: `gude_exporter_probe_requests_total`, `gude_exporter_probe_duration_seconds`, plus
the default Python/process collectors.

## Notes

- Devices are probed fresh on every scrape — no caching, no persistent connections.
- Plain HTTP on port 80 is the default transport, matching every example in Gude's own
  documentation and typical deployment on an isolated management VLAN. Set `scheme: https` (and
  `port: 443`, adjusted as needed) per device for units with TLS configured; `verify_tls` defaults
  to **off** since these devices typically carry a self-signed certificate on their management
  interface out of the box.
- This exporter was built from Gude's published API documentation
  ([EPC_HTTP_Interface](https://wiki.gude-systems.com/EPC_HTTP_Interface),
  [HTTP_JSON_Sensor_Data](https://wiki.gude-systems.com/HTTP_JSON_Sensor_Data)), not verified live
  against a physical EPC-8035 — see `CLAUDE.md` for exactly what that means for the exporter's
  design (in particular, the self-describing `gude_sensor_value` metric and the best-effort
  `gude_device_info`). If you have a real device, checking a `/probe` response against it and
  filing corrections is the single most useful thing to do next.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .                      # lint, includes bandit (S) security rules
pytest --cov                      # unit + stub-device tests (no real PDU needed)
scripts/smoke-test.sh             # starts the real exporter against a stub device
pip-audit -r requirements.txt     # known-vulnerable dependencies
```

The tests serve a canned `statusjsn.js` response from a local stub HTTP server, so they exercise
the real `http.client` code path. They are shaped from Gude's documentation, **not** captured from
a physical EPC-8035 — see `CLAUDE.md`.

### CI and security scanning

| Check | Where | What it covers |
|---|---|---|
| Ruff | `ci.yml` | lint + static security rules (flake8-bandit) |
| pytest | `ci.yml` | Python 3.10–3.14 matrix, with coverage |
| Smoke test | `ci.yml` | real process, endpoints, graceful SIGTERM |
| pip-audit | `ci.yml` (weekly too) | known CVEs in `requirements.txt` |
| Trivy | `ci.yml` (weekly too) | fixable HIGH/CRITICAL CVEs in the Docker image; SARIF goes to the Security tab |
| CodeQL | GitHub default setup (repo settings) | static analysis of the Python code and workflows |
| Dependency review | `dependency-review.yml` | blocks PRs that add dependencies with high-severity CVEs |
| Dependabot | `dependabot.yml` | weekly PRs for pip, Docker base image and GitHub Actions |

`docker.yml` only publishes an image after the whole CI workflow has passed, and rebuilds weekly to
pick up base-image fixes.
