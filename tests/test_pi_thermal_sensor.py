#!/usr/bin/env python3
"""Unit tests for contrib.sensors.pi_thermal_sensor.

Covers the full non-network surface of the sensor daemon:

- Sysfs readers against fabricated filesystem trees
- vcgencmd parsing via subprocess mocks
- ``ThermalReading`` JSON serialization round-trip
- ``SensorConfig`` loading with full / partial / missing INI files
- ``PiThermalSensor._sample()`` end-to-end with all IO stubbed
- CLI arg parsing and config override precedence

No MQTT broker, no network, no hardware required.  For the network
integration tests (real paho client against a real mosquitto broker)
see ``test_pi_thermal_integration.py``.

Run::

    python3 -m unittest tests.test_pi_thermal_sensor -v
    python3 -m pytest tests/test_pi_thermal_sensor.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from contrib.sensors import pi_thermal_sensor as pts
from contrib.sensors.pi_thermal_sensor import (
    PiThermalSensor,
    SensorConfig,
    ThermalReading,
    _load_config,
    _platform_slug,
    _read_core_volts,
    _read_cpu_temp_c,
    _read_fan_pwm_step,
    _read_fan_rpm,
    _read_loadavg,
    _read_pi_model,
    _read_throttled_flags,
    _read_uptime_s,
    _run_vcgencmd,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: str, content: str) -> None:
    """Write ``content`` to ``path``, creating parent dirs as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# Platform slug
# ---------------------------------------------------------------------------

class TestPlatformSlug(unittest.TestCase):
    """_platform_slug should map device-tree model strings to short slugs."""

    def test_pi5_matches(self) -> None:
        """Raspberry Pi 5 model strings slug to 'pi5'."""
        self.assertEqual(
            _platform_slug("Raspberry Pi 5 Model B Rev 1.0"), "pi5",
        )

    def test_pi4_matches(self) -> None:
        """Raspberry Pi 4 model strings slug to 'pi4'."""
        self.assertEqual(
            _platform_slug("Raspberry Pi 4 Model B Rev 1.5"), "pi4",
        )

    def test_pi3_matches(self) -> None:
        """Raspberry Pi 3 model strings slug to 'pi3'."""
        self.assertEqual(
            _platform_slug("Raspberry Pi 3 Model B Plus Rev 1.3"), "pi3",
        )

    def test_unknown_pi_falls_back_to_pi(self) -> None:
        """An unrecognized Raspberry Pi string slugs to 'pi'."""
        self.assertEqual(
            _platform_slug("Raspberry Pi Zero 2 W Rev 1.0"), "pi",
        )

    def test_non_pi_slugs_to_linux(self) -> None:
        """A non-Pi model string slugs to 'linux'."""
        self.assertEqual(_platform_slug("Generic x86_64 machine"), "linux")

    def test_case_insensitive(self) -> None:
        """Matching is case-insensitive."""
        self.assertEqual(_platform_slug("RASPBERRY PI 5"), "pi5")


# ---------------------------------------------------------------------------
# ThermalReading serialization
# ---------------------------------------------------------------------------

