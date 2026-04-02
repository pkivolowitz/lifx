"""Tests for wake word detection — sliding window, cooldown, mock detector."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import time
import unittest

import numpy as np

from voice.satellite.wake import MockWakeDetector


class TestMockWakeDetector(unittest.TestCase):
    """Tests for MockWakeDetector (keyboard-triggered)."""

    def test_no_trigger_returns_none(self) -> None:
        """Feeding audio without triggering returns None."""
        det = MockWakeDetector(cooldown=0.1)
        chunk = np.zeros(1280, dtype=np.int16)
        self.assertIsNone(det.feed(chunk))

    def test_trigger_returns_score(self) -> None:
        """After trigger(), feed returns 1.0."""
        det = MockWakeDetector(cooldown=0.1)
        det.trigger()
        chunk = np.zeros(1280, dtype=np.int16)
        score = det.feed(chunk)
        self.assertEqual(score, 1.0)

    def test_trigger_consumed_after_one_feed(self) -> None:
        """Trigger is consumed — second feed returns None."""
        det = MockWakeDetector(cooldown=0.0)
        det.trigger()
        chunk = np.zeros(1280, dtype=np.int16)
        det.feed(chunk)  # Consumes trigger.
        # Need to wait for cooldown.
        time.sleep(0.05)
        self.assertIsNone(det.feed(chunk))

    def test_cooldown_suppresses(self) -> None:
        """Trigger during cooldown is suppressed."""
        det = MockWakeDetector(cooldown=1.0)
        det.trigger()
        chunk = np.zeros(1280, dtype=np.int16)
        det.feed(chunk)  # Fires, starts cooldown.
        det.trigger()
        self.assertIsNone(det.feed(chunk))  # Suppressed.

    def test_cooldown_expires(self) -> None:
        """After cooldown expires, trigger works again."""
        det = MockWakeDetector(cooldown=0.05)
        det.trigger()
        chunk = np.zeros(1280, dtype=np.int16)
        det.feed(chunk)  # Fires.
        time.sleep(0.1)  # Wait for cooldown.
        det.trigger()
        score = det.feed(chunk)
        self.assertEqual(score, 1.0)

    def test_reset_clears_trigger(self) -> None:
        """Reset clears a pending trigger."""
        det = MockWakeDetector(cooldown=0.0)
        det.trigger()
        det.reset()
        chunk = np.zeros(1280, dtype=np.int16)
        self.assertIsNone(det.feed(chunk))

    def test_multiple_triggers_only_fires_once(self) -> None:
        """Multiple triggers before feed only fire once."""
        det = MockWakeDetector(cooldown=0.0)
        det.trigger()
        det.trigger()
        det.trigger()
        chunk = np.zeros(1280, dtype=np.int16)
        score = det.feed(chunk)
        self.assertEqual(score, 1.0)
        time.sleep(0.05)
        self.assertIsNone(det.feed(chunk))

    def test_audio_content_ignored(self) -> None:
        """Mock detector ignores actual audio content."""
        det = MockWakeDetector(cooldown=0.0)
        loud = np.full(1280, 32767, dtype=np.int16)
        self.assertIsNone(det.feed(loud))
        det.trigger()
        silent = np.zeros(1280, dtype=np.int16)
        self.assertEqual(det.feed(silent), 1.0)


if __name__ == "__main__":
    unittest.main()
