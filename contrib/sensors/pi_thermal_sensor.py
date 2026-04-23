#!/usr/bin/env python3
"""Pi hardware thermal sensor — publishes SoC temp, fan, throttle flags to MQTT.

Standalone sensor daemon deployed to each Raspberry Pi in the fleet.
Reads local thermal state from ``/sys/class/thermal``, ``/sys/class/hwmon``,
and ``vcgencmd``, then publishes a normalized ``ThermalReading`` to the
GlowUp MQTT broker on a signal-class-first topic tree::

    glowup/hardware/thermal/<node_id>     (retained, every interval)

Online/offline presence follows the existing ``distributed.capability``
convention using MQTT Last Will & Testament::

    glowup/node/<node_id>/status          (retained, "online" | "offline")
    glowup/node/<node_id>/capability      (retained, NodeCapability JSON)

Runs on Pi 3/4/5.  Degrades gracefully on boards without a hwmon fan
entry (Pi 3, Pi 4 with 5V-always-on fan): ``fan_rpm`` and
``fan_pwm_step`` are reported as ``null`` while ``fan_declared_present``
is populated from the config file so the server-side operator can still
treat the host as "fan-expected" for alerting.

The top-level ``ThermalReading`` schema is normalized across all
platforms (Pi / Mac / Jetson / NAS) so a single dashboard subscription
``glowup/hardware/thermal/#`` sees one consistent shape.  Platform-
specific fields (``throttled_flags``, ``volts_core``) go in ``extra``.

Usage::

    python3 -m contrib.sensors.pi_thermal_sensor --config /etc/glowup/pi_thermal.conf
    python3 contrib/sensors/pi_thermal_sensor.py --config /etc/glowup/pi_thermal.conf

Deploy (Pi):

- Copy this file to ``/opt/glowup-sensors/pi_thermal_sensor.py``
- ``sudo apt install -y python3-paho-mqtt``
- Drop a config at ``/etc/glowup/pi_thermal.conf`` (see pi_thermal.conf.example)
- Install the ``services/pi-thermal.service`` systemd unit and enable it

Press Ctrl+C (SIGINT) or send SIGTERM for graceful shutdown.  On exit
the sensor explicitly publishes ``offline`` on its status topic so the
orchestrator sees the transition immediately without waiting for LWT.
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

# The interval watcher lives next to us in contrib/sensors.  Direct
# module-next-to-module import so this file works whether launched as
# `python -m contrib.sensors.pi_thermal_sensor` or `python path/to/file`.
try:
    from contrib.sensors._interval_watcher import IntervalWatcher
except ImportError:
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from _interval_watcher import IntervalWatcher  # type: ignore

logger: logging.Logger = logging.getLogger("glowup.pi_thermal")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default broker host — Pi 5 "glowup" hub runs the canonical MQTT broker.
# See reference_project_state.md.
_DEFAULT_BROKER_HOST: str = "10.0.0.214"

# Default MQTT port — mosquitto stock listener.
_DEFAULT_BROKER_PORT: int = 1883

# Default publish interval (seconds).  30s balances trend resolution
# against MQTT/retain churn on the broker.
_DEFAULT_INTERVAL_S: float = 30.0

# Default config file path on deployed Pis.
_DEFAULT_CONFIG_PATH: str = "/etc/glowup/pi_thermal.conf"

# MQTT topic tree.  Signal-class-first under hardware/, per the
# architecture decision on 2026-04-11 — see feedback memory of the
# same date.  Platform is metadata inside the payload.
_HARDWARE_TOPIC_PREFIX: str = "glowup/hardware/thermal/"

# Node-level topics — reused from distributed.capability conventions
# so this sensor plays with any future orchestrator cleanly.
_NODE_TOPIC_PREFIX: str = "glowup/node/"
_STATUS_SUFFIX: str = "/status"
_CAPABILITY_SUFFIX: str = "/capability"

# LWT and explicit-shutdown payloads.
_STATUS_ONLINE: str = "online"
_STATUS_OFFLINE: str = "offline"

# MQTT QoS — at-least-once delivery so a late subscriber sees retained
# state even after a broker restart.
_QOS_AT_LEAST_ONCE: int = 1

# Keep-alive to the broker (seconds).  paho default, stated explicitly.
_MQTT_KEEPALIVE_S: int = 60

# Sysfs paths.
_THERMAL_ZONE_TEMP_PATH: str = "/sys/class/thermal/thermal_zone0/temp"
_HWMON_ROOT: str = "/sys/class/hwmon"
_COOLING_DEVICE_CUR_STATE: str = "/sys/class/thermal/cooling_device0/cur_state"
_PROC_LOADAVG: str = "/proc/loadavg"
_PROC_UPTIME: str = "/proc/uptime"
_DEVICE_TREE_MODEL: str = "/proc/device-tree/model"

# Kernel reports temperature in millicelsius.
_MILLICELSIUS_PER_CELSIUS: float = 1000.0

# vcgencmd subprocess timeout — the firmware mailbox rarely takes more
# than a few ms, but cap it so a hung call can't stall a sample cycle.
_VCGENCMD_TIMEOUT_S: float = 5.0

# Name of the hwmon device exposed by the Pi 5 cooling_fan dtoverlay.
_PWM_FAN_HWMON_NAME: str = "pwmfan"

# Reconnect backoff when the MQTT loop drops (seconds).
_MQTT_RECONNECT_DELAY_S: float = 5.0


# ---------------------------------------------------------------------------
# ThermalReading
# ---------------------------------------------------------------------------

@dataclass
class ThermalReading:
    """One normalized thermal sample from a hardware node.

    The top-level fields are the cross-platform schema — every producer
    (Pi, Mac, Jetson, NAS) fills these with ``None`` where the signal
    is not available.  Platform-specific fields live in ``extra`` and
    are ignored by generic consumers.

    Attributes:
        ts:                    ISO 8601 UTC timestamp of the sample.
        node_id:               Short hostname / logical node identifier.
        platform:              Short platform slug (e.g. ``"pi5"``).
        cpu_temp_c:            SoC temperature in degrees Celsius.
        fan_rpm:               Tach-reported fan RPM (None if no tach).
        fan_pwm_step:          Firmware cooling step (None if no cooling
                               device registered — see Pi 3/4 with 5V
                               always-on fan).
        fan_declared_present:  Config-declared fan presence, used to
                               distinguish "no fan expected" from "fan
                               expected but invisible to sysfs".
        load_1m:               1-minute load average from /proc/loadavg.
        load_5m:               5-minute load average.
        load_15m:              15-minute load average.
        uptime_s:              Seconds since boot from /proc/uptime.
        extra:                 Platform-specific fields (throttled flags,
                               core voltage, Pi-model string, etc.).
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
        """Serialize to compact JSON for MQTT publishing.

        Returns:
            Compact JSON string with all fields.
        """
        return json.dumps(asdict(self), separators=(",", ":"))