class TestThermalReadingJson(unittest.TestCase):
    """ThermalReading.to_json() produces parseable, field-preserving JSON."""

    def _sample(self) -> ThermalReading:
        """Return a fully populated sample reading for round-trip tests."""
        return ThermalReading(
            ts="2026-04-11T20:30:00Z",
            node_id="hub",
            platform="pi5",
            cpu_temp_c=56.7,
            fan_rpm=2739,
            fan_pwm_step=1,
            fan_declared_present=True,
            load_1m=0.42,
            load_5m=0.50,
            load_15m=0.58,
            uptime_s=186432.0,
            extra={"throttled_flags": "0x0", "volts_core": 0.8715},
        )

    def test_round_trip(self) -> None:
        """JSON encoded then decoded matches the original dict."""
        reading: ThermalReading = self._sample()
        data: dict[str, Any] = json.loads(reading.to_json())
        self.assertEqual(data, asdict(reading))

    def test_null_fields_serialize_as_json_null(self) -> None:
        """Missing sensor values serialize to JSON null, not omitted."""
        reading: ThermalReading = ThermalReading(
            ts="2026-04-11T20:30:00Z",
            node_id="mbclock",
            platform="pi4",
            cpu_temp_c=None,
            fan_rpm=None,
            fan_pwm_step=None,
            fan_declared_present=True,
            load_1m=0.0,
            load_5m=0.0,
            load_15m=0.0,
            uptime_s=120.0,
            extra={},
        )
        data: dict[str, Any] = json.loads(reading.to_json())
        self.assertIsNone(data["cpu_temp_c"])
        self.assertIsNone(data["fan_rpm"])
        self.assertIsNone(data["fan_pwm_step"])
        self.assertTrue(data["fan_declared_present"])

    def test_compact_form(self) -> None:
        """to_json emits compact form (no whitespace between separators)."""
        raw: str = self._sample().to_json()
        self.assertNotIn(": ", raw)
        self.assertNotIn(", ", raw)


# ---------------------------------------------------------------------------
# Sysfs readers — against fabricated trees
# ---------------------------------------------------------------------------

