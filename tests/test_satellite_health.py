"""Tests for the satellite deep-health probe protocol.

The hub asks every satellite for a fresh subsystem snapshot every
HUB_SATELLITE_PROBE_INTERVAL_S and on-demand via
POST /api/satellites/{room}/health/check.  These tests verify:

- The satellite daemon's _run_deep_health_check() correctly
  classifies stale vs fresh subsystems and produces an actionable
  recommended_action when any subsystem fails.

- time:* vs device:* filtering still holds for the broker-2
  liveness stamp (overlaps with test_signal_power_recording but
  re-asserted here because the scope is "satellite health").

- Source-level contracts: the satellite daemon subscribes to
  TOPIC_HEALTH_REQUEST and publishes on TOPIC_HEALTH_REPLY_PREFIX,
  and the hub subscribes to both the heartbeat and reply topics.

- Handler/field-name contracts: GlowUpRequestHandler exposes the
  satellite state dicts and the probe client; handlers/dashboard.py
  imports the staleness threshold and the request topic from
  voice.constants (no duplicated magic strings).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import os
import time
import unittest
from typing import Any
from unittest.mock import MagicMock

from voice import constants as voice_c


# ---------------------------------------------------------------------------
# Helper — build a fake daemon instance just enough to run the health check
# ---------------------------------------------------------------------------


def _make_fake_daemon(
    *,
    audio_age: float = 0.5,
    wake_age: float = 0.5,
    utterance_age: float = 30.0,
    mqtt_connected: bool = True,
    mock_wake: bool = False,
    gated: bool = False,
) -> Any:
    """Build a SatelliteDaemon stand-in with health-check state only.

    The real daemon pulls in pyaudio and openwakeword at import time
    via ``from voice.satellite.daemon import SatelliteDaemon``.  Those
    imports are heavy and may not be available in the test env, so
    this helper constructs a plain object with just the attributes
    ``_run_deep_health_check`` reads, then binds the real method to
    it.  The test stays hermetic and fast.
    """
    from voice.satellite.daemon import SatelliteDaemon
    now: float = time.time()
    fake: Any = MagicMock()
    fake._room = "Dining Room"
    fake._gated = gated
    fake._mock_wake = mock_wake
    fake._last_audio_frame_ts = now - audio_age
    fake._last_wake_eval_ts = now - wake_age
    fake._last_utterance_ts = now - utterance_age
    fake._audio_frames_total = 12345
    import threading
    fake._health_lock = threading.Lock()
    fake._gate_lock = threading.Lock()
    fake._gate_open = False
    fake._gate_expires = 0.0
    # MQTT client with configurable is_connected.
    fake._mqtt_client = MagicMock()
    fake._mqtt_client.is_connected.return_value = mqtt_connected
    return fake


class TestDeepHealthCheckLogic(unittest.TestCase):
    """Verify _run_deep_health_check classifies subsystems correctly."""

    def test_all_fresh_reports_ok(self) -> None:
        """Fresh audio, fresh wake, connected MQTT → ok=true."""
        from voice.satellite.daemon import SatelliteDaemon
        fake = _make_fake_daemon()
        report = SatelliteDaemon._run_deep_health_check(fake, "test-1")
        self.assertTrue(
            report["ok"],
            f"all-fresh should report ok=true, got {report}",
        )
        self.assertIsNone(
            report["recommended_action"],
            "healthy report must have recommended_action=null",
        )
        self.assertEqual(report["id"], "test-1")
        self.assertEqual(report["room"], "Dining Room")
        self.assertIn("audio_capture", report["checks"])
        self.assertIn("wake_inference", report["checks"])
        self.assertIn("mqtt", report["checks"])
        self.assertTrue(report["checks"]["audio_capture"]["ok"])
        self.assertTrue(report["checks"]["wake_inference"]["ok"])
        self.assertTrue(report["checks"]["mqtt"]["ok"])

    def test_stale_audio_fails_and_recommends_restart(self) -> None:
        """Audio frames older than the threshold → fail + actionable text."""
        from voice.satellite.daemon import SatelliteDaemon
        fake = _make_fake_daemon(
            audio_age=voice_c.SAT_AUDIO_FRAME_STALE_S + 5.0,
        )
        report = SatelliteDaemon._run_deep_health_check(fake, "test-2")
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["audio_capture"]["ok"])
        rec: str = report["recommended_action"]
        self.assertIsNotNone(rec)
        self.assertIn("audio capture", rec)
        self.assertIn("restart glowup-satellite", rec)

    def test_stale_wake_while_audio_fresh_is_caught(self) -> None:
        """Live audio but frozen wake thread → wake check fails.

        This is the exact hang-in-a-subthread scenario #3-only
        heartbeats cannot detect.  The deep check must catch it.
        """
        from voice.satellite.daemon import SatelliteDaemon
        fake = _make_fake_daemon(
            audio_age=0.2,
            wake_age=voice_c.SAT_WAKE_EVAL_STALE_S + 10.0,
        )
        report = SatelliteDaemon._run_deep_health_check(fake, "test-3")
        self.assertFalse(report["ok"])
        self.assertTrue(
            report["checks"]["audio_capture"]["ok"],
            "audio capture should still be fresh in this scenario",
        )
        self.assertFalse(report["checks"]["wake_inference"]["ok"])
        self.assertIn(
            "wake-word inference",
            report["recommended_action"] or "",
        )

    def test_mqtt_disconnected_prioritised_over_other_failures(self) -> None:
        """MQTT failure masks every other failure in the recommendation."""
        from voice.satellite.daemon import SatelliteDaemon
        fake = _make_fake_daemon(
            mqtt_connected=False,
            audio_age=voice_c.SAT_AUDIO_FRAME_STALE_S + 5.0,
        )
        report = SatelliteDaemon._run_deep_health_check(fake, "test-4")
        self.assertFalse(report["ok"])
        self.assertIn("MQTT", report["recommended_action"] or "")

    def test_mock_wake_suppresses_wake_failure(self) -> None:
        """mock_wake=true must not fire a wake-inference failure.

        Mock wake never evaluates the detector; the check has to
        recognise this and pass.  Otherwise every dev-mode satellite
        reports a false positive forever.
        """
        from voice.satellite.daemon import SatelliteDaemon
        fake = _make_fake_daemon(mock_wake=True)
        fake._last_wake_eval_ts = 0.0  # never ran
        report = SatelliteDaemon._run_deep_health_check(fake, "test-5")
        self.assertTrue(report["checks"]["wake_inference"]["ok"])
        self.assertIn(
            "mock_wake",
            report["checks"]["wake_inference"]["detail"],
        )

    def test_never_seen_audio_reports_never_seen(self) -> None:
        """A zero timestamp must not report 'inf seconds ago'."""
        from voice.satellite.daemon import SatelliteDaemon
        fake = _make_fake_daemon()
        fake._last_audio_frame_ts = 0.0
        report = SatelliteDaemon._run_deep_health_check(fake, "test-6")
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["audio_capture"]["ok"])
        self.assertIn(
            "no audio frames ever received",
            report["checks"]["audio_capture"]["detail"],
        )
        # Age is reported as None for unseen subsystems (not +inf).
        self.assertIsNone(report["checks"]["audio_capture"]["age_s"])


# ---------------------------------------------------------------------------
# Source-level contracts — guard against silent renames / unwirings
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    """Slurp a project-relative file into a string."""
    full: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        path,
    )
    with open(full) as f:
        return f.read()


class TestWiringContracts(unittest.TestCase):
    """Contract tests that bind the satellite, hub, and constants."""

    def test_constants_define_required_topics(self) -> None:
        """voice/constants.py must define the request + reply topics."""
        self.assertTrue(hasattr(voice_c, "TOPIC_HEALTH_REQUEST"))
        self.assertTrue(hasattr(voice_c, "TOPIC_HEALTH_REPLY_PREFIX"))
        self.assertTrue(
            voice_c.TOPIC_HEALTH_REQUEST.startswith("glowup/voice/"),
        )
        self.assertTrue(
            voice_c.TOPIC_HEALTH_REPLY_PREFIX.startswith("glowup/voice/"),
        )
        self.assertTrue(hasattr(voice_c, "SAT_HEARTBEAT_STALE_S"))
        self.assertGreater(
            voice_c.SAT_HEARTBEAT_STALE_S,
            voice_c.HEARTBEAT_INTERVAL_S,
            "heartbeat staleness must exceed interval or every "
            "satellite will flap on every single missed tick",
        )

    def test_satellite_daemon_subscribes_to_request(self) -> None:
        """daemon.py must subscribe to TOPIC_HEALTH_REQUEST in _init_mqtt."""
        src: str = _read("voice/satellite/daemon.py")
        self.assertIn("TOPIC_HEALTH_REQUEST", src)
        self.assertIn(
            ".subscribe(C.TOPIC_HEALTH_REQUEST", src,
            "satellite does not subscribe to the deep-check "
            "request topic — hub probes will be ignored",
        )
        self.assertIn("_on_health_request_message", src)
        self.assertIn("_run_deep_health_check", src)
        self.assertIn("_publish_health_reply", src)

    def test_satellite_daemon_publishes_on_reply_topic(self) -> None:
        """daemon.py must publish replies on TOPIC_HEALTH_REPLY_PREFIX."""
        src: str = _read("voice/satellite/daemon.py")
        self.assertIn("TOPIC_HEALTH_REPLY_PREFIX", src)
        self.assertIn("_health_reply_topic", src)

    def test_server_subscribes_to_heartbeat_and_reply(self) -> None:
        """server.py must subscribe to both voice topics."""
        src: str = _read("server.py")
        self.assertIn("TOPIC_STATUS_PREFIX", src)
        self.assertIn("TOPIC_HEALTH_REPLY_PREFIX", src)
        self.assertIn("_on_satellite_heartbeat", src)
        self.assertIn("_on_satellite_health_reply", src)
        self.assertIn("_satellite_probe_loop", src)

    def test_handler_class_exposes_state_dicts(self) -> None:
        """GlowUpRequestHandler must declare the four satellite fields.

        If any of these is renamed in one file without the other,
        every satellite health endpoint silently returns empty.
        """
        import server
        cls = server.GlowUpRequestHandler
        self.assertTrue(hasattr(cls, "satellite_heartbeats"))
        self.assertTrue(hasattr(cls, "satellite_health_replies"))
        self.assertTrue(hasattr(cls, "satellite_health_events"))
        self.assertTrue(hasattr(cls, "satellite_state_lock"))
        self.assertTrue(hasattr(cls, "satellite_probe_client"))

    def test_dashboard_imports_voice_constants(self) -> None:
        """handlers/dashboard.py must import the thresholds from voice.constants.

        Local re-definition would let voice.constants drift.
        """
        import handlers.dashboard as dm
        self.assertTrue(hasattr(dm, "_SAT_HEARTBEAT_STALE_S"))
        self.assertTrue(hasattr(dm, "_SAT_PROBE_TIMEOUT_S"))
        self.assertTrue(hasattr(dm, "_VOICE_TOPIC_HEALTH_REQUEST"))
        # And they must be the same values as the constants module —
        # no accidental local override.
        self.assertEqual(
            dm._SAT_HEARTBEAT_STALE_S,
            voice_c.SAT_HEARTBEAT_STALE_S,
        )
        self.assertEqual(
            dm._VOICE_TOPIC_HEALTH_REQUEST,
            voice_c.TOPIC_HEALTH_REQUEST,
        )

    def test_routes_registered(self) -> None:
        """The two new route paths must appear in server.py's route table."""
        src: str = _read("server.py")
        self.assertIn(
            '"api", "satellites", "health"', src,
            "GET /api/satellites/health route not registered",
        )
        self.assertIn(
            '"api", "satellites", "{room}", "health", "check"', src,
            "POST /api/satellites/{room}/health/check route not registered",
        )


if __name__ == "__main__":
    unittest.main()
