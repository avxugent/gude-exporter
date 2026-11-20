"""probe_device() against a stub HTTP server, plus the pure parsers."""

from __future__ import annotations

import base64

from gude_exporter import prober
from gude_exporter.prober import _parse_misc, _parse_outputs, _parse_sensors, probe_device


def _probe(dev, **kw):
    return probe_device("127.0.0.1", "admin", "s3cret", port=dev.port, timeout=3, **kw)


def test_success_parses_outlets_sensors_and_info(stub_device):
    r = _probe(stub_device)
    assert r.success and r.error is None
    assert [(o.index, o.name, o.state, o.type_label) for o in r.outlets] == [
        (1, "Power Port 1", True, "powerport"),
        (2, "Power Port 2", False, "powerport"),
        (3, "GPIO 1", True, "gpio"),
    ]
    by_key = {(s.type_label, s.instance_id, s.field): s for s in r.sensors}
    # blocks are matched by type, not array position (values are reordered in the sample)
    assert by_key[("outlet_power_meter", "1", "Active Power")].value == 12.5
    assert by_key[("outlet_power_meter", "2", "Active Power")].value == 0.0
    assert by_key[("line_power_meter", "L1", "Voltage")].unit == "V"
    assert by_key[("line_power_meter", "L1", "Frequency")].value == 50.0
    assert r.info == {
        "product": "Expert Power Control 8035",
        "serial": "ABC123",
        "firmware_version": "1.2.3",
    }
    assert r.duration_seconds > 0


def test_request_shape_and_basic_auth_header(stub_device):
    _probe(stub_device)
    path, headers = stub_device.requests[0]
    assert path == "/statusjsn.js?components=81937"
    expected = base64.b64encode(b"admin:s3cret").decode()
    assert headers["Authorization"] == f"Basic {expected}"
    assert "admin" not in path and "s3cret" not in path  # never in the URL


def test_http_401_is_auth_failure(stub_device):
    stub_device.status = 401
    r = _probe(stub_device)
    assert not r.success
    assert "401" in r.error


def test_unexpected_status(stub_device):
    stub_device.status = 500
    r = _probe(stub_device)
    assert not r.success and "500" in r.error


def test_invalid_json(stub_device):
    stub_device.body_bytes = b"<html>not json</html>"
    r = _probe(stub_device)
    assert not r.success and r.error


def test_json_not_an_object(stub_device):
    stub_device.body = [1, 2, 3]
    r = _probe(stub_device)
    assert not r.success and "not a JSON object" in r.error


def test_object_without_known_sections_fails(stub_device):
    stub_device.body = {"something": "else"}
    r = _probe(stub_device)
    assert not r.success and "none of the requested sections" in r.error


def test_device_with_no_outlets_or_sensors_still_succeeds(stub_device):
    stub_device.body = {"outputs": [], "sensor_descr": [], "sensor_values": []}
    r = _probe(stub_device)
    assert r.success and r.outlets == [] and r.sensors == []


def test_oversized_response_rejected(stub_device, monkeypatch):
    monkeypatch.setattr(prober, "_MAX_RESPONSE_BYTES", 100)
    r = _probe(stub_device)
    assert not r.success and "maximum size" in r.error


def test_connection_refused_reports_failure():
    r = probe_device("127.0.0.1", "a", "b", port=1, timeout=2)
    assert not r.success and r.error
    assert r.duration_seconds >= 0


def test_default_ports(monkeypatch):
    seen = {}

    class Boom(OSError):
        pass

    def fake_http(host, port, timeout=None):
        seen["http"] = port
        raise Boom("x")

    def fake_https(host, port, timeout=None, context=None):
        seen["https"] = (port, context)
        raise Boom("x")

    monkeypatch.setattr(prober.http.client, "HTTPConnection", fake_http)
    monkeypatch.setattr(prober.http.client, "HTTPSConnection", fake_https)
    probe_device("h", "u", "p")
    probe_device("h", "u", "p", scheme="https")
    assert seen["http"] == 80
    assert seen["https"][0] == 443


def test_https_verify_tls_toggle(monkeypatch):
    import ssl

    contexts = []

    def fake_https(host, port, timeout=None, context=None):
        contexts.append(context)
        raise OSError("x")

    monkeypatch.setattr(prober.http.client, "HTTPSConnection", fake_https)
    probe_device("h", "u", "p", scheme="https", verify_tls=False)
    probe_device("h", "u", "p", scheme="https", verify_tls=True)
    assert contexts[0].verify_mode == ssl.CERT_NONE and not contexts[0].check_hostname
    assert contexts[1].verify_mode == ssl.CERT_REQUIRED and contexts[1].check_hostname


# ---- pure parsers --------------------------------------------------------


def test_parse_outputs_skips_malformed_entries():
    out = _parse_outputs(
        [{"name": "ok", "state": 1, "type": 1}, {"name": "no state"}, "junk", None]
    )
    assert [o.index for o in out] == [1]
    assert _parse_outputs("nope") == []
    assert _parse_outputs(None) == []


def test_parse_outputs_unknown_type_and_default_name():
    (o,) = _parse_outputs([{"state": 0, "type": 99}])
    assert o.name == "Outlet 1" and o.type_label == "unknown" and o.state is False


def test_parse_sensors_tolerates_bad_shapes():
    descr = [
        "junk",
        {"type": 8, "fields": [{"name": "A", "unit": "W"}], "properties": []},
        {"type": 99, "fields": [{"name": "Z"}], "properties": [{"id": "x"}]},
        {"type": 51, "fields": [{"name": "T", "unit": "C"}], "properties": [{"id": "t"}]},
    ]
    values = [
        {"type": 8, "values": [[{"v": "3.5"}, {"v": 9}], [{"x": 1}], [{"v": "bad"}]]},
        {"type": 51, "values": [[{"v": 21.5}]]},
        # type 99 has no values block -> skipped
    ]
    got = _parse_sensors(descr, values)
    assert [(s.type_label, s.instance_id, s.field, s.value) for s in got] == [
        ("outlet_power_meter", "1", "A", 3.5),  # extra field k>=len(fields) dropped
        ("temperature", "t", "T", 21.5),
    ]


def test_parse_sensors_non_list_inputs():
    assert _parse_sensors(None, None) == []
    assert _parse_sensors({}, []) == []


def test_parse_sensors_unlisted_type_gets_generic_label():
    got = _parse_sensors(
        [{"type": 77, "fields": [{"name": "F"}], "properties": [{"id": "i", "name": "N"}]}],
        [{"type": 77, "values": [[{"v": 1}]]}],
    )
    assert got[0].type_label == "type_77" and got[0].unit == "" and got[0].instance_name == "N"


def test_parse_misc_candidates_case_insensitive():
    assert _parse_misc({"Model": "EPC", "SerNum": 42, "FW_Version": "1.0", "boot_version": ""}) == {
        "product": "EPC",
        "serial": "42",
        "firmware_version": "1.0",
    }
    assert _parse_misc(None) == {} and _parse_misc([]) == {} and _parse_misc({}) == {}