class TestSysfsReaders(unittest.TestCase):
    """Sysfs readers should parse real file content and degrade gracefully."""

    def setUp(self) -> None:
        """Create a temporary directory to stand in for /sys and /proc."""
        self._tmp: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self._root: str = self._tmp.name

    def tearDown(self) -> None:
        """Remove the temporary directory."""
        self._tmp.cleanup()

    # ---- cpu temp ------------------------------------------------------

    def test_cpu_temp_valid_value(self) -> None:
        """Valid millicelsius file → correct Celsius conversion."""
        path: str = os.path.join(self._root, "thermal_zone0", "temp")
        _write(path, "52350\n")
        with patch.object(pts, "_THERMAL_ZONE_TEMP_PATH", path):
            self.assertAlmostEqual(_read_cpu_temp_c(), 52.35, places=3)

    def test_cpu_temp_missing_file(self) -> None:
        """Missing thermal_zone path → None (no exception)."""
        with patch.object(
            pts, "_THERMAL_ZONE_TEMP_PATH", "/nonexistent/path/xyz",
        ):
            self.assertIsNone(_read_cpu_temp_c())

    def test_cpu_temp_garbage_content(self) -> None:
        """Non-numeric content → None, no exception."""
        path: str = os.path.join(self._root, "temp-bad")
        _write(path, "not a number\n")
        with patch.object(pts, "_THERMAL_ZONE_TEMP_PATH", path):
            self.assertIsNone(_read_cpu_temp_c())

    # ---- fan rpm -------------------------------------------------------

    def _make_hwmon_tree(
        self, entries: list[tuple[str, Optional[int]]],
    ) -> str:
        """Create a fake /sys/class/hwmon directory.

        Args:
            entries: list of (hwmon_name, fan1_input_value_or_None).

        Returns:
            Root of the fake hwmon directory.
        """
        root: str = os.path.join(self._root, "hwmon")
        os.makedirs(root, exist_ok=True)
        for idx, (name, rpm) in enumerate(entries):
            hwmon_dir: str = os.path.join(root, f"hwmon{idx}")
            os.makedirs(hwmon_dir, exist_ok=True)
            _write(os.path.join(hwmon_dir, "name"), name + "\n")
            if rpm is not None:
                _write(
                    os.path.join(hwmon_dir, "fan1_input"), f"{rpm}\n",
                )
        return root

    def test_fan_rpm_found_in_pwmfan(self) -> None:
        """pwmfan entry in hwmon tree → correct RPM."""
        root: str = self._make_hwmon_tree([
            ("cpu_thermal", None),
            ("rp1_adc", None),
            ("pwmfan", 2739),
            ("rpi_volt", None),
        ])
        with patch.object(pts, "_HWMON_ROOT", root):
            self.assertEqual(_read_fan_rpm(), 2739)

    def test_fan_rpm_no_pwmfan_returns_none(self) -> None:
        """hwmon tree without pwmfan → None."""
        root: str = self._make_hwmon_tree([
            ("cpu_thermal", None),
            ("rpi_volt", None),
        ])
        with patch.object(pts, "_HWMON_ROOT", root):
            self.assertIsNone(_read_fan_rpm())

    def test_fan_rpm_missing_root(self) -> None:
        """Nonexistent hwmon root → None."""
        with patch.object(
            pts, "_HWMON_ROOT", "/nonexistent/hwmon/xyz",
        ):
            self.assertIsNone(_read_fan_rpm())

    def test_fan_rpm_pwmfan_no_fan1_input(self) -> None:
        """pwmfan dir without fan1_input file → None."""
        root: str = self._make_hwmon_tree([("pwmfan", None)])
        with patch.object(pts, "_HWMON_ROOT", root):
            self.assertIsNone(_read_fan_rpm())

    # ---- cooling device ------------------------------------------------

    def test_fan_pwm_step_valid(self) -> None:
        """Valid cur_state integer → correct parse."""
        path: str = os.path.join(self._root, "cooling", "cur_state")
        _write(path, "3\n")
        with patch.object(pts, "_COOLING_DEVICE_CUR_STATE", path):
            self.assertEqual(_read_fan_pwm_step(), 3)

    def test_fan_pwm_step_missing(self) -> None:
        """Nonexistent cooling_device path → None."""
        with patch.object(
            pts, "_COOLING_DEVICE_CUR_STATE", "/nonexistent/abc",
        ):
            self.assertIsNone(_read_fan_pwm_step())

    # ---- loadavg -------------------------------------------------------

    def test_loadavg_valid(self) -> None:
        """Valid /proc/loadavg → correct tuple."""
        path: str = os.path.join(self._root, "loadavg")
        _write(path, "0.42 0.50 0.58 1/234 5678\n")
        with patch.object(pts, "_PROC_LOADAVG", path):
            self.assertEqual(
                _read_loadavg(), (0.42, 0.50, 0.58),
            )

    def test_loadavg_missing_falls_back_to_zeros(self) -> None:
        """Missing loadavg → zero tuple (graceful degradation)."""
        with patch.object(
            pts, "_PROC_LOADAVG", "/nonexistent/loadavg",
        ):
            self.assertEqual(_read_loadavg(), (0.0, 0.0, 0.0))

    # ---- uptime --------------------------------------------------------

    def test_uptime_valid(self) -> None:
        """Valid /proc/uptime → correct first field."""
        path: str = os.path.join(self._root, "uptime")
        _write(path, "186432.25 123456.78\n")
        with patch.object(pts, "_PROC_UPTIME", path):
            self.assertAlmostEqual(_read_uptime_s(), 186432.25)

    def test_uptime_missing(self) -> None:
        """Missing uptime → 0.0."""
        with patch.object(pts, "_PROC_UPTIME", "/nonexistent/uptime"):
            self.assertEqual(_read_uptime_s(), 0.0)

    # ---- pi model ------------------------------------------------------

    def test_pi_model_strips_null_terminator(self) -> None:
        """Device-tree model strips trailing null bytes."""
        path: str = os.path.join(self._root, "model")
        with open(path, "wb") as fh:
            fh.write(b"Raspberry Pi 5 Model B Rev 1.0\x00")
        with patch.object(pts, "_DEVICE_TREE_MODEL", path):
            self.assertEqual(
                _read_pi_model(), "Raspberry Pi 5 Model B Rev 1.0",
            )

    def test_pi_model_missing_returns_unknown(self) -> None:
        """Missing device-tree → 'unknown'."""
        with patch.object(
            pts, "_DEVICE_TREE_MODEL", "/nonexistent/model",
        ):
            self.assertEqual(_read_pi_model(), "unknown")


# ---------------------------------------------------------------------------
# vcgencmd parsing
# ---------------------------------------------------------------------------

