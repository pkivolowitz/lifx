"""Tests for emitters.audio_out — AudioOutEmitter unit tests.

Validates emitter registration, construction, parameter handling,
frame acceptance/rejection, mute toggle, capabilities declaration,
and lifecycle (open/close) without requiring actual audio hardware.

The audio stream is mocked where necessary to avoid PortAudio
dependencies in CI environments.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import unittest
from unittest.mock import patch, MagicMock
from typing import Any

from emitters import create_emitter, get_registry, get_emitter_types
from emitters.audio_out import (
    AudioOutEmitter,
    AMPLITUDE_GATE,
    FREQ_DEFAULT,
    FREQ_MIN,
    FREQ_MAX,
    MASTER_VOLUME_DEFAULT,
    PORTAMENTO_TC_DEFAULT,
    SAMPLE_RATE,
    VIBRATO_RATE_DEFAULT,
    VIBRATO_DEPTH_DEFAULT,
    VIBRATO_AMP_DEPTH_DEFAULT,
)


class TestAudioOutRegistration(unittest.TestCase):
    """Verify AudioOutEmitter is auto-registered in the emitter registry."""

    def test_registered_in_registry(self) -> None:
        """audio_out must appear in the global emitter registry."""
        registry = get_registry()
        self.assertIn("audio_out", registry)
        self.assertIs(registry["audio_out"], AudioOutEmitter)

    def test_in_type_list(self) -> None:
        """audio_out must appear in the sorted type list."""
        self.assertIn("audio_out", get_emitter_types())

    def test_create_emitter_by_type(self) -> None:
        """create_emitter('audio_out', ...) must return an AudioOutEmitter."""
        emitter = create_emitter("audio_out", "test", {})
        self.assertIsInstance(emitter, AudioOutEmitter)


class TestAudioOutConstruction(unittest.TestCase):
    """Test construction and parameter initialization."""

    def test_default_params(self) -> None:
        """Default parameters match module constants."""
        e = AudioOutEmitter("test", {})
        self.assertAlmostEqual(e.master_volume, MASTER_VOLUME_DEFAULT)
        self.assertAlmostEqual(e.portamento, PORTAMENTO_TC_DEFAULT)
        self.assertAlmostEqual(e.vibrato_rate, VIBRATO_RATE_DEFAULT)
        self.assertAlmostEqual(e.vibrato_depth, VIBRATO_DEPTH_DEFAULT)
        self.assertAlmostEqual(e.vibrato_amp_depth, VIBRATO_AMP_DEPTH_DEFAULT)

    def test_config_override(self) -> None:
        """Config dict overrides default parameters."""
        e = AudioOutEmitter("test", {
            "master_volume": 0.8,
            "portamento": 0.1,
            "vibrato_rate": 7.0,
        })
        self.assertAlmostEqual(e.master_volume, 0.8)
        self.assertAlmostEqual(e.portamento, 0.1)
        self.assertAlmostEqual(e.vibrato_rate, 7.0)

    def test_param_clamping(self) -> None:
        """Parameters are clamped to their declared ranges."""
        e = AudioOutEmitter("test", {
            "master_volume": 5.0,   # max is 1.0
            "vibrato_rate": -1.0,   # min is 0.0
        })
        self.assertLessEqual(e.master_volume, 1.0)
        self.assertGreaterEqual(e.vibrato_rate, 0.0)

    def test_name_preserved(self) -> None:
        """Instance name is stored correctly."""
        e = AudioOutEmitter("bed:speaker", {})
        self.assertEqual(e.name, "bed:speaker")

    def test_initial_state(self) -> None:
        """Emitter starts unmuted, not open, with default frequency."""
        e = AudioOutEmitter("test", {})
        self.assertFalse(e.muted)
        self.assertFalse(e._is_open)
        self.assertAlmostEqual(e._target_freq, FREQ_DEFAULT)
        self.assertAlmostEqual(e._target_amp, 0.0)


class TestAudioOutOnEmit(unittest.TestCase):
    """Test frame acceptance and rejection logic."""

    def setUp(self) -> None:
        """Create a fresh emitter for each test."""
        self.emitter = AudioOutEmitter("test", {})

    def test_accept_valid_frame(self) -> None:
        """Valid dict frame with frequency and amplitude is accepted."""
        result: bool = self.emitter.on_emit(
            {"frequency": 440.0, "amplitude": 0.5}, {}
        )
        self.assertTrue(result)
        self.assertAlmostEqual(self.emitter._target_freq, 440.0)
        self.assertAlmostEqual(self.emitter._target_amp, 0.5)

    def test_partial_frame_frequency_only(self) -> None:
        """Frame with only frequency updates frequency, leaves amplitude."""
        self.emitter.on_emit({"frequency": 220.0, "amplitude": 0.7}, {})
        self.emitter.on_emit({"frequency": 880.0}, {})
        self.assertAlmostEqual(self.emitter._target_freq, 880.0)
        self.assertAlmostEqual(self.emitter._target_amp, 0.7)

    def test_partial_frame_amplitude_only(self) -> None:
        """Frame with only amplitude updates amplitude, leaves frequency."""
        self.emitter.on_emit({"frequency": 330.0, "amplitude": 0.3}, {})
        self.emitter.on_emit({"amplitude": 0.9}, {})
        self.assertAlmostEqual(self.emitter._target_freq, 330.0)
        self.assertAlmostEqual(self.emitter._target_amp, 0.9)

    def test_reject_non_dict_frame(self) -> None:
        """Non-dict frames are rejected with False."""
        self.assertFalse(self.emitter.on_emit("not a dict", {}))
        self.assertFalse(self.emitter.on_emit(42, {}))
        self.assertFalse(self.emitter.on_emit([1, 2, 3], {}))

    def test_frequency_clamped_low(self) -> None:
        """Frequency below FREQ_MIN is clamped."""
        self.emitter.on_emit({"frequency": 1.0}, {})
        self.assertAlmostEqual(self.emitter._target_freq, FREQ_MIN)

    def test_frequency_clamped_high(self) -> None:
        """Frequency above FREQ_MAX is clamped."""
        self.emitter.on_emit({"frequency": 99999.0}, {})
        self.assertAlmostEqual(self.emitter._target_freq, FREQ_MAX)

    def test_amplitude_clamped(self) -> None:
        """Amplitude is clamped to [0.0, 1.0]."""
        self.emitter.on_emit({"amplitude": -0.5}, {})
        self.assertAlmostEqual(self.emitter._target_amp, 0.0)

        self.emitter.on_emit({"amplitude": 2.0}, {})
        self.assertAlmostEqual(self.emitter._target_amp, 1.0)

    def test_empty_dict_accepted(self) -> None:
        """Empty dict is a valid frame (no-op, nothing updated)."""
        self.emitter.on_emit({"frequency": 440.0, "amplitude": 0.5}, {})
        result: bool = self.emitter.on_emit({}, {})
        self.assertTrue(result)
        # Values unchanged.
        self.assertAlmostEqual(self.emitter._target_freq, 440.0)
        self.assertAlmostEqual(self.emitter._target_amp, 0.5)


class TestAudioOutMute(unittest.TestCase):
    """Test mute/unmute toggle."""

    def test_toggle_mute(self) -> None:
        """toggle_mute alternates between muted and unmuted."""
        e = AudioOutEmitter("test", {})
        self.assertFalse(e.muted)

        result: bool = e.toggle_mute()
        self.assertTrue(result)
        self.assertTrue(e.muted)

        result = e.toggle_mute()
        self.assertFalse(result)
        self.assertFalse(e.muted)

    def test_mute_property(self) -> None:
        """muted property reflects internal state."""
        e = AudioOutEmitter("test", {})
        self.assertFalse(e.muted)
        e._muted = True
        self.assertTrue(e.muted)


class TestAudioOutCapabilities(unittest.TestCase):
    """Test capability declaration."""

    def test_capabilities_frame_types(self) -> None:
        """Capabilities must declare 'scalar' as accepted frame type."""
        e = AudioOutEmitter("test", {})
        caps = e.capabilities()
        self.assertIn("scalar", caps.accepted_frame_types)

    def test_capabilities_max_rate(self) -> None:
        """Capabilities must declare a reasonable max rate."""
        e = AudioOutEmitter("test", {})
        caps = e.capabilities()
        self.assertGreater(caps.max_rate_hz, 0.0)

    def test_capabilities_extra_fields(self) -> None:
        """Capabilities must include sample_rate and channels in extra."""
        e = AudioOutEmitter("test", {})
        caps = e.capabilities()
        self.assertEqual(caps.extra["sample_rate"], SAMPLE_RATE)
        self.assertEqual(caps.extra["channels"], 1)

    def test_capabilities_to_dict(self) -> None:
        """Capabilities serialize to a JSON-safe dict."""
        e = AudioOutEmitter("test", {})
        d = e.capabilities().to_dict()
        self.assertIsInstance(d, dict)
        self.assertIn("accepted_frame_types", d)
        self.assertIn("max_rate_hz", d)


class TestAudioOutLifecycle(unittest.TestCase):
    """Test lifecycle with mocked audio stream."""

    @patch("emitters.audio_out.sd.OutputStream")
    def test_open_creates_stream(self, mock_stream_cls: MagicMock) -> None:
        """on_open() creates and starts an OutputStream."""
        mock_stream = MagicMock()
        mock_stream_cls.return_value = mock_stream

        e = AudioOutEmitter("test", {})
        e.on_open()

        mock_stream_cls.assert_called_once()
        mock_stream.start.assert_called_once()
        self.assertTrue(e._is_open)

    @patch("emitters.audio_out.sd.OutputStream")
    def test_close_stops_stream(self, mock_stream_cls: MagicMock) -> None:
        """on_close() stops and closes the stream."""
        mock_stream = MagicMock()
        mock_stream_cls.return_value = mock_stream

        e = AudioOutEmitter("test", {})
        e.on_open()
        e.on_close()

        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()
        self.assertFalse(e._is_open)

    @patch("emitters.audio_out.sd.OutputStream")
    def test_close_without_open(self, mock_stream_cls: MagicMock) -> None:
        """on_close() without prior on_open() does not crash."""
        e = AudioOutEmitter("test", {})
        e.on_close()  # Should not raise.
        self.assertFalse(e._is_open)

    def test_on_configure_does_not_crash(self) -> None:
        """on_configure() is a no-op and should not raise."""
        e = AudioOutEmitter("test", {})
        e.on_configure({})  # Should not raise.

    def test_on_flush_does_not_crash(self) -> None:
        """on_flush() is a no-op and should not raise."""
        e = AudioOutEmitter("test", {})
        e.on_flush()  # Should not raise.


class TestAudioOutGetStatus(unittest.TestCase):
    """Test the get_status() introspection method."""

    def test_status_fields(self) -> None:
        """get_status() returns expected fields."""
        e = AudioOutEmitter("test:speaker", {})
        status = e.get_status()
        self.assertEqual(status["name"], "test:speaker")
        self.assertEqual(status["type"], "audio_out")
        self.assertIn("params", status)
        self.assertIn("capabilities", status)
        self.assertIn("master_volume", status["params"])
        self.assertIn("vibrato_rate", status["params"])


class TestAudioOutGetParams(unittest.TestCase):
    """Test parameter introspection."""

    def test_get_params_returns_all(self) -> None:
        """get_params() returns all declared parameters."""
        e = AudioOutEmitter("test", {})
        params = e.get_params()
        self.assertIn("master_volume", params)
        self.assertIn("portamento", params)
        self.assertIn("vibrato_rate", params)
        self.assertIn("vibrato_depth", params)
        self.assertIn("vibrato_amp_depth", params)

    def test_set_params_updates_values(self) -> None:
        """set_params() updates parameter values."""
        e = AudioOutEmitter("test", {})
        e.set_params(master_volume=0.9, vibrato_rate=10.0)
        self.assertAlmostEqual(e.master_volume, 0.9)
        self.assertAlmostEqual(e.vibrato_rate, 10.0)


if __name__ == "__main__":
    unittest.main()