# ---------------------------------------------------------------------------
# Low-level sysfs / vcgencmd readers
# ---------------------------------------------------------------------------

def _read_cpu_temp_c() -> Optional[float]:
    """Read the primary thermal zone temperature in Celsius.

    Returns:
        Temperature in Celsius, or ``None`` if sysfs is unavailable or
        the file contains garbage.
    """
    try:
        with open(_THERMAL_ZONE_TEMP_PATH, "r") as fh:
            millic: float = float(fh.read().strip())
            return millic / _MILLICELSIUS_PER_CELSIUS
    except (OSError, ValueError) as exc:
        logger.debug("cpu_temp read failed: %s", exc)
        return None


def _read_fan_rpm() -> Optional[int]:
    """Read fan RPM from the pwmfan hwmon device, if present.

    Walks ``/sys/class/hwmon/*/name`` looking for the ``pwmfan`` entry
    (Pi 5 cooling_fan dtoverlay).  On a match, reads ``fan1_input``.

    Returns:
        RPM reading, or ``None`` if no pwmfan hwmon is registered.
    """
    try:
        for entry in os.listdir(_HWMON_ROOT):
            name_path: str = os.path.join(_HWMON_ROOT, entry, "name")
            try:
                with open(name_path, "r") as fh:
                    if fh.read().strip() != _PWM_FAN_HWMON_NAME:
                        continue
                rpm_path: str = os.path.join(
                    _HWMON_ROOT, entry, "fan1_input",
                )
                with open(rpm_path, "r") as fh:
                    return int(fh.read().strip())
            except (OSError, ValueError) as exc:
                logger.debug("hwmon %s read failed: %s", entry, exc)
                continue
    except OSError as exc:
        logger.debug("hwmon enumeration failed: %s", exc)
    return None