class TestVcgencmdReaders(unittest.TestCase):
    """_run_vcgencmd and its consumers should parse real vcgencmd output."""

    def _mock_run(
        self,
        *,
        rc: int = 0,
        stdout: str = "",
        stderr: str = "",
        exc: Optional[Exception] = None,
    ) -> Any:
        """Build a subprocess.run mock returning a CompletedProcess."""
        def _inner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(
                args=args[0], returncode=rc, stdout=stdout, stderr=stderr,
            )
        return _inner

    def test_run_vcgencmd_happy_path(self) -> None:
        """Successful run returns stripped stdout."""
        with patch(
            "contrib.sensors.pi_thermal_sensor.subprocess.run",
            side_effect=self._mock_run(rc=0, stdout="throttled=0x0\n"),
        ):
            self.assertEqual(
                _run_vcgencmd(["get_throttled"]), "throttled=0x0",
            )

    def test_run_vcgencmd_nonzero_rc_returns_none(self) -> None:
        """Nonzero return code → None."""
        with patch(
            "contrib.sensors.pi_thermal_sensor.subprocess.run",
            side_effect=self._mock_run(
                rc=1, stderr="Command not registered",
            ),
        ):
            self.assertIsNone(_run_vcgencmd(["measure_fan"]))

    def test_run_vcgencmd_missing_binary_returns_none(self) -> None:
        """FileNotFoundError (no vcgencmd on PATH) → None."""
        with patch(
            "contrib.sensors.pi_thermal_sensor.subprocess.run",
            side_effect=self._mock_run(exc=FileNotFoundError("vcgencmd")),
        ):
            self.assertIsNone(_run_vcgencmd(["measure_temp"]))

    def test_run_vcgencmd_timeout_returns_none(self) -> None:
        """TimeoutExpired → None."""
        with patch(
            "contrib.sensors.pi_thermal_sensor.subprocess.run",
            side_effect=self._mock_run(
                exc=subprocess.TimeoutExpired("vcgencmd", 5),
            ),
        ):
            self.assertIsNone(_run_vcgencmd(["measure_temp"]))

    def test_throttled_flags_parses_hex(self) -> None:
        """'throttled=0x50000' → '0x50000'."""
        with patch.object(
            pts, "_run_vcgencmd", return_value="throttled=0x50000",
        ):
            self.assertEqual(_read_throttled_flags(), "0x50000")

    def test_throttled_flags_none_on_missing_equals(self) -> None:
        """Garbage without '=' → None (defensive)."""
        with patch.object(
            pts, "_run_vcgencmd", return_value="garbage without equals",
        ):
            self.assertIsNone(_read_throttled_flags())

    def test_throttled_flags_none_when_vcgencmd_unavailable(self) -> None:
        """_run_vcgencmd returning None → None."""
        with patch.object(pts, "_run_vcgencmd", return_value=None):
            self.assertIsNone(_read_throttled_flags())

    def test_core_volts_parses_value(self) -> None:
        """'volt=0.8715V' → 0.8715."""
        with patch.object(
            pts, "_run_vcgencmd", return_value="volt=0.8715V",
        ):
            self.assertAlmostEqual(_read_core_volts(), 0.8715, places=4)

    def test_core_volts_none_on_garbage(self) -> None:
        """Garbage output → None."""
        with patch.object(
            pts, "_run_vcgencmd", return_value="not a voltage",
        ):
            self.assertIsNone(_read_core_volts())


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfigLoading(unittest.TestCase):
    """_load_config should honor INI values and fall back to defaults."""

    def setUp(self) -> None:
        """Create a temp dir for config files."""
        self._tmp: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self._path: str = os.path.join(self._tmp.name, "pi_thermal.conf")

    def tearDown(self) -> None:
        """Remove the temp dir."""
        self._tmp.cleanup()

    def test_full_config(self) -> None:
        """Every field present → all values come from the file."""
        _write(self._path, (
            "[mqtt]\n"
            "broker = 192.0.2.10\n"
            "port = 1884\n"
            "\n"
            "[sensor]\n"
            "interval = 15\n"
            "node_id = test-node\n"
            "fan_declared_present = true\n"
        ))
        cfg: SensorConfig = _load_config(self._path)
        self.assertEqual(cfg.broker_host, "192.0.2.10")
        self.assertEqual(cfg.broker_port, 1884)
        self.assertEqual(cfg.interval_s, 15.0)
        self.assertEqual(cfg.node_id, "test-node")
        self.assertTrue(cfg.fan_declared_present)

    def test_missing_file_uses_defaults(self) -> None:
        """Missing config file → all module defaults, node_id from hostname."""
        cfg: SensorConfig = _load_config("/nonexistent/pi_thermal.conf")
        self.assertEqual(cfg.broker_host, pts._DEFAULT_BROKER_HOST)
        self.assertEqual(cfg.broker_port, pts._DEFAULT_BROKER_PORT)
        self.assertEqual(cfg.interval_s, pts._DEFAULT_INTERVAL_S)
        self.assertFalse(cfg.fan_declared_present)
        self.assertNotEqual(cfg.node_id, "")

    def test_partial_config_mqtt_only(self) -> None:
        """Only [mqtt] present → sensor section defaults."""
        _write(self._path, (
            "[mqtt]\n"
            "broker = 192.0.2.99\n"
        ))
        cfg: SensorConfig = _load_config(self._path)
        self.assertEqual(cfg.broker_host, "192.0.2.99")
        self.assertEqual(cfg.broker_port, pts._DEFAULT_BROKER_PORT)
        self.assertEqual(cfg.interval_s, pts._DEFAULT_INTERVAL_S)

    def test_blank_node_id_falls_back_to_hostname(self) -> None:
        """Blank node_id → lowercased short hostname."""
        _write(self._path, (
            "[sensor]\n"
            "node_id =\n"
        ))
        cfg: SensorConfig = _load_config(self._path)
        self.assertTrue(len(cfg.node_id) > 0)
        self.assertEqual(cfg.node_id, cfg.node_id.lower())
        self.assertNotIn(".", cfg.node_id)

    def test_fan_declared_present_false(self) -> None:
        """fan_declared_present = false parses as False."""
        _write(self._path, (
            "[sensor]\n"
            "fan_declared_present = false\n"
        ))
        cfg: SensorConfig = _load_config(self._path)
        self.assertFalse(cfg.fan_declared_present)


