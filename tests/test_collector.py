"""GudeCollector -> metric families, driven by a stub device."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from gude_exporter.collector import GudeCollector


def _device(stub, name="pdu1"):
    return {
        "name": name,
        "host": "127.0.0.1",
        "port": stub.port,
        "username": "u",
        "password": "p",
        "timeout": 3,
    }


def _render(devices) -> str:
    reg = CollectorRegistry(auto_describe=True)
    reg.register(GudeCollector(devices))
    return generate_latest(reg).decode()


def test_metrics_for_healthy_device(stub_device):
    text = _render([_device(stub_device)])
    assert 'probe_success{device="pdu1",host="127.0.0.1"} 1.0' in text
    assert 'gude_outlet_state{device="pdu1",host="127.0.0.1",outlet="1",' in text
    assert 'outlet_name="Power Port 2",type="powerport"} 0.0' in text
    assert (
        'gude_sensor_value{device="pdu1",field="Active Power",host="127.0.0.1",instance="1",'
        'instance_name="Outlet 1",sensor_type="outlet_power_meter",unit="W"} 12.5'
    ) in text
    assert 'gude_device_info{device="pdu1",firmware_version="1.2.3"' in text


def test_failed_device_does_not_abort_scrape(stub_device):
    dead = {"name": "dead", "host": "127.0.0.1", "port": 1, "timeout": 2}
    text = _render([dead, _device(stub_device)])
    assert 'probe_success{device="dead",host="127.0.0.1"} 0.0' in text
    assert 'probe_success{device="pdu1",host="127.0.0.1"} 1.0' in text
    assert 'gude_outlet_state{device="dead"' not in text


def test_no_devices_yields_empty_families():
    text = _render([])
    assert "gude_outlet_state{" not in text


def test_describe_lists_families():
    names = {m.name for m in GudeCollector([]).describe()}
    assert {"probe_success", "probe_duration_seconds", "gude_outlet_state",
            "gude_sensor_value", "gude_device"} <= names
