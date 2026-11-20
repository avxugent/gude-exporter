"""
Prometheus collector for Gude Expert Power Control (EPC) PDUs.

Probes one or more devices over Gude's HTTP/JSON status interface and exposes
outlet on/off state plus every sensor reading the device reports (per-outlet
and incoming-line power metering, temperature/humidity if attached) as
Prometheus metrics.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily, Metric

from .prober import ProbeResult, probe_device

logger = logging.getLogger(__name__)


class GudeCollector:
    """
    A Prometheus custom collector that probes Gude EPC devices.

    Each call to :meth:`collect` probes every configured device fresh (no
    caching) and yields the results.
    """

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        """
        :param devices: List of device configuration dicts. Each dict must
            contain at least ``name``, ``host``, ``username``, and
            ``password``; optional keys are ``port``, ``scheme``,
            ``timeout``, and ``verify_tls``.
        """
        self._devices = devices

    # ------------------------------------------------------------------
    # prometheus_client collector interface
    # ------------------------------------------------------------------

    def describe(self) -> Generator[Metric, None, None]:
        yield GaugeMetricFamily("probe_success", "")
        yield GaugeMetricFamily("probe_duration_seconds", "")
        yield GaugeMetricFamily("gude_outlet_state", "")
        yield GaugeMetricFamily("gude_sensor_value", "")
        yield InfoMetricFamily("gude_device", "")

    def collect(self) -> Generator[Metric, None, None]:
        """Probe all devices and yield Prometheus metric families."""
        results: list[tuple[dict[str, Any], ProbeResult]] = [
            (dev_cfg, self._probe(dev_cfg)) for dev_cfg in self._devices
        ]
        base_labels = ["device", "host"]

        # --- probe_success / probe_duration_seconds ---
        success_family = GaugeMetricFamily(
            "probe_success",
            "1 if the device responded to a statusjsn.js query, 0 otherwise.",
            labels=base_labels,
        )
        duration_family = GaugeMetricFamily(
            "probe_duration_seconds",
            "Duration of the probe in seconds.",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            labels = [dev_cfg["name"], dev_cfg["host"]]
            success_family.add_metric(labels, 1.0 if r.success else 0.0)
            duration_family.add_metric(labels, r.duration_seconds)
        yield success_family
        yield duration_family

        # --- gude_outlet_state ---
        outlet_family = GaugeMetricFamily(
            "gude_outlet_state",
            "1 if the outlet is switched on, 0 if off.",
            labels=base_labels + ["outlet", "outlet_name", "type"],
        )
        for dev_cfg, r in results:
            for o in r.outlets:
                outlet_family.add_metric(
                    [dev_cfg["name"], dev_cfg["host"], str(o.index), o.name, o.type_label],
                    1.0 if o.state else 0.0,
                )
        yield outlet_family

        # --- gude_sensor_value ---
        # One row per (instance, field) reading, self-described by the device
        # at probe time (field name + unit come from its own response, not a
        # hardcoded schema - see prober.py's module docstring for why).
        sensor_family = GaugeMetricFamily(
            "gude_sensor_value",
            "Sensor reading, as self-described by the device (see the "
            "'field' and 'unit' labels). Covers per-outlet and incoming-line "
            "power metering plus any attached temperature/humidity probes.",
            labels=base_labels + ["sensor_type", "instance", "instance_name", "field", "unit"],
        )
        for dev_cfg, r in results:
            for s in r.sensors:
                sensor_family.add_metric(
                    [
                        dev_cfg["name"], dev_cfg["host"], s.type_label,
                        s.instance_id, s.instance_name, s.field, s.unit,
                    ],
                    s.value,
                )
        yield sensor_family

        # --- gude_device (info) ---
        device_family = InfoMetricFamily(
            "gude_device",
            "Device identity, best-effort (Gude does not publish a fixed "
            "field schema for this - see prober.py).",
            labels=base_labels,
        )
        for dev_cfg, r in results:
            if r.info:
                device_family.add_metric([dev_cfg["name"], dev_cfg["host"]], r.info)
        yield device_family

    # ------------------------------------------------------------------
    # Device probing
    # ------------------------------------------------------------------

    def _probe(self, dev_cfg: dict[str, Any]) -> ProbeResult:
        """Probe a single device and return the result."""
        return probe_device(
            host=dev_cfg["host"],
            username=dev_cfg.get("username") or "",
            password=dev_cfg.get("password") or "",
            port=dev_cfg.get("port"),
            scheme=dev_cfg.get("scheme", "http"),
            timeout=float(dev_cfg.get("timeout", 10.0)),
            verify_tls=bool(dev_cfg.get("verify_tls", False)),
        )