# ---------------------------------------------------------------------------
# PiThermalSensor._sample() end-to-end (no network)
# ---------------------------------------------------------------------------

class TestSampleEndToEnd(unittest.TestCase):
    """_sample() should assemble a ThermalReading from mocked readers."""

    def _make_sensor(self, fan_declared: bool = False) -> PiThermalSensor:
        """Build a PiThermalSensor without connecting."""
        return PiThermalSensor(
            broker_host="127.0.0.1",
            broker_port=1883,
            interval_s=1.0,
            node_id="unit-test",
            hostname="unit-test.local",
            fan_declared_present=fan_declared,
            pi_model="Raspberry Pi 5 Model B Rev 1.0",
            platform="pi5",
        )

    def test_sample_happy_path_pi5(self) -> None:
        """All readers succeed → every field populated, extra has vcgencmd data."""
        sensor: PiThermalSensor = self._make_sensor(fan_declared=False)
        with (
            patch.object(pts, "_read_cpu_temp_c", return_value=56.7),
            patch.object(pts, "_read_fan_rpm", return_value=2739),
            patch.object(pts, "_read_fan_pwm_step", return_value=1),
            patch.object(
                pts, "_read_loadavg", return_value=(0.42, 0.50, 0.58),
            ),
            patch.object(pts, "_read_uptime_s", return_value=186432.0),
            patch.object(
                pts, "_read_throttled_flags", return_value="0x0",
            ),
            patch.object(pts, "_read_core_volts", return_value=0.8715),
        ):
            reading: ThermalReading = sensor._sample()
        self.assertEqual(reading.node_id, "unit-test")
        self.assertEqual(reading.platform, "pi5")
        self.assertEqual(reading.cpu_temp_c, 56.7)
        self.assertEqual(reading.fan_rpm, 2739)
        self.assertEqual(reading.fan_pwm_step, 1)
        self.assertFalse(reading.fan_declared_present)
        self.assertEqual(reading.load_1m, 0.42)
        self.assertEqual(reading.extra["throttled_flags"], "0x0")
        self.assertEqual(reading.extra["volts_core"], 0.8715)
        self.assertEqual(
            reading.extra["model"], "Raspberry Pi 5 Model B Rev 1.0",
        )

    def test_sample_pi4_null_fan_fields(self) -> None:
        """Pi 4 path: fan readers return None → fields are None, declared still flows."""
        sensor: PiThermalSensor = self._make_sensor(fan_declared=True)
        with (
            patch.object(pts, "_read_cpu_temp_c", return_value=51.6),
            patch.object(pts, "_read_fan_rpm", return_value=None),
            patch.object(pts, "_read_fan_pwm_step", return_value=None),
            patch.object(
                pts, "_read_loadavg", return_value=(0.0, 0.0, 0.0),
            ),
            patch.object(pts, "_read_uptime_s", return_value=120.0),
            patch.object(pts, "_read_throttled_flags", return_value=None),
            patch.object(pts, "_read_core_volts", return_value=None),
        ):
            reading: ThermalReading = sensor._sample()
        self.assertEqual(reading.cpu_temp_c, 51.6)
        self.assertIsNone(reading.fan_rpm)
        self.assertIsNone(reading.fan_pwm_step)
        self.assertTrue(reading.fan_declared_present)
        self.assertNotIn("throttled_flags", reading.extra)
        self.assertNotIn("volts_core", reading.extra)

    def test_sample_ts_is_iso8601_utc(self) -> None:
        """ts is ISO 8601 UTC with trailing Z."""
        sensor: PiThermalSensor = self._make_sensor()
        with (
            patch.object(pts, "_read_cpu_temp_c", return_value=50.0),
            patch.object(pts, "_read_fan_rpm", return_value=None),
            patch.object(pts, "_read_fan_pwm_step", return_value=None),
            patch.object(
                pts, "_read_loadavg", return_value=(0.0, 0.0, 0.0),
            ),
            patch.object(pts, "_read_uptime_s", return_value=0.0),
            patch.object(pts, "_read_throttled_flags", return_value=None),
            patch.object(pts, "_read_core_volts", return_value=None),
        ):
            reading: ThermalReading = sensor._sample()
        self.assertTrue(reading.ts.endswith("Z"))
        self.assertRegex(
            reading.ts,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        )


