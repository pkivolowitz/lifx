#!/usr/bin/env python3
"""Unit tests for effect rendering.

Verifies that every registered effect produces valid HSBK frames
for a range of zone counts and time values.  Tests:
  - Correct frame length (exactly zone_count tuples)
  - HSBK value ranges (0–65535 for H/S/B, 1500–9000 for kelvin)
  - No exceptions for edge-case zone counts (1, 3, 36, 108)
  - Stateful effects (fireworks, rule30, etc.) survive on_start/render cycles
  - Effect registry is non-empty and all effects are instantiable

No network or hardware dependencies — effects are pure renderers.
"""

import unittest
from typing import Any

from effects import (
    get_registry,
    create_effect,
    HSBK_MAX,
    KELVIN_MIN,
    KELVIN_MAX,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HSBK component indices.
H: int = 0
S: int = 1
B: int = 2
K: int = 3

# Zone counts to test: 1 zone (mini bulb), 3 zones (single string segment),
# 36 zones (one string light), 108 zones (full 3-string chain).
TEST_ZONE_COUNTS: list[int] = [1, 3, 36, 108]

# Time values to sample: t=0 (first frame), t=1 (1 second in),
# t=10 (well into the effect).
TEST_TIMES: list[float] = [0.0, 1.0, 10.0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_frame(
    test: unittest.TestCase,
    frame: list,
    zone_count: int,
    effect_name: str,
    t: float,
) -> None:
    """Assert that a rendered frame is valid HSBK data.

    Args:
        test:        The test case (for assertions).
        frame:       The list of HSBK tuples returned by render().
        zone_count:  Expected number of zones.
        effect_name: Effect name (for error messages).
        t:           Time value (for error messages).
    """
    test.assertEqual(
        len(frame), zone_count,
        f"{effect_name} at t={t}: expected {zone_count} zones, "
        f"got {len(frame)}",
    )
    for i, hsbk in enumerate(frame):
        test.assertEqual(
            len(hsbk), 4,
            f"{effect_name} zone {i} at t={t}: expected 4 components, "
            f"got {len(hsbk)}",
        )
        h_val, s_val, b_val, k_val = hsbk

        test.assertGreaterEqual(
            h_val, 0,
            f"{effect_name} zone {i} H={h_val} < 0",
        )
        test.assertLessEqual(
            h_val, HSBK_MAX,
            f"{effect_name} zone {i} H={h_val} > {HSBK_MAX}",
        )

        test.assertGreaterEqual(
            s_val, 0,
            f"{effect_name} zone {i} S={s_val} < 0",
        )
        test.assertLessEqual(
            s_val, HSBK_MAX,
            f"{effect_name} zone {i} S={s_val} > {HSBK_MAX}",
        )

        test.assertGreaterEqual(
            b_val, 0,
            f"{effect_name} zone {i} B={b_val} < 0",
        )
        test.assertLessEqual(
            b_val, HSBK_MAX,
            f"{effect_name} zone {i} B={b_val} > {HSBK_MAX}",
        )

        test.assertGreaterEqual(
            k_val, KELVIN_MIN,
            f"{effect_name} zone {i} K={k_val} < {KELVIN_MIN}",
        )
        test.assertLessEqual(
            k_val, KELVIN_MAX,
            f"{effect_name} zone {i} K={k_val} > {KELVIN_MAX}",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEffectRegistry(unittest.TestCase):
    """Tests for the effect registry."""

    def test_registry_is_non_empty(self) -> None:
        """At least one effect is registered."""
        registry = get_registry()
        self.assertGreater(len(registry), 0)

    def test_all_effects_instantiable(self) -> None:
        """Every registered effect can be created with default params."""
        for name in get_registry():
            try:
                effect = create_effect(name)
                self.assertIsNotNone(effect)
            except Exception as exc:
                self.fail(
                    f"create_effect('{name}') raised {type(exc).__name__}: "
                    f"{exc}"
                )

    def test_unknown_effect_raises(self) -> None:
        """create_effect with a bogus name raises ValueError."""
        with self.assertRaises(ValueError):
            create_effect("nonexistent_effect_xyz")


class TestEffectRendering(unittest.TestCase):
    """Verify all effects render valid HSBK frames.

    Dynamically generates test methods for every registered effect
    at multiple zone counts and time values.
    """

    pass  # Test methods are added dynamically below.


def _make_render_test(
    effect_name: str, zone_count: int,
) -> callable:
    """Create a test method for a specific effect and zone count.

    Args:
        effect_name: Registered effect name.
        zone_count:  Number of zones to render.

    Returns:
        A test method function.
    """
    def test_method(self: unittest.TestCase) -> None:
        effect = create_effect(effect_name)
        effect.on_start(zone_count)
        for t in TEST_TIMES:
            frame = effect.render(t, zone_count)
            _validate_frame(self, frame, zone_count, effect_name, t)
    test_method.__doc__ = (
        f"{effect_name} renders valid {zone_count}-zone frames"
    )
    return test_method


# Dynamically add a test method for every effect × zone count combination.
for _name in sorted(get_registry()):
    for _zc in TEST_ZONE_COUNTS:
        method_name = f"test_{_name}_{_zc}zones"
        setattr(
            TestEffectRendering,
            method_name,
            _make_render_test(_name, _zc),
        )


class TestEffectMultipleRenders(unittest.TestCase):
    """Verify stateful effects survive multiple render cycles."""

    def test_stateful_effects_multiple_frames(self) -> None:
        """Render 50 frames at 20fps for each effect without crashing."""
        fps: float = 20.0
        frame_count: int = 50
        zone_count: int = 36

        for name in sorted(get_registry()):
            effect = create_effect(name)
            effect.on_start(zone_count)
            for i in range(frame_count):
                t = i / fps
                try:
                    frame = effect.render(t, zone_count)
                    self.assertEqual(len(frame), zone_count)
                except Exception as exc:
                    self.fail(
                        f"{name} crashed at frame {i} (t={t:.3f}): "
                        f"{type(exc).__name__}: {exc}"
                    )


if __name__ == "__main__":
    unittest.main()
