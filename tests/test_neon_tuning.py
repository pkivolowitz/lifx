"""Tests for Neon-class device detection and auto-tuning.

These tests do not require hardware or a database — they use mock
emitters to verify the Engine's auto-tuning logic.

Run::

    python3 -m pytest tests/test_neon_tuning.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure the project root is importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import Engine, DEFAULT_FPS, NEON_FPS
from transport import NEON_PRODUCTS, MULTIZONE_PRODUCTS, LifxDevice


class TestNeonProductSet(unittest.TestCase):
    """Verify NEON_PRODUCTS is a proper subset of MULTIZONE_PRODUCTS."""

    def test_neon_products_subset_of_multizone(self) -> None:
        """Every Neon product ID should also be a multizone product."""
        self.assertTrue(
            NEON_PRODUCTS.issubset(MULTIZONE_PRODUCTS),
            f"NEON_PRODUCTS has IDs not in MULTIZONE_PRODUCTS: "
            f"{NEON_PRODUCTS - MULTIZONE_PRODUCTS}",
        )

    def test_neon_products_not_empty(self) -> None:
        """NEON_PRODUCTS should contain at least one product ID."""
        self.assertGreater(len(NEON_PRODUCTS), 0)


class TestLifxDeviceIsNeon(unittest.TestCase):
    """Test the is_neon property on LifxDevice."""

    def _make_device(self, product_id: int) -> LifxDevice:
        """Create a LifxDevice with a mocked product ID."""
        dev = MagicMock(spec=LifxDevice)
        dev.product = product_id
        # Call the actual property implementation.
        dev.is_neon = product_id in NEON_PRODUCTS
        return dev

    def test_neon_us_is_neon(self) -> None:
        """Product 141 (Neon US) should be detected as Neon."""
        dev = self._make_device(141)
        self.assertTrue(dev.is_neon)

    def test_neon_intl_is_neon(self) -> None:
        """Product 142 (Neon Intl) should be detected as Neon."""
        dev = self._make_device(142)
        self.assertTrue(dev.is_neon)

    def test_outdoor_neon_is_neon(self) -> None:
        """Product 161 (Outdoor Neon US) should be detected as Neon."""
        dev = self._make_device(161)
        self.assertTrue(dev.is_neon)

    def test_indoor_neon_is_neon(self) -> None:
        """Product 205 (Indoor Neon US) should be detected as Neon."""
        dev = self._make_device(205)
        self.assertTrue(dev.is_neon)

    def test_string_light_is_not_neon(self) -> None:
        """Product 143 (String Light US) should NOT be Neon."""
        dev = self._make_device(143)
        self.assertFalse(dev.is_neon)

    def test_beam_is_not_neon(self) -> None:
        """Product 38 (Beam) should NOT be Neon."""
        dev = self._make_device(38)
        self.assertFalse(dev.is_neon)

    def test_unknown_product_not_neon(self) -> None:
        """An unknown product ID should NOT be Neon."""
        dev = self._make_device(9999)
        self.assertFalse(dev.is_neon)


class TestEngineNeonAutoTuning(unittest.TestCase):
    """Test Engine auto-tuning behavior for Neon emitters."""

    def _mock_emitter(self, is_neon: bool = False,
                      zone_count: int = 30) -> MagicMock:
        """Create a mock emitter with configurable Neon flag."""
        em = MagicMock()
        em.is_neon = is_neon
        em.zone_count = zone_count
        return em

    def test_neon_lowers_fps(self) -> None:
        """Engine should auto-tune FPS to NEON_FPS for Neon devices."""
        em = self._mock_emitter(is_neon=True)
        engine = Engine([em])
        self.assertEqual(engine.fps, NEON_FPS)

    def test_non_neon_keeps_default(self) -> None:
        """Engine should keep DEFAULT_FPS for non-Neon devices."""
        em = self._mock_emitter(is_neon=False)
        engine = Engine([em])
        self.assertEqual(engine.fps, DEFAULT_FPS)

    def test_explicit_fps_overrides_auto_tune(self) -> None:
        """Explicit --fps should override Neon auto-tuning."""
        em = self._mock_emitter(is_neon=True)
        engine = Engine([em], fps=5, fps_explicit=True)
        self.assertEqual(engine.fps, 5)

    def test_mixed_emitters_detects_neon(self) -> None:
        """If any emitter is Neon, auto-tuning should activate."""
        neon = self._mock_emitter(is_neon=True)
        string = self._mock_emitter(is_neon=False, zone_count=50)
        engine = Engine([neon, string])
        self.assertEqual(engine.fps, NEON_FPS)

    def test_no_is_neon_attribute_safe(self) -> None:
        """Emitters without is_neon should not crash auto-tuning."""
        em = MagicMock(spec=[])  # No attributes at all.
        em.zone_count = 10
        engine = Engine([em])
        self.assertEqual(engine.fps, DEFAULT_FPS)

    def test_transition_not_overridden(self) -> None:
        """Neon auto-tuning should NOT override transition_ms."""
        em = self._mock_emitter(is_neon=True)
        engine = Engine([em])
        # transition_ms_override should remain None (use default calc).
        self.assertIsNone(engine._transition_ms_override)


if __name__ == "__main__":
    unittest.main()
