"""
Gude Expert Power Control (EPC) device prober.

Speaks Gude's HTTP/JSON status interface (``GET /statusjsn.js?components=<bitmask>``),
documented at https://wiki.gude-systems.com/EPC_HTTP_Interface and
https://wiki.gude-systems.com/HTTP_JSON_Sensor_Data. Built from Gude's published API
documentation, not verified live against a physical EPC-8035 (unlike this fleet's
lw3-exporter, which was verified against a real device before being written) — if a
probe against a real unit turns up a mismatch, that's the thing to fix first.

Two response shapes matter here, both requested via the ``components`` bitmask
(a sum of power-of-two flags — see ``_COMPONENTS`` below):

- ``outputs``: one entry per switched/metered outlet — ``{"name", "state", "type",
  "batch", "wdog"}``. ``state`` is 0/1 (off/on); ``type`` is 1 (Powerport) or 2
  (GPIO). Outlet number is the entry's 1-based position in the array (matching the
  ``p=`` parameter used by the device's own switching CGI, ``/ov.html`` — this
  exporter doesn't call that; it's read-only, matching the other exporters here).

- ``sensor_descr`` / ``sensor_values``: a self-describing measurement interface,
  used for both per-outlet metering (type 8, "Outlet power meter") and whole-device
  incoming-line metering (type 1/9/15). Two parallel top-level arrays, matched
  element-by-element:

    sensor_descr[i]  = {"type": <int>, "num": <int>,
                         "fields": [{"name": ..., "unit": ..., "decPrecision": ...}, ...],
                         "properties": [{"id": ..., "name": ...}, ...]}   # one per instance
    sensor_values[i] = {"type": <int>, "num": <int>,
                         "values": [[{"v": ...}, ...], ...]}          # instance -> field -> reading

  ``properties[j]`` describes instance ``j`` (e.g. outlet 3); ``values[j][k]``
  is that instance's reading for ``fields[k]`` (e.g. Voltage). Field *names* and
  *units* are read from the device's own response, not hardcoded — Gude's docs
  don't publish a fixed field list per sensor type, and it varies by product
  (e.g. 3-phase line meters carry fields single-phase ones don't), so this prober
  discovers them at probe time rather than assuming a fixed schema, mirroring how
  this fleet's lw3-exporter discovers temperature sensors instead of hardcoding a
  path (see its ``_discover_temperatures``).

Auth: HTTP Basic (``Authorization: Basic base64(username:password)``), sent as a
header rather than embedded in the URL (Gude's own docs show both forms; the header
form keeps credentials out of access logs). Default transport is plain HTTP on port
80, matching every example in Gude's docs; set ``scheme: https`` per device for
units with TLS configured on their management interface (self-signed by default on
this class of embedded device, hence ``verify_tls`` defaulting to ``False``, same
default this fleet's lw3-exporter uses for the same reason).
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HTTP_PORT = 80
DEFAULT_HTTPS_PORT = 443

# statusjsn.js 'components' bitmask - only the bits this prober actually reads.
# See https://wiki.gude-systems.com/EPC_HTTP_Interface for the full table.
_COMPONENT_OUTPUTS = 1  # switched/metered outlet states
_COMPONENT_MISC = 16  # firmware/bootloader/product info (best-effort; see _parse_misc)
_COMPONENT_SENSOR_VALUES = 16384  # actual sensor readings
_COMPONENT_SENSOR_DESCR = 65536  # sensor field/instance descriptions
_COMPONENTS = (
    _COMPONENT_OUTPUTS | _COMPONENT_MISC | _COMPONENT_SENSOR_VALUES | _COMPONENT_SENSOR_DESCR
)

# Sensor 'type' -> human-readable label, per
# https://wiki.gude-systems.com/HTTP_JSON_Sensor_Data. Only used to label
# metrics; readings for unlisted type IDs are still exposed, just with a
# generic label.
_SENSOR_TYPE_LABELS = {
    1: "line_power_meter",
    7: "digital_inputs",
    8: "outlet_power_meter",
    9: "line_power_meter_rcd",
    12: "bender_rcmb",
    15: "line_power_meter_3phase",
    16: "neutral_current",
    17: "dc_outlet_meter",
    20: "system_data",
    51: "temperature",
    52: "temperature_humidity",
    53: "temperature_humidity_pressure",
    101: "bank",
    102: "dc_power_source",
}

_OUTPUT_TYPE_LABELS = {1: "powerport", 2: "gpio"}

# A status response is small JSON; cap the read so a misbehaving device can't
# make us buffer an unbounded body.
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass
class OutletReading:
    """One entry from the 'outputs' array."""

    index: int  # 1-based, matches the device's own '?p=' switching parameter
    name: str
    state: bool
    type_label: str


@dataclass
class SensorReading:
    """One (instance, field) measurement from 'sensor_descr' + 'sensor_values'."""

    sensor_type: int
    type_label: str
    instance_id: str
    instance_name: str
    field: str
    unit: str
    value: float


@dataclass
class ProbeResult:
    """Result of probing a single Gude EPC device."""

    success: bool = False
    error: str | None = None
    duration_seconds: float = 0.0

    outlets: list[OutletReading] = field(default_factory=list)
    sensors: list[SensorReading] = field(default_factory=list)
    # Best-effort device identity from the 'misc' component. Keys present
    # depend on firmware/model - see _parse_misc.
    info: dict[str, str] = field(default_factory=dict)


class _ProtocolError(ValueError):
    """Well-formed HTTP/JSON, but not the shape this prober expects."""


def _parse_outputs(data: Any) -> list[OutletReading]:
    outlets: list[OutletReading] = []
    if not isinstance(data, list):
        return outlets
    for i, entry in enumerate(data):
        try:
            outlets.append(
                OutletReading(
                    index=i + 1,
                    name=str(entry.get("name", f"Outlet {i + 1}")),
                    state=bool(entry["state"]),
                    type_label=_OUTPUT_TYPE_LABELS.get(entry.get("type"), "unknown"),
                )
            )
        except (KeyError, TypeError, AttributeError) as exc:
            logger.debug("Skipping malformed outputs[%d] entry: %s", i, exc)
    return outlets


def _parse_sensors(descr: Any, values: Any) -> list[SensorReading]:
    """
    Zip 'sensor_descr' and 'sensor_values' into flat readings.

    Both are lists of per-type blocks in the same order (confirmed by Gude's
    docs); each block's 'properties' (descr) and 'values' (values) are
    parallel per-instance arrays, and within an instance, 'fields' (descr) and
    the per-instance value list (values) are parallel per-field. A mismatch
    anywhere (missing block, wrong length) means this device's response
    doesn't match the documented shape - skip that block rather than raise,
    since every other block may still be fine.
    """
    readings: list[SensorReading] = []
    if not isinstance(descr, list) or not isinstance(values, list):
        return readings

    values_by_type: dict[int, Any] = {}
    for block in values:
        if isinstance(block, dict) and "type" in block:
            values_by_type[block["type"]] = block

    for descr_block in descr:
        if not isinstance(descr_block, dict):
            continue
        sensor_type = descr_block.get("type")
        value_block = values_by_type.get(sensor_type)
        if value_block is None:
            continue
        type_label = _SENSOR_TYPE_LABELS.get(sensor_type, f"type_{sensor_type}")

        fields = descr_block.get("fields") or []
        properties = descr_block.get("properties") or []
        instance_values = value_block.get("values") or []

        for j, instance_readings in enumerate(instance_values):
            prop = properties[j] if j < len(properties) else {}
            instance_id = str(prop.get("id", j + 1))
            instance_name = str(prop.get("name", instance_id))
            for k, reading in enumerate(instance_readings or []):
                if k >= len(fields):
                    break
                try:
                    value = float(reading["v"])
                except (KeyError, TypeError, ValueError):
                    continue
                field_descr = fields[k] or {}
                readings.append(
                    SensorReading(
                        sensor_type=sensor_type,
                        type_label=type_label,
                        instance_id=instance_id,
                        instance_name=instance_name,
                        field=str(field_descr.get("name", f"field_{k}")),
                        unit=str(field_descr.get("unit", "")),
                        value=value,
                    )
                )
    return readings


# Candidate key names for device identity, tried in order. Not documented by
# Gude with a fixed schema (see module docstring) - best-effort only, so an
# unrecognized 'misc' shape just yields an empty info dict rather than
# failing the probe.
_MISC_KEY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "product": ("product", "model", "devname", "sys_devname", "productname"),
    "serial": ("serial", "sernum", "serial_device", "serialnumber"),
    "firmware_version": ("version_fw", "fw_version", "firmware", "version"),
    "bootloader_version": ("version_boot", "boot_version", "bootloader"),
}


def _parse_misc(data: Any) -> dict[str, str]:
    info: dict[str, str] = {}
    if not isinstance(data, dict):
        return info
    lowered = {str(k).lower(): v for k, v in data.items()}
    for label, candidates in _MISC_KEY_CANDIDATES.items():
        for key in candidates:
            if key in lowered and lowered[key] not in (None, ""):
                info[label] = str(lowered[key])
                break
    return info


def probe_device(
    host: str,
    username: str,
    password: str,
    port: int | None = None,
    scheme: str = "http",
    timeout: float = 10.0,
    verify_tls: bool = False,
) -> ProbeResult:
    """
    Query a Gude EPC device's ``/statusjsn.js`` endpoint for outlet states and
    self-described sensor readings (per-outlet + line power metering,
    temperature/humidity probes if attached).

    Connectivity, HTTP, and JSON failures are caught and reported via
    :attr:`ProbeResult.success` / :attr:`ProbeResult.error` rather than raised,
    so callers can always emit a ``probe_success`` metric. The probe succeeds
    once the response parses as JSON with at least one recognized top-level
    section (``outputs`` or sensor data) - a device that legitimately has zero
    switched outlets configured, or zero sensors, shouldn't fail the probe on
    that alone, but an empty/malformed body should.
    """
    start = time.monotonic()
    result = ProbeResult()

    if port is None:
        port = DEFAULT_HTTPS_PORT if scheme == "https" else DEFAULT_HTTP_PORT

    auth = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    path = f"/statusjsn.js?components={_COMPONENTS}"

    conn: http.client.HTTPConnection
    try:
        if scheme == "https":
            ssl_context = ssl.create_default_context()
            if not verify_tls:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=ssl_context
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)

        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read(_MAX_RESPONSE_BYTES + 1)

            if resp.status == 401:
                raise _ProtocolError("authentication failed (HTTP 401)")
            if resp.status != 200:
                raise _ProtocolError(f"unexpected HTTP status {resp.status}")
            if len(body) > _MAX_RESPONSE_BYTES:
                raise _ProtocolError("response exceeded maximum size")

            data = json.loads(body)
            if not isinstance(data, dict):
                raise _ProtocolError("response was not a JSON object")

            outlets = _parse_outputs(data.get("outputs"))
            sensors = _parse_sensors(data.get("sensor_descr"), data.get("sensor_values"))
            info = _parse_misc(data.get("misc"))

            if not outlets and not sensors and "outputs" not in data and "sensor_descr" not in data:
                raise _ProtocolError(
                    "response contained none of the requested sections "
                    "(outputs, sensor_descr)"
                )

            result.outlets = outlets
            result.sensors = sensors
            result.info = info
            result.success = True
        finally:
            conn.close()

    except (OSError, http.client.HTTPException, json.JSONDecodeError, _ProtocolError) as exc:
        result.error = str(exc)
        logger.warning("Probe failed for %s:%d: %s", host, port, exc)
    finally:
        result.duration_seconds = time.monotonic() - start

    return result