def _read_fan_pwm_step() -> Optional[int]:
    """Read the firmware thermal controller's current cooling step.

    On a Pi 5 with the cooling_fan dtoverlay, this is a 0..N step that
    tracks which PWM bucket the firmware has placed the fan in.

    Returns:
        Cooling step integer, or ``None`` if no cooling device exists.
    """
    try:
        with open(_COOLING_DEVICE_CUR_STATE, "r") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError) as exc:
        logger.debug("cooling_device cur_state read failed: %s", exc)
        return None


def _read_loadavg() -> tuple[float, float, float]:
    """Read the 1, 5, and 15-minute load averages.

    Returns:
        Tuple of three floats.  All zeros on read failure — load is
        cheap and its absence is unusual, so a zero is benign.
    """
    try:
        with open(_PROC_LOADAVG, "r") as fh:
            parts: list[str] = fh.read().split()
            return float(parts[0]), float(parts[1]), float(parts[2])
    except (OSError, ValueError, IndexError) as exc:
        logger.debug("loadavg read failed: %s", exc)
        return 0.0, 0.0, 0.0


def _read_uptime_s() -> float:
    """Read seconds-since-boot from /proc/uptime.

    Returns:
        Uptime in seconds (float), or 0.0 on read failure.
    """
    try:
        with open(_PROC_UPTIME, "r") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError) as exc:
        logger.debug("uptime read failed: %s", exc)
        return 0.0


def _read_pi_model() -> str:
    """Read the board model string from the device tree.

    Returns:
        Model string (e.g. ``"Raspberry Pi 5 Model B Rev 1.0"``) or
        ``"unknown"`` on a platform without a Linux device tree.
    """
    try:
        # The device-tree model blob is null-terminated; strip before decode.
        with open(_DEVICE_TREE_MODEL, "rb") as fh:
            raw: bytes = fh.read().rstrip(b"\x00")
            return raw.decode("ascii", errors="replace")
    except OSError as exc:
        logger.debug("pi model read failed: %s", exc)
        return "unknown"


def _platform_slug(model: str) -> str:
    """Derive a short platform slug from the device-tree model string.

    Args:
        model: Long board model string from /proc/device-tree/model.

    Returns:
        ``"pi5"`` / ``"pi4"`` / ``"pi3"`` / ``"pi"`` (unknown Pi) /
        ``"linux"`` (not a Pi).
    """
    m: str = model.lower()
    if "raspberry pi 5" in m:
        return "pi5"
    if "raspberry pi 4" in m:
        return "pi4"
    if "raspberry pi 3" in m:
        return "pi3"
    if "raspberry pi" in m:
        return "pi"
    return "linux"


def _run_vcgencmd(subcommand: list[str]) -> Optional[str]:
    """Run a vcgencmd invocation and return its trimmed stdout.

    Args:
        subcommand: Argv list passed after ``vcgencmd``.

    Returns:
        Stripped stdout on success, ``None`` on any failure (missing
        binary, non-zero exit, timeout, kernel mailbox error).
    """
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            ["vcgencmd", *subcommand],
            capture_output=True,
            text=True,
            timeout=_VCGENCMD_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0:
            logger.debug(
                "vcgencmd %s nonzero rc=%d: %s",
                subcommand, result.returncode, result.stderr.strip(),
            )
            return None
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("vcgencmd %s failed: %s", subcommand, exc)
        return None


def _read_throttled_flags() -> Optional[str]:
    """Read the firmware's cumulative throttle / undervoltage flags.

    The ``vcgencmd get_throttled`` bitmask carries history: the high
    bits record events that have cleared.  This is the single most
    important diagnostic for "was this board ever stressed" because
    it survives the transient condition.

    Returns:
        Hex string (e.g. ``"0x50000"``) or ``None`` if vcgencmd is
        unavailable.
    """
    out: Optional[str] = _run_vcgencmd(["get_throttled"])
    if out is None:
        return None
    # Output format: "throttled=0x0"
    if "=" not in out:
        return None
    return out.split("=", 1)[-1].strip()


def _read_core_volts() -> Optional[float]:
    """Read the SoC core rail voltage reported by the firmware.

    Returns:
        Voltage as a float, or ``None`` on any failure.
    """
    out: Optional[str] = _run_vcgencmd(["measure_volts", "core"])
    if out is None:
        return None
    # Output format: "volt=0.8715V"
    try:
        return float(out.split("=", 1)[-1].rstrip("V"))
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# PiThermalSensor
# ---------------------------------------------------------------------------