# ---------------------------------------------------------------------------
# Topic construction
# ---------------------------------------------------------------------------

class TestTopicConstruction(unittest.TestCase):
    """Topic strings should follow the agreed architecture."""

    def _make_sensor(self, node_id: str) -> PiThermalSensor:
        """Build a sensor with the given node_id, no connect."""
        return PiThermalSensor(
            broker_host="127.0.0.1",
            broker_port=1883,
            interval_s=30.0,
            node_id=node_id,
            hostname=node_id,
            fan_declared_present=False,
            pi_model="Raspberry Pi 5",
            platform="pi5",
        )

    def test_thermal_topic_signal_class_first(self) -> None:
        """Hardware thermal topic is glowup/hardware/thermal/<node_id>."""
        s: PiThermalSensor = self._make_sensor("broker-2")
        self.assertEqual(
            s._thermal_topic, "glowup/hardware/thermal/broker-2",
        )

    def test_status_topic_follows_node_prefix(self) -> None:
        """Status topic is glowup/node/<node_id>/status."""
        s: PiThermalSensor = self._make_sensor("hub")
        self.assertEqual(s._status_topic, "glowup/node/hub/status")

    def test_capability_topic_follows_node_prefix(self) -> None:
        """Capability topic is glowup/node/<node_id>/capability."""
        s: PiThermalSensor = self._make_sensor("mbclock")
        self.assertEqual(
            s._capability_topic, "glowup/node/mbclock/capability",
        )


if __name__ == "__main__":
    unittest.main()
