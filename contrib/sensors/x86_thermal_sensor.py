#!/usr/bin/env python3
"""x86 hardware thermal sensor — publishes CPU/NVMe temps to MQTT.

Companion to ``pi_thermal_sensor.py`` for Intel/AMD x86 hosts.
Reads thermal state from ``lm-sensors`` (via ``sensors -j``) and
publishes a normalized ``ThermalReading`` to the GlowUp MQTT broker::

    glowup/hardware/thermal/<node_id>     (retained, every interval)

Uses the same schema as the Pi sensor so the thermal dashboard,
logger, and fleet overview work identically.  Platform-specific
fields (per-core temps, NVMe temps) go in ``extra``.

Designed for: Intel NUCs, generic x86 servers, AMD Epyc boxes.
Requires ``lm-sensors`` installed (``sudo apt install lm-sensors``).

Usage::

    python3 x86_thermal_sensor.py --broker 10.0.0.214 --node notapi
    python3 x86_thermal_sensor.py --config /etc/glowup/x86_thermal.conf

Deploy:
    - Copy to /opt/glowup-sensors/x86_thermal_sensor.py
    - sudo apt install -y lm-sensors python3-paho-mqtt
    - sudo sensors-detect --auto  (one-time, loads kernel modules)
    - Install systemd unit and enable
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import configparser
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from types import FrameType
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

logger: logging.Logger = logging.getLogger("glowup.x86_thermal")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BROKER_HOST: str = "10.0.0.214"
_DEFAULT_BROKER_PORT: int = 1883
_DEFAULT_INTERVAL_S: float = 30.0
_THERMAL_TOPIC_PREFIX: str = "glowup/hardware/thermal"
_STATUS_TOPIC_PREFIX: str = "glowup/node"


# ---------------------------------------------------------------------------
# ThermalReading (same schema as pi_thermal_sensor.py)
# ---------------------------------------------------------------------------

@dataclass
class ThermalReading:
    """One normalized thermal sample from a hardware node.

    Cross-platform schema — identical to pi_thermal_sensor.py.
    """

    ts: str
    node_id: str
    platform: str
    cpu_temp_c: Optional[float]
    fan_rpm: Optional[int]
    fan_pwm_step: Optional[int]
    fan_declared_present: bool
    load_1m: float
    load_5m: float
    load_15m: float
    uptime_s: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """Serialize to compact JSON for MQTT publishing."""
        return json.dumps(asdict(self), separators=(",", ":"))


# ---------------------------------------------------------------------------
# lm-sensors reader
# ---------------------------------------------------------------------------

def read_sensors_json() -> dict[str, Any]:
    """Run ``sensors -j`` and parse the JSON output.

    Returns:
        Parsed JSON dict from lm-sensors, or empty dict on failure.
    """
    try:
        result = subprocess.run(
            ["sensors", "-j"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.warning("sensors -j returned %d", result.returncode)
            return {}
        return json.loads(result.stdout)
    except FileNotFoundError:
        logger.error("sensors not found — install with: sudo apt install lm-sensors")
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("sensors -j output not valid JSON: %s", exc)
        return {}
    except Exception as exc:
        logger.warning("sensors -j failed: %s", exc)
        return {}


def extract_cpu_temp(sensors_data: dict) -> tuple[Optional[float], dict[str, float]]:
    """Extract CPU package temp and per-core temps from sensors data.

    Looks for ``coretemp-isa-*`` (Intel) or ``k10temp-pci-*`` (AMD).

    Args:
        sensors_data: Parsed output from ``sensors -j``.

    Returns:
        Tuple of (package_temp_c, {core_name: temp_c}).
    """
    package_temp: Optional[float] = None
    core_temps: dict[str, float] = {}

    for chip_name, chip_data in sensors_data.items():
        if not isinstance(chip_data, dict):
            continue
        chip_lower: str = chip_name.lower()

        # Intel coretemp.
        if "coretemp" in chip_lower:
            for label, readings in chip_data.items():
                if not isinstance(readings, dict):
                    continue
                label_lower: str = label.lower()
                for key, val in readings.items():
                    if "_input" in key and isinstance(val, (int, float)):
                        if "package" in label_lower:
                            package_temp = float(val)
                        elif "core" in label_lower:
                            core_temps[label] = float(val)
                        break

        # AMD k10temp.
        if "k10temp" in chip_lower:
            for label, readings in chip_data.items():
                if not isinstance(readings, dict):
                    continue
                for key, val in readings.items():
                    if "_input" in key and isinstance(val, (int, float)):
                        if "tctl" in label.lower() or "tdie" in label.lower():
                            package_temp = float(val)
                        else:
                            core_temps[label] = float(val)
                        break

    return package_temp, core_temps


def extract_nvme_temp(sensors_data: dict) -> dict[str, float]:
    """Extract NVMe temperatures from sensors data.

    Args:
        sensors_data: Parsed output from ``sensors -j``.

    Returns:
        Dict of {nvme_label: temp_c}.
    """
    nvme_temps: dict[str, float] = {}
    for chip_name, chip_data in sensors_data.items():
        if not isinstance(chip_data, dict):
            continue
        if "nvme" not in chip_name.lower():
            continue
        for label, readings in chip_data.items():
            if not isinstance(readings, dict):
                continue
            for key, val in readings.items():
                if "_input" in key and isinstance(val, (int, float)):
                    nvme_temps[f"{chip_name}/{label}"] = float(val)
                    break
    return nvme_temps


def read_load() -> tuple[float, float, float]:
    """Read system load averages.

    Returns:
        Tuple of (1m, 5m, 15m) load averages.
    """
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        load = os.getloadavg()
        return load[0], load[1], load[2]


def read_uptime() -> float:
    """Read system uptime in seconds."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def collect_reading(node_id: str, fan_declared: bool) -> ThermalReading:
    """Collect a single thermal reading from this host.

    Args:
        node_id:       Logical node identifier.
        fan_declared:  Whether a fan is expected on this host.

    Returns:
        A populated ThermalReading.
    """
    sensors_data: dict = read_sensors_json()
    package_temp, core_temps = extract_cpu_temp(sensors_data)
    nvme_temps: dict = extract_nvme_temp(sensors_data)
    load_1, load_5, load_15 = read_load()
    uptime: float = read_uptime()

    extra: dict[str, Any] = {}
    if core_temps:
        extra["core_temps"] = core_temps
    if nvme_temps:
        extra["nvme_temps"] = nvme_temps

    return ThermalReading(
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        node_id=node_id,
        platform="x86",
        cpu_temp_c=package_temp,
        fan_rpm=None,
        fan_pwm_step=None,
        fan_declared_present=fan_declared,
        load_1m=load_1,
        load_5m=load_5,
        load_15m=load_15,
        uptime_s=uptime,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# MQTT publisher loop
# ---------------------------------------------------------------------------

_running: bool = True


def _shutdown(sig: int, frame: Optional[FrameType]) -> None:
    """Signal handler for graceful shutdown."""
    global _running
    logger.info("Shutting down (signal %d)", sig)
    _running = False


def run_sensor(
    node_id: str,
    broker_host: str = _DEFAULT_BROKER_HOST,
    broker_port: int = _DEFAULT_BROKER_PORT,
    interval_s: float = _DEFAULT_INTERVAL_S,
    fan_declared: bool = False,
) -> None:
    """Run the thermal sensor publish loop.

    Args:
        node_id:      Logical node identifier.
        broker_host:  MQTT broker address.
        broker_port:  MQTT broker port.
        interval_s:   Seconds between readings.
        fan_declared: Whether a fan is expected.
    """
    global _running

    if not _HAS_PAHO:
        logger.error("paho-mqtt not installed — pip install paho-mqtt")
        return

    thermal_topic: str = f"{_THERMAL_TOPIC_PREFIX}/{node_id}"
    status_topic: str = f"{_STATUS_TOPIC_PREFIX}/{node_id}/status"

    # paho 2.x requires CallbackAPIVersion; 1.x does not have it.
    _client_id: str = f"thermal-{node_id}-{int(time.time())}"
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=_client_id,
        )
    else:
        client = mqtt.Client(client_id=_client_id)
    # LWT: mark offline if we disconnect unexpectedly.
    client.will_set(status_topic, "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(c, ud, flags, rc, props=None):
        if rc == 0:
            logger.info("MQTT connected to %s:%d", broker_host, broker_port)
            c.publish(status_topic, "online", qos=1, retain=True)
        else:
            logger.warning("MQTT connect failed rc=%s", rc)

    client.on_connect = on_connect
    client.connect_async(broker_host, broker_port)
    client.loop_start()

    logger.info(
        "x86 thermal sensor starting — node=%s, broker=%s:%d, interval=%.0fs",
        node_id, broker_host, broker_port, interval_s,
    )

    try:
        while _running:
            reading: ThermalReading = collect_reading(node_id, fan_declared)
            payload: str = reading.to_json()
            info = client.publish(thermal_topic, payload, qos=0, retain=True)
            if info.rc == 0:
                logger.debug(
                    "Published: cpu=%.1f°C load=%.2f",
                    reading.cpu_temp_c or 0.0, reading.load_1m,
                )
            else:
                logger.warning("Publish rc=%s", info.rc)
            # Sleep in short increments for responsive shutdown.
            deadline: float = time.time() + interval_s
            while _running and time.time() < deadline:
                time.sleep(1.0)
    finally:
        client.publish(status_topic, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()
        logger.info("x86 thermal sensor stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="GlowUp x86 thermal sensor",
    )
    parser.add_argument(
        "--config", default=None,
        help="INI config file (same format as pi_thermal.conf)",
    )
    parser.add_argument(
        "--node", default=None,
        help="Node identifier (default: hostname)",
    )
    parser.add_argument(
        "--broker", default=_DEFAULT_BROKER_HOST,
        help=f"MQTT broker (default: {_DEFAULT_BROKER_HOST})",
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_BROKER_PORT,
        help=f"MQTT port (default: {_DEFAULT_BROKER_PORT})",
    )
    parser.add_argument(
        "--interval", type=float, default=_DEFAULT_INTERVAL_S,
        help=f"Seconds between readings (default: {_DEFAULT_INTERVAL_S})",
    )
    parser.add_argument(
        "--fan", action="store_true",
        help="Declare that this host has a fan",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Load config file if provided.
    node_id: str = args.node or socket.gethostname().split(".")[0].lower()
    broker: str = args.broker
    port: int = args.port
    interval: float = args.interval
    fan: bool = args.fan

    if args.config:
        cp = configparser.ConfigParser()
        cp.read(args.config)
        sec: str = "thermal" if cp.has_section("thermal") else "DEFAULT"
        node_id = cp.get(sec, "node_id", fallback=node_id)
        broker = cp.get(sec, "broker_host", fallback=broker)
        port = cp.getint(sec, "broker_port", fallback=port)
        interval = cp.getfloat(sec, "interval", fallback=interval)
        fan = cp.getboolean(sec, "fan_declared_present", fallback=fan)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run_sensor(
        node_id=node_id,
        broker_host=broker,
        broker_port=port,
        interval_s=interval,
        fan_declared=fan,
    )


if __name__ == "__main__":
    main()
