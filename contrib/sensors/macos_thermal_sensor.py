#!/usr/bin/env python3
"""macOS hardware thermal sensor — publishes CPU temp + load to MQTT.

Companion to ``pi_thermal_sensor.py`` and ``x86_thermal_sensor.py``.
Targets Apple Silicon Macs (Mac Studio, Mac mini, MacBook Pro M-series).
Reads CPU/GPU die temperatures via ``macmon`` (brew-installable,
sudoless, Apple-Silicon-native) and publishes a normalized
``ThermalReading`` to the GlowUp MQTT broker::

    glowup/hardware/thermal/<node_id>     (retained, every interval)

Uses the same schema as the Pi/x86 sensors so the thermal dashboard,
logger, and fleet overview work identically.

What works on macOS:
    cpu_temp_c      — from `macmon pipe -s 1` (.temp.cpu_temp_avg)
    extra.gpu_temp_c — same probe (.temp.gpu_temp_avg)
    extra.cpu_power_w — `.cpu_power`
    load_1m/5m/15m  — `os.getloadavg()`
    uptime_s        — `sysctl -n kern.boottime`
    platform        — "macos"
    extra.model     — `sysctl -n hw.model`

What does NOT work on macOS without sudo (and is omitted):
    fan_rpm         — Mac Studio fan tach is gated to powermetrics(sudo)
    fan_pwm_step    — N/A
    throttled_flags — Pi-firmware-specific (vcgencmd), no Mac equivalent

Requires:
    brew install macmon
    pip install paho-mqtt

Usage::

    python3 macos_thermal_sensor.py --broker 10.0.0.214 --node daedalus
    python3 macos_thermal_sensor.py --config ~/macos_thermal.conf
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import configparser
import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import asdict, dataclass, field
from types import FrameType
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

logger: logging.Logger = logging.getLogger("glowup.macos_thermal")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BROKER_HOST: str = "10.0.0.214"
_DEFAULT_BROKER_PORT: int = 1883
_DEFAULT_INTERVAL_S: float = 30.0
_THERMAL_TOPIC_PREFIX: str = "glowup/hardware/thermal"
_STATUS_TOPIC_PREFIX: str = "glowup/node"


# ---------------------------------------------------------------------------
# ThermalReading (same schema as pi_thermal_sensor.py / x86_thermal_sensor.py)
# ---------------------------------------------------------------------------

@dataclass
class ThermalReading:
    """One normalized thermal sample from a macOS host."""

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
# Readers
# ---------------------------------------------------------------------------

def read_macmon_sample() -> dict[str, Any]:
    """Run ``macmon pipe -s 1`` and return the parsed JSON sample.

    macmon is a sudoless Apple Silicon system monitor.  ``pipe -s 1``
    emits exactly one JSON line and exits.  Schema (relevant fields)::

        {"temp":{"cpu_temp_avg":35.78,"gpu_temp_avg":32.03},
         "cpu_power":0.05, "gpu_power":0.01, "all_power":0.07, ...}

    Returns:
        Parsed dict, or empty dict on any failure.
    """
    try:
        result = subprocess.run(
            ["macmon", "pipe", "-s", "1"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.warning("macmon pipe returned %d: %s",
                           result.returncode, result.stderr.strip())
            return {}
        # `pipe -s 1` emits one line; strip + parse.
        return json.loads(result.stdout.strip())
    except FileNotFoundError:
        logger.error("macmon not found — install with: brew install macmon")
        return {}
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        logger.warning("macmon parse/timeout: %s", exc)
        return {}


def read_uptime_s() -> float:
    """Read system uptime in seconds via ``sysctl -n kern.boottime``.

    Output looks like ``{ sec = 1776700000, usec = 123456 } Sat Apr 19 ...``.
    We just need the ``sec = ...`` field.
    """
    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True, text=True, timeout=2,
        )
        m = re.search(r"sec\s*=\s*(\d+)", result.stdout)
        if not m:
            return 0.0
        return time.time() - float(m.group(1))
    except Exception:
        return 0.0


def read_model() -> Optional[str]:
    """Read hardware model identifier via ``sysctl -n hw.model``.

    Returns strings like ``Mac14,13`` (Mac Studio M2 Max) or ``Mac15,3``
    (MacBook Pro M3).  None on any failure.
    """
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.model"],
            capture_output=True, text=True, timeout=2,
        )
        out: str = result.stdout.strip()
        return out or None
    except Exception:
        return None


def collect_reading(node_id: str) -> ThermalReading:
    """Collect a single thermal reading from this Mac.

    Args:
        node_id: Logical node identifier.
    """
    sample: dict[str, Any] = read_macmon_sample()
    temp_block: dict[str, Any] = sample.get("temp") or {}
    cpu_temp: Optional[float] = temp_block.get("cpu_temp_avg")
    gpu_temp: Optional[float] = temp_block.get("gpu_temp_avg")

    load_1, load_5, load_15 = os.getloadavg()
    uptime: float = read_uptime_s()
    model: Optional[str] = read_model()

    extra: dict[str, Any] = {}
    if model:
        extra["model"] = model
    if gpu_temp is not None:
        extra["gpu_temp_c"] = float(gpu_temp)
    cpu_power: Any = sample.get("cpu_power")
    if isinstance(cpu_power, (int, float)):
        extra["cpu_power_w"] = float(cpu_power)

    return ThermalReading(
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        node_id=node_id,
        platform="macos",
        cpu_temp_c=cpu_temp,
        fan_rpm=None,
        fan_pwm_step=None,
        # Mac Studio physically has fans, just no tach we can read
        # without sudo.  Mark declared so the dashboard's "rising temp
        # without fan response" alert logic treats it as fan-equipped.
        fan_declared_present=True,
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
) -> None:
    """Run the thermal sensor publish loop."""
    global _running

    if not _HAS_PAHO:
        logger.error("paho-mqtt not installed — pip install paho-mqtt")
        return

    thermal_topic: str = f"{_THERMAL_TOPIC_PREFIX}/{node_id}"
    status_topic: str = f"{_STATUS_TOPIC_PREFIX}/{node_id}/status"

    _client_id: str = f"thermal-{node_id}-{int(time.time())}"
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=_client_id,
        )
    else:
        client = mqtt.Client(client_id=_client_id)
    client.will_set(status_topic, "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on_connect(
        c: mqtt.Client, ud: Any, flags: Any, rc: Any, props: Any = None,
    ) -> None:
        if rc == 0:
            logger.info("MQTT connected to %s:%d", broker_host, broker_port)
            c.publish(status_topic, "online", qos=1, retain=True)
        else:
            logger.warning("MQTT connect failed rc=%s", rc)

    client.on_connect = on_connect
    client.connect_async(broker_host, broker_port)
    client.loop_start()

    logger.info(
        "macOS thermal sensor starting — node=%s, broker=%s:%d, interval=%.0fs",
        node_id, broker_host, broker_port, interval_s,
    )

    try:
        while _running:
            reading: ThermalReading = collect_reading(node_id)
            payload: str = reading.to_json()
            info = client.publish(thermal_topic, payload, qos=0, retain=True)
            if info.rc == 0:
                logger.debug(
                    "Published: cpu=%.1f°C load=%.2f",
                    reading.cpu_temp_c or 0.0, reading.load_1m,
                )
            else:
                logger.warning("Publish rc=%s", info.rc)
            deadline: float = time.time() + interval_s
            while _running and time.time() < deadline:
                time.sleep(1.0)
    finally:
        client.publish(status_topic, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()
        logger.info("macOS thermal sensor stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="GlowUp macOS thermal sensor",
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
        "--verbose", "-v", action="store_true", help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    node_id: str = args.node or socket.gethostname().split(".")[0].lower()
    broker: str = args.broker
    port: int = args.port
    interval: float = args.interval

    if args.config:
        cp = configparser.ConfigParser()
        cp.read(args.config)
        if cp.has_section("mqtt"):
            broker = cp.get("mqtt", "broker", fallback=broker)
            port = cp.getint("mqtt", "port", fallback=port)
        if cp.has_section("sensor"):
            interval = cp.getfloat("sensor", "interval", fallback=interval)
            override: str = cp.get("sensor", "node_id", fallback="").strip()
            if override:
                node_id = override

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run_sensor(
        node_id=node_id,
        broker_host=broker,
        broker_port=port,
        interval_s=interval,
    )


if __name__ == "__main__":
    main()
