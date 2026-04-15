"""Tests for power signal recording via _on_remote_signal.

When the Zigbee adapter subprocess publishes a power-related signal
to ``glowup/signals/{device}:{property}``, the server's
``_on_remote_signal`` callback must feed it to PowerLogger.record().

This contract was broken during the adapter process isolation refactor:
the adapter's ``_power_logger`` field became unreachable across the
process boundary, and the server-side callback was never wired to
compensate.  These tests encode the contract so it can't regress.

The tests exercise the actual ``_on_remote_signal`` function extracted
from server.py's startup path.  A real PowerLogger (temp SQLite DB) is
used — no mocking of the recording path.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from typing import Any, Optional

from infrastructure.power_logger import (
    MIN_WRITE_INTERVAL,
    POWER_PROPERTIES,
    PowerLogger,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Signal topic prefix — matches TOPIC_SIGNALS in process_base.py.
SIGNAL_TOPIC_PREFIX: str = "glowup/signals/"


# ---------------------------------------------------------------------------
# Helpers — reproduce _on_remote_signal's contract
# ---------------------------------------------------------------------------

def make_mqtt_message(topic: str, payload: Any) -> SimpleNamespace:
    """Build a fake paho MQTT message object.

    Args:
        topic:   MQTT topic string.
        payload: Value to JSON-encode as the message payload.

    Returns:
        Object with ``.topic`` and ``.payload`` attributes.
    """
    return SimpleNamespace(
        topic=topic,
        payload=json.dumps(payload).encode(),
    )


def simulate_on_remote_signal(
    message: SimpleNamespace,
    power_logger: Optional[PowerLogger],
    signal_bus: Optional[Any] = None,
    liveness_holder: Optional[list[Optional[float]]] = None,
) -> None:
    """Reproduce exactly what _on_remote_signal in server.py does.

    This must be kept in sync with the real implementation.  If the
    real function changes, update this and the tests.

    Args:
        message:         Fake paho MQTT message.
        power_logger:    PowerLogger instance (or None).
        signal_bus:      Optional SignalBus (ignored in these tests).
        liveness_holder: Single-slot list used as a mutable "out
                         parameter" for the broker-2 liveness
                         timestamp.  When the real callback would
                         update ``GlowUpRequestHandler.broker2_signals_last_ts``,
                         this simulator writes ``time.time()`` into
                         ``liveness_holder[0]`` instead.  Pass
                         ``[None]`` to observe the stamp; pass
                         ``None`` to skip tracking.
    """
    parts: list[str] = message.topic.split("/", 2)
    if len(parts) < 3:
        return
    sig_name: str = parts[2]

    # Broker-2 liveness stamp — every non-time signal counts.
    # Stamp happens before JSON parse so malformed payloads still
    # register as "broker-2 is alive and publishing."
    if liveness_holder is not None and not sig_name.startswith("time:"):
        liveness_holder[0] = time.time()

    try:
        sig_value: Any = json.loads(message.payload)
    except (json.JSONDecodeError, ValueError):
        return
    if signal_bus is not None:
        signal_bus.write_local(sig_name, sig_value)

    # --- Power-recording contract ---
    # Power-related signals must be fed to PowerLogger.
    if power_logger is not None:
        sig_parts: list[str] = sig_name.split(":", 1)
        if len(sig_parts) == 2:
            try:
                power_logger.record(
                    sig_parts[0], sig_parts[1], float(sig_value),
                )
            except (ValueError, TypeError):
                pass


def extract_on_remote_signal_source() -> str:
    """Read _on_remote_signal's source from server.py.

    Returns the function body as a string for contract verification.
    """
    import re
    server_path: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server.py",
    )
    with open(server_path) as f:
        source: str = f.read()

    # Extract the _on_remote_signal function body.
    # It starts at "def _on_remote_signal(" and ends at the next
    # line with equal or less indentation that isn't blank.
    match = re.search(
        r"(def _on_remote_signal\(.*?\n(?:(?:[ \t]+.*|[ \t]*)\n)*)",
        source,
    )
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestRemoteSignalPowerRecording(unittest.TestCase):
    """Verify that power signals from adapter subprocesses get recorded."""

    def setUp(self) -> None:
        """Create a temp PowerLogger for each test."""
        self._tmpfile = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False,
        )
        self._path: str = self._tmpfile.name
        self._tmpfile.close()
        self.pl: PowerLogger = PowerLogger(db_path=self._path)
        self.pl._last_write.clear()

    def tearDown(self) -> None:
        """Clean up temp DB."""
        self.pl.close()
        os.unlink(self._path)

    def test_power_signal_recorded(self) -> None:
        """A power signal on glowup/signals/ML_Power:power must produce a DB row."""
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}ML_Power:power", 154.7,
        )
        simulate_on_remote_signal(msg, self.pl)
        rows = self.pl.query(device="ML_Power", hours=1, resolution=1)
        self.assertGreater(len(rows), 0, "PowerLogger has no rows after power signal")
        self.assertAlmostEqual(rows[0]["power"], 154.7, places=1)

    def test_voltage_signal_recorded(self) -> None:
        """Voltage signals are also power-related and must be recorded."""
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}ML_Power:voltage", 121.9,
        )
        simulate_on_remote_signal(msg, self.pl)
        # Force write window for next property.
        self.pl._last_write["ML_Power"] = 0.0
        msg2 = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}ML_Power:power", 158.0,
        )
        simulate_on_remote_signal(msg2, self.pl)
        rows = self.pl.query(device="ML_Power", hours=1, resolution=1)
        found_voltage = any(r.get("voltage") for r in rows)
        self.assertTrue(found_voltage, "Voltage was not recorded")

    def test_energy_signal_recorded(self) -> None:
        """Energy (kWh) signals must be recorded."""
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}ML_Power:energy", 11.2,
        )
        simulate_on_remote_signal(msg, self.pl)
        devices = self.pl.devices()
        self.assertIn("ML_Power", devices)

    def test_non_power_signal_ignored(self) -> None:
        """Non-power signals (occupancy, temperature) must not create rows."""
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}Office:occupancy", 1.0,
        )
        simulate_on_remote_signal(msg, self.pl)
        self.assertEqual(
            self.pl.devices(), [],
            "Non-power signal created a DB row",
        )

    def test_string_payload_ignored(self) -> None:
        """Non-numeric payloads must not crash or record."""
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}ML_Power:power", "ON",
        )
        simulate_on_remote_signal(msg, self.pl)
        # "ON" can't be float() — should be silently ignored.
        self.assertEqual(self.pl.devices(), [])

    def test_no_power_logger_no_crash(self) -> None:
        """When power_logger is None, signal processing must not crash."""
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}ML_Power:power", 154.7,
        )
        # Should not raise.
        simulate_on_remote_signal(msg, None)

    def test_malformed_topic_no_crash(self) -> None:
        """Topics without enough parts are silently skipped."""
        msg = make_mqtt_message("glowup/signals", 154.7)
        simulate_on_remote_signal(msg, self.pl)
        # Two-part topic — no signal name, should be skipped.
        # (The prefix split produces exactly 2 parts.)

    def test_signal_without_colon_no_crash(self) -> None:
        """Signal names without a colon separator are silently skipped."""
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}ML_Power_power", 154.7,
        )
        simulate_on_remote_signal(msg, self.pl)
        self.assertEqual(
            self.pl.devices(), [],
            "Signal without colon separator should not record",
        )

    def test_multiple_devices_recorded_independently(self) -> None:
        """Signals from different devices create independent rows."""
        for device, power in [("ML_Power", 158.0), ("LRTV", 95.0)]:
            msg = make_mqtt_message(
                f"{SIGNAL_TOPIC_PREFIX}{device}:power", power,
            )
            simulate_on_remote_signal(msg, self.pl)
        devices = self.pl.devices()
        self.assertIn("ML_Power", devices)
        self.assertIn("LRTV", devices)

    def test_all_power_properties_accepted(self) -> None:
        """Every property in POWER_PROPERTIES must be recordable."""
        for prop in POWER_PROPERTIES:
            self.pl._last_write.clear()
            msg = make_mqtt_message(
                f"{SIGNAL_TOPIC_PREFIX}TestPlug:{prop}", 42.0,
            )
            simulate_on_remote_signal(msg, self.pl)
        self.assertIn("TestPlug", self.pl.devices())


class TestServerCodeFeedsPowerLogger(unittest.TestCase):
    """Ground-truth test against the real server.py _on_remote_signal.

    Reads the actual source of _on_remote_signal from server.py and
    verifies it contains the power_logger.record() call.  If the
    wiring is missing, this test fails — it is the canary.

    This is a source-level contract test because _on_remote_signal
    is a closure inside server.py's startup path and cannot be
    imported or called in isolation.
    """

    def test_on_remote_signal_calls_power_logger_record(self) -> None:
        """_on_remote_signal must contain power_logger.record() call.

        This test will FAIL if the wiring between signal reception
        and power_logger.record() is missing — which is the exact
        bug that broke the power dashboard after the process refactor.
        """
        source: str = extract_on_remote_signal_source()
        self.assertTrue(
            len(source) > 0,
            "Could not find _on_remote_signal in server.py",
        )
        self.assertIn(
            "power_logger",
            source,
            "server.py _on_remote_signal does not reference power_logger — "
            "power dashboard will show no data",
        )
        self.assertIn(
            ".record(",
            source,
            "server.py _on_remote_signal does not call .record() — "
            "power signals are received but never stored",
        )


class TestBroker2LivenessStamp(unittest.TestCase):
    """Verify the /api/home/health broker-2 liveness probe wiring.

    The hub reports zigbee/BLE health by observing whether any
    non-time signal has arrived on glowup/signals/# recently.  The
    real callback in server.py stamps
    ``GlowUpRequestHandler.broker2_signals_last_ts`` on every
    qualifying message, and handlers/dashboard.py compares that
    timestamp against ``BROKER2_SIGNALS_STALE_SEC``.

    These tests verify both the filter logic (via the simulator)
    and the name-level contract between the producer side
    (server.py) and the consumer side (handlers/dashboard.py).  If
    anyone renames the attribute without touching the other file,
    these tests fire.
    """

    def test_device_signal_stamps_liveness(self) -> None:
        """A device:prop signal must bump the liveness timestamp."""
        holder: list[Optional[float]] = [None]
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}BYIR:power", 0.7,
        )
        before: float = time.time()
        simulate_on_remote_signal(msg, None, liveness_holder=holder)
        self.assertIsNotNone(
            holder[0],
            "device:prop signal did not stamp the liveness holder",
        )
        self.assertGreaterEqual(holder[0], before)

    def test_time_signal_does_not_stamp_liveness(self) -> None:
        """time:* signals originate on the hub and must NOT stamp.

        If time signals stamped the liveness holder, a silent
        broker-2 outage would still report healthy because the hub
        keeps publishing time signals to itself.
        """
        holder: list[Optional[float]] = [None]
        msg = make_mqtt_message(
            f"{SIGNAL_TOPIC_PREFIX}time:epoch", 1776253652.0,
        )
        simulate_on_remote_signal(msg, None, liveness_holder=holder)
        self.assertIsNone(
            holder[0],
            "time:* signal incorrectly stamped broker-2 liveness — "
            "a silent broker-2 outage would be hidden",
        )

    def test_malformed_payload_still_stamps(self) -> None:
        """A non-parseable payload must still count as 'broker-2 alive'.

        The stamp happens before JSON parsing so a broken device
        message still proves broker-2's network path is intact.
        """
        holder: list[Optional[float]] = [None]
        msg = SimpleNamespace(
            topic=f"{SIGNAL_TOPIC_PREFIX}BYIR:power",
            payload=b"not-json",
        )
        simulate_on_remote_signal(msg, None, liveness_holder=holder)
        self.assertIsNotNone(
            holder[0],
            "malformed broker-2 payload failed to stamp liveness — "
            "a noisy-but-broken producer would report unhealthy",
        )

    def test_handler_attribute_and_constant_exist(self) -> None:
        """server.py and handlers/dashboard.py must agree on names.

        The hub's health handler reads
        ``GlowUpRequestHandler.broker2_signals_last_ts`` and compares
        against ``BROKER2_SIGNALS_STALE_SEC``.  If either identifier
        is renamed in one file without the other, the liveness
        probe silently reverts to always-False.
        """
        import server
        import handlers.dashboard as dashboard_mod
        self.assertTrue(
            hasattr(server.GlowUpRequestHandler, "broker2_signals_last_ts"),
            "server.GlowUpRequestHandler missing broker2_signals_last_ts — "
            "health probe will never see a stamp",
        )
        self.assertTrue(
            hasattr(dashboard_mod, "BROKER2_SIGNALS_STALE_SEC"),
            "handlers/dashboard.py missing BROKER2_SIGNALS_STALE_SEC — "
            "health probe has no staleness threshold",
        )
        self.assertIsInstance(
            dashboard_mod.BROKER2_SIGNALS_STALE_SEC, (int, float),
        )
        self.assertGreater(dashboard_mod.BROKER2_SIGNALS_STALE_SEC, 0)

    def test_server_callback_stamps_in_source(self) -> None:
        """server.py's real _on_remote_signal must contain the stamp.

        Source-level check: if the stamp line is ever deleted or
        the field is renamed without also renaming the reader, the
        simulator above is useless because it only tests its own
        copy of the logic.  This nails the real file.
        """
        source: str = extract_on_remote_signal_source()
        self.assertIn(
            "broker2_signals_last_ts",
            source,
            "server.py _on_remote_signal does not stamp "
            "broker2_signals_last_ts — health probe is dead",
        )
        self.assertIn(
            'not sig_name.startswith("time:")',
            source,
            "server.py _on_remote_signal does not filter time:* "
            "signals — a silent broker-2 outage will report healthy",
        )


if __name__ == "__main__":
    unittest.main()