class PiThermalSensor:
    """MQTT-publishing thermal sensor for Raspberry Pi hardware.

    Connects to the configured MQTT broker with an LWT on the
    ``glowup/node/<node_id>/status`` topic, publishes a NodeCapability
    announcement once on startup, then samples and publishes a
    normalized :class:`ThermalReading` on every interval until stopped.

    Args:
        broker_host:           MQTT broker host.
        broker_port:           MQTT broker TCP port.
        interval_s:            Seconds between samples.
        node_id:               Short node identifier (used in topics).
        hostname:              Long hostname for NodeCapability metadata.
        fan_declared_present:  Whether the config file says a fan is
                               physically present on this host (used for
                               Pi 3/4 with 5V-always-on fans that are
                               invisible to sysfs).
        pi_model:              Long device-tree model string.
        platform:              Short platform slug (``"pi5"``, etc.).
    """

    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        interval_s: float,
        node_id: str,
        hostname: str,
        fan_declared_present: bool,
        pi_model: str,
        platform: str,
    ) -> None:
        """See class docstring."""
        self._broker_host: str = broker_host
        self._broker_port: int = broker_port
        self._interval_s: float = interval_s
        self._node_id: str = node_id
        self._hostname: str = hostname
        self._fan_declared_present: bool = fan_declared_present
        self._pi_model: str = pi_model
        self._platform: str = platform

        self._status_topic: str = (
            f"{_NODE_TOPIC_PREFIX}{node_id}{_STATUS_SUFFIX}"
        )
        self._capability_topic: str = (
            f"{_NODE_TOPIC_PREFIX}{node_id}{_CAPABILITY_SUFFIX}"
        )
        self._thermal_topic: str = f"{_HARDWARE_TOPIC_PREFIX}{node_id}"

        self._client: Optional["mqtt.Client"] = None
        self._stop_event: threading.Event = threading.Event()
        self._samples_published: int = 0
        self._start_time: float = 0.0
        # Fleet-wide interval override via retained MQTT config topic.
        self._watcher: IntervalWatcher = IntervalWatcher(interval_s)

    # ---- MQTT lifecycle -----------------------------------------------------

    def connect(self) -> None:
        """Build the MQTT client, set LWT, connect, publish online state.

        Raises:
            RuntimeError: If paho-mqtt is not importable.
            OSError:      If the broker is unreachable (propagated from
                          paho ``connect``).
        """
        if not _HAS_PAHO:
            raise RuntimeError(
                "paho-mqtt is required. Install with "
                "'sudo apt install -y python3-paho-mqtt'."
            )

        # client_id scoped to the node so the broker cleanly replaces
        # a prior instance's session on reconnect.
        client: mqtt.Client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"pi-thermal-{self._node_id}",
        )
        client.will_set(
            self._status_topic,
            _STATUS_OFFLINE,
            qos=_QOS_AT_LEAST_ONCE,
            retain=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        # Attach BEFORE connect so the subscribe fires on the first
        # CONNACK rather than racing the initial publish.
        self._watcher.attach(client)

        client.connect(
            self._broker_host, self._broker_port, _MQTT_KEEPALIVE_S,
        )
        client.loop_start()
        self._client = client

        # Publish online status and capability announcement.  These
        # are retained so a late subscriber sees current state.
        client.publish(
            self._status_topic, _STATUS_ONLINE,
            qos=_QOS_AT_LEAST_ONCE, retain=True,
        )
        self._publish_capability()

    def _on_connect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        *args: Any,
    ) -> None:
        """paho callback — logs connection outcome.

        paho v2 passes (flags, reason_code, properties); v1 passed
        (flags, rc).  Using ``*args`` accepts both; the reason code
        is always the second positional.
        """
        raw_rc = args[1] if len(args) >= 2 else args[0]
        rc: int = raw_rc.value if hasattr(raw_rc, "value") else int(raw_rc)
        if rc == 0:
            logger.info(
                "connected to %s:%d as pi-thermal-%s",
                self._broker_host, self._broker_port, self._node_id,
            )
        else:
            logger.error("mqtt connect failed rc=%d", rc)

    def _on_disconnect(
        self,
        client: "mqtt.Client",
        userdata: Any,
        *args: Any,
    ) -> None:
        """paho callback — logs unexpected disconnects.

        paho v2 passes (flags, reason_code, properties); v1 passed
        (rc,).  Using ``*args`` accepts both.
        """
        raw_rc = args[1] if len(args) >= 2 else args[0]
        rc: int = raw_rc.value if hasattr(raw_rc, "value") else int(raw_rc)
        if rc != 0:
            logger.warning(
                "mqtt unexpected disconnect rc=%d (paho will retry)", rc,
            )

    def _publish_capability(self) -> None:
        """Publish a NodeCapability announcement for this sensor."""
        cap: dict[str, Any] = {
            "node_id": self._node_id,
            "hostname": self._hostname,
            "roles": ["sensor"],
            "resources": {
                "hardware": ["thermal"],
                "platform": self._platform,
                "model": self._pi_model,
            },
            "operators": [],
            "emitters": [],
            "version": __version__,
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
            ),
        }
        assert self._client is not None
        self._client.publish(
            self._capability_topic,
            json.dumps(cap, separators=(",", ":")),
            qos=_QOS_AT_LEAST_ONCE,
            retain=True,
        )

    # ---- Sampling -----------------------------------------------------------

    def _sample(self) -> ThermalReading:
        """Take one snapshot of all thermal metrics.

        Returns:
            A fully populated :class:`ThermalReading`.  Fields that can
            not be read are set to ``None`` (fan_rpm, fan_pwm_step,
            cpu_temp_c) or omitted from ``extra`` (throttled_flags,
            volts_core).
        """
        cpu_temp_c: Optional[float] = _read_cpu_temp_c()
        fan_rpm: Optional[int] = _read_fan_rpm()
        fan_pwm_step: Optional[int] = _read_fan_pwm_step()
        l1, l5, l15 = _read_loadavg()
        uptime_s: float = _read_uptime_s()

        extra: dict[str, Any] = {"model": self._pi_model}
        throttled: Optional[str] = _read_throttled_flags()
        if throttled is not None:
            extra["throttled_flags"] = throttled
        volts: Optional[float] = _read_core_volts()
        if volts is not None:
            extra["volts_core"] = volts

        return ThermalReading(
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            node_id=self._node_id,
            platform=self._platform,
            cpu_temp_c=cpu_temp_c,
            fan_rpm=fan_rpm,
            fan_pwm_step=fan_pwm_step,
            fan_declared_present=self._fan_declared_present,
            load_1m=l1,
            load_5m=l5,
            load_15m=l15,
            uptime_s=uptime_s,
            extra=extra,
        )

    # ---- Main loop ----------------------------------------------------------

    def run(self) -> None:
        """Publish a sample every ``interval_s`` until stop() is called."""
        self._start_time = time.monotonic()
        logger.info(
            "pi_thermal_sensor running — node_id=%s platform=%s "
            "interval=%.1fs fan_declared_present=%s topic=%s",
            self._node_id, self._platform, self._interval_s,
            self._fan_declared_present, self._thermal_topic,
        )

        while not self._stop_event.is_set():
            try:
                reading: ThermalReading = self._sample()
                assert self._client is not None
                self._client.publish(
                    self._thermal_topic,
                    reading.to_json(),
                    qos=_QOS_AT_LEAST_ONCE,
                    retain=True,
                )
                self._samples_published += 1
                logger.debug("published %s", reading)
            except Exception as exc:
                # Never let one bad sample kill the daemon — log and
                # continue.  Perry's rule: corrupt/garbage input must
                # not crash; every handler logs or re-raises.
                logger.error(
                    "sample/publish failed: %s", exc, exc_info=True,
                )
            # Read live — respects fleet-wide updates to
            # glowup/config/thermal_interval_s between samples.
            self._stop_event.wait(self._watcher.current())

    def stop(self) -> None:
        """Graceful shutdown — publish offline and disconnect.

        Explicit offline publish is belt-and-suspenders; the LWT will
        also fire if the process is killed hard.
        """
        self._stop_event.set()
        if self._client is None:
            return
        try:
            self._client.publish(
                self._status_topic, _STATUS_OFFLINE,
                qos=_QOS_AT_LEAST_ONCE, retain=True,
            )
            # Tiny pause so the offline publish makes it onto the wire
            # before disconnect cancels the loop.
            time.sleep(0.2)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:
            logger.error("clean shutdown failed: %s", exc)
        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "pi_thermal_sensor stopped — %d samples in %.1f s",
            self._samples_published, elapsed,
        )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

@dataclass
class SensorConfig:
    """Parsed sensor configuration.

    Attributes:
        broker_host:          MQTT broker host.
        broker_port:          MQTT broker TCP port.
        interval_s:           Publish interval in seconds.
        node_id:              Short node identifier.
        fan_declared_present: Whether a fan is physically present.
    """
    broker_host: str
    broker_port: int
    interval_s: float
    node_id: str
    fan_declared_present: bool


def _load_config(path: str) -> SensorConfig:
    """Load and validate configuration from an INI file.

    Missing sections and keys fall back to module defaults so a minimal
    config file is legal.  A missing file entirely also falls back to
    defaults with a single logged warning.

    Args:
        path: Filesystem path to the INI config.

    Returns:
        A populated :class:`SensorConfig`.
    """
    parser: configparser.ConfigParser = configparser.ConfigParser()
    if os.path.exists(path):
        parser.read(path)
    else:
        logger.warning(
            "config file %s not found — using built-in defaults", path,
        )

    mqtt_section: configparser.SectionProxy = (
        parser["mqtt"] if parser.has_section("mqtt")
        else configparser.ConfigParser()[configparser.DEFAULTSECT]
    )
    sensor_section: configparser.SectionProxy = (
        parser["sensor"] if parser.has_section("sensor")
        else configparser.ConfigParser()[configparser.DEFAULTSECT]
    )

    broker_host: str = mqtt_section.get("broker", _DEFAULT_BROKER_HOST)
    broker_port: int = int(mqtt_section.get("port", _DEFAULT_BROKER_PORT))
    interval_s: float = float(
        sensor_section.get("interval", _DEFAULT_INTERVAL_S),
    )
    node_id: str = sensor_section.get("node_id", "").strip()
    if not node_id:
        # Fall back to short hostname — matches existing
        # reference_broker2.md naming (short lowercase).
        node_id = socket.gethostname().split(".")[0].lower()

    fan_declared_present: bool = sensor_section.getboolean(
        "fan_declared_present", fallback=False,
    )

    return SensorConfig(
        broker_host=broker_host,
        broker_port=broker_port,
        interval_s=interval_s,
        node_id=node_id,
        fan_declared_present=fan_declared_present,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Command-line entry point.

    Returns:
        Process exit code (0 on clean shutdown, nonzero on fatal error).
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp Pi thermal sensor — publishes SoC/fan state to MQTT",
    )
    parser.add_argument(
        "--config", default=_DEFAULT_CONFIG_PATH,
        help=f"Path to INI config (default: {_DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--broker", default=None,
        help="Override broker host from config",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override broker port from config",
    )
    parser.add_argument(
        "--interval", type=float, default=None,
        help="Override publish interval in seconds",
    )
    parser.add_argument(
        "--node-id", default=None,
        help="Override node identifier from config",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args: argparse.Namespace = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not _HAS_PAHO:
        logger.error(
            "paho-mqtt is not installed. Run: "
            "sudo apt install -y python3-paho-mqtt",
        )
        return 2

    cfg: SensorConfig = _load_config(args.config)
    if args.broker is not None:
        cfg.broker_host = args.broker
    if args.port is not None:
        cfg.broker_port = args.port
    if args.interval is not None:
        cfg.interval_s = args.interval
    if args.node_id is not None:
        cfg.node_id = args.node_id

    pi_model: str = _read_pi_model()
    platform: str = _platform_slug(pi_model)
    hostname: str = socket.gethostname()

    sensor: PiThermalSensor = PiThermalSensor(
        broker_host=cfg.broker_host,
        broker_port=cfg.broker_port,
        interval_s=cfg.interval_s,
        node_id=cfg.node_id,
        hostname=hostname,
        fan_declared_present=cfg.fan_declared_present,
        pi_model=pi_model,
        platform=platform,
    )

    def _shutdown(signum: int, frame: Optional[FrameType]) -> None:
        """Signal handler — stop the sensor and exit cleanly."""
        logger.info("received signal %d — shutting down", signum)
        sensor.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        sensor.connect()
    except Exception as exc:
        logger.error("failed to connect to broker: %s", exc)
        return 1

    try:
        sensor.run()
    except KeyboardInterrupt:
        sensor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
