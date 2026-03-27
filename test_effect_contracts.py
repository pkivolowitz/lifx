#!/usr/bin/env python3
"""Effect render contract enforcement — elevated tier pre-release validation.

This test suite enforces the render() contract across every registered effect
in the GlowUp effect engine.  It is designed to catch contract violations
before they reach production: wrong return types, out-of-range HSBK values,
float-where-int-expected, NaN/inf corruption, and incorrect zone counts.

Unlike unit tests that exercise individual effect logic, this suite treats
each effect as a black box and validates only the public render interface.
Every effect must satisfy the same invariants regardless of implementation.

Run this before any release to verify that new or modified effects do not
violate the HSBK contract.

Contract:
    - render(t, zone_count) returns a list
    - Length equals zone_count (except 2D grid effects)
    - Every element is a tuple of length 4
    - Every component is int (not float, not bool)
    - hue in [0, 65535]
    - saturation in [0, 65535]
    - brightness in [0, 65535]
    - kelvin in [1500, 9000]
    - No NaN, no inf in any value
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import math
import unittest
from typing import Any, Optional

from effects import (
    Effect,
    MediaEffect,
    get_registry,
    create_effect,
    HSBK_MAX,
    KELVIN_MIN,
    KELVIN_MAX,
)

logger: logging.Logger = logging.getLogger("glowup.test.effect_contracts")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum value for hue, saturation, and brightness (unsigned 16-bit).
HSB_MAX: int = HSBK_MAX  # 65535

# Kelvin range boundaries.
K_MIN: int = KELVIN_MIN  # 1500
K_MAX: int = KELVIN_MAX  # 9000

# HSBK tuple component indices.
IDX_HUE: int = 0
IDX_SAT: int = 1
IDX_BRI: int = 2
IDX_KEL: int = 3

# HSBK tuple length.
HSBK_TUPLE_LEN: int = 4

# Zone counts representing real devices:
# 1 = single bulb, 3 = short segment, 36 = one strip, 108 = Luna or chain.
ZONE_COUNT_SINGLE: int = 1
ZONE_COUNT_SHORT: int = 3
ZONE_COUNT_STRIP: int = 36
ZONE_COUNT_LUNA: int = 108

# Time values for render calls.
# 0.0 = first frame, 0.5 = mid-second, 1.0 = one second, 5.0 = well in.
TIME_START: float = 0.0
TIME_HALF: float = 0.5
TIME_ONE: float = 1.0
TIME_FIVE: float = 5.0

# Number of consecutive frames for time-series validation.
TIMESERIES_FRAME_COUNT: int = 20

# Time step between consecutive frames (~30 fps).
TIMESERIES_DT: float = 1.0 / 30.0

# Effects that render width*height pixels on a 2D grid — their output
# length may differ from the zone_count argument passed to render().
# Note: grid_map registers as "_grid_map" (internal/diagnostic effect).
GRID_EFFECTS: frozenset[str] = frozenset({
    "plasma2d",
    "ripple2d",
    "spectrum2d",
    "matrix_rain",
    "_grid_map",
})

# Effects known to be MediaEffect subclasses — require a SignalBus that
# is not available in test context.  Listed explicitly per spec; the
# dynamic check below also catches any unlisted MediaEffect subclasses.
MEDIA_EFFECTS_EXPLICIT: frozenset[str] = frozenset({
    "spectrum",
    "waveform",
    "soundlevel",
})

# Component labels for readable assertion messages.
_COMPONENT_NAMES: tuple[str, ...] = ("hue", "saturation", "brightness", "kelvin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_media_effect(cls: type) -> bool:
    """Check whether an effect class is a MediaEffect subclass.

    Checks the MRO rather than relying on a hardcoded set so that new
    MediaEffect subclasses are automatically detected and skipped.

    Args:
        cls: The effect class to inspect.

    Returns:
        True if the class inherits from MediaEffect.
    """
    return issubclass(cls, MediaEffect)


def _should_skip(name: str, cls: type) -> Optional[str]:
    """Determine whether an effect should be skipped, and why.

    Checks for MediaEffect subclasses, transient effects that are not
    designed for continuous rendering, and effects that require optional
    dependencies.

    Args:
        name: The registered effect name.
        cls:  The effect class.

    Returns:
        A skip reason string, or None if the effect should be tested.
    """
    # MediaEffect subclasses need a SignalBus — skip.
    if name in MEDIA_EFFECTS_EXPLICIT or _is_media_effect(cls):
        return f"{name} is a MediaEffect (requires SignalBus)"
    return None


def _try_create(name: str) -> Optional[Effect]:
    """Attempt to create an effect with default parameters.

    Catches ImportError (missing optional dependencies like sounddevice)
    and ValueError (effects that require mandatory params) so the test
    suite can skip gracefully instead of crashing.

    Args:
        name: The registered effect name.

    Returns:
        An Effect instance, or None if creation failed.
    """
    try:
        return create_effect(name)
    except ImportError as exc:
        logger.info("Skipping %s — ImportError: %s", name, exc)
        return None
    except ValueError as exc:
        logger.info("Skipping %s — ValueError: %s", name, exc)
        return None


def _validate_hsbk_tuple(
    test: unittest.TestCase,
    pixel: Any,
    effect_name: str,
    zone_idx: int,
    t: float,
    zone_count: int,
) -> None:
    """Validate a single HSBK tuple against the contract.

    Asserts every invariant: type is tuple, length is 4, every component
    is int (not float, not bool), values are in range, no NaN, no inf.

    Args:
        test:        The TestCase instance (for assertion methods).
        pixel:       The HSBK value to validate.
        effect_name: Effect name (for readable failure messages).
        zone_idx:    Zone index within the frame.
        t:           Time value used for the render call.
        zone_count:  Zone count used for the render call.
    """
    ctx: str = (
        f"[{effect_name}] zone={zone_idx} t={t} zones={zone_count}"
    )

    # Must be a tuple of length 4.
    test.assertIsInstance(
        pixel, tuple,
        f"{ctx}: expected tuple, got {type(pixel).__name__}",
    )
    test.assertEqual(
        len(pixel), HSBK_TUPLE_LEN,
        f"{ctx}: expected {HSBK_TUPLE_LEN}-tuple, got length {len(pixel)}",
    )

    for comp_idx in range(HSBK_TUPLE_LEN):
        val: Any = pixel[comp_idx]
        comp_name: str = _COMPONENT_NAMES[comp_idx]

        # Must be int — not float, not bool (bool is a subclass of int
        # in Python, so reject it explicitly).
        test.assertIsInstance(
            val, int,
            f"{ctx} {comp_name}: expected int, got {type(val).__name__} ({val!r})",
        )
        test.assertNotIsInstance(
            val, bool,
            f"{ctx} {comp_name}: got bool instead of int ({val!r})",
        )

        # No NaN or inf — ints cannot be NaN/inf in Python, but
        # defensive check in case of exotic subclasses.
        test.assertFalse(
            isinstance(val, float) and (math.isnan(val) or math.isinf(val)),
            f"{ctx} {comp_name}: NaN or inf detected ({val!r})",
        )

        # Range checks.
        if comp_idx == IDX_KEL:
            test.assertGreaterEqual(
                val, K_MIN,
                f"{ctx} {comp_name}: {val} < {K_MIN}",
            )
            test.assertLessEqual(
                val, K_MAX,
                f"{ctx} {comp_name}: {val} > {K_MAX}",
            )
        else:
            test.assertGreaterEqual(
                val, 0,
                f"{ctx} {comp_name}: {val} < 0",
            )
            test.assertLessEqual(
                val, HSB_MAX,
                f"{ctx} {comp_name}: {val} > {HSB_MAX}",
            )


def _validate_frame(
    test: unittest.TestCase,
    frame: Any,
    effect_name: str,
    zone_count: int,
    t: float,
    check_length: bool = True,
) -> None:
    """Validate a complete render() result.

    Checks that the result is a list, optionally checks length, and
    validates every HSBK tuple.

    Args:
        test:         The TestCase instance.
        frame:        The render() return value.
        effect_name:  Effect name for messages.
        zone_count:   Expected zone count.
        t:            Time value used.
        check_length: If True, assert len(frame) == zone_count.
    """
    ctx: str = f"[{effect_name}] t={t} zones={zone_count}"

    # Must be a list.
    test.assertIsInstance(
        frame, list,
        f"{ctx}: render() returned {type(frame).__name__}, expected list",
    )

    # Length check (disabled for 2D grid effects).
    if check_length:
        test.assertEqual(
            len(frame), zone_count,
            f"{ctx}: expected {zone_count} zones, got {len(frame)}",
        )

    # Validate every pixel.
    for zone_idx, pixel in enumerate(frame):
        _validate_hsbk_tuple(test, pixel, effect_name, zone_idx, t, zone_count)


def _renderable_effects() -> list[tuple[str, type]]:
    """Return a sorted list of (name, class) for effects that can be tested.

    Filters out MediaEffect subclasses and effects in the explicit skip
    set.  Sorted for deterministic test ordering.

    Returns:
        List of (name, class) tuples.
    """
    registry: dict[str, type[Effect]] = get_registry()
    result: list[tuple[str, type]] = []
    for name in sorted(registry):
        cls: type = registry[name]
        reason: Optional[str] = _should_skip(name, cls)
        if reason is not None:
            logger.info("Excluding %s: %s", name, reason)
            continue
        result.append((name, cls))
    return result


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestEffectContractBasic(unittest.TestCase):
    """Validate every effect's render output at zone_count=36 (common strip)."""

    def test_all_effects_at_strip_zone_count(self) -> None:
        """Every renderable effect must produce valid HSBK at 36 zones.

        Iterates the full registry, creates each effect with defaults,
        calls on_start, renders at four time values, and validates every
        frame against the HSBK contract.
        """
        zone_count: int = ZONE_COUNT_STRIP
        times: list[float] = [TIME_START, TIME_HALF, TIME_ONE, TIME_FIVE]
        tested: int = 0
        skipped: int = 0

        for name, cls in _renderable_effects():
            effect: Optional[Effect] = _try_create(name)
            if effect is None:
                skipped += 1
                continue

            with self.subTest(effect=name):
                effect.on_start(zone_count)

                # 2D grid effects may return a different length.
                check_len: bool = name not in GRID_EFFECTS

                for t in times:
                    frame: list = effect.render(t, zone_count)
                    _validate_frame(
                        self, frame, name, zone_count, t,
                        check_length=check_len,
                    )
                tested += 1

        # Ensure the registry is not empty — catch import failures.
        self.assertGreater(
            tested, 0,
            "No effects were tested — registry may be empty or all were skipped",
        )
        logger.info(
            "TestEffectContractBasic: tested=%d skipped=%d", tested, skipped,
        )


class TestEffectContractSingleZone(unittest.TestCase):
    """Validate every effect at zone_count=1 (single bulb edge case)."""

    def test_all_effects_at_single_zone(self) -> None:
        """Render contract holds for the degenerate case of one zone.

        Single-zone rendering exercises division-by-zero edge cases and
        boundary conditions in effects that compute per-zone offsets.
        """
        zone_count: int = ZONE_COUNT_SINGLE
        times: list[float] = [TIME_START, TIME_HALF, TIME_ONE, TIME_FIVE]
        tested: int = 0

        for name, cls in _renderable_effects():
            effect: Optional[Effect] = _try_create(name)
            if effect is None:
                continue

            with self.subTest(effect=name):
                effect.on_start(zone_count)

                check_len: bool = name not in GRID_EFFECTS

                for t in times:
                    frame: list = effect.render(t, zone_count)
                    _validate_frame(
                        self, frame, name, zone_count, t,
                        check_length=check_len,
                    )
                tested += 1

        self.assertGreater(tested, 0, "No effects tested at single zone")


class TestEffectContractLargeZone(unittest.TestCase):
    """Validate every effect at zone_count=108 (Luna / large chain)."""

    def test_all_effects_at_luna_zone_count(self) -> None:
        """Render contract holds for the largest supported zone count.

        Luna uses 108 zones (multiple tiles).  This catches off-by-one
        errors and scaling issues in effects that pre-allocate arrays.
        """
        zone_count: int = ZONE_COUNT_LUNA
        times: list[float] = [TIME_START, TIME_HALF, TIME_ONE, TIME_FIVE]
        tested: int = 0

        for name, cls in _renderable_effects():
            effect: Optional[Effect] = _try_create(name)
            if effect is None:
                continue

            with self.subTest(effect=name):
                effect.on_start(zone_count)

                check_len: bool = name not in GRID_EFFECTS

                for t in times:
                    frame: list = effect.render(t, zone_count)
                    _validate_frame(
                        self, frame, name, zone_count, t,
                        check_length=check_len,
                    )
                tested += 1

        self.assertGreater(tested, 0, "No effects tested at Luna zone count")


class TestEffectContractTimeSeries(unittest.TestCase):
    """Render 20 consecutive frames and validate every one.

    Time-series validation catches state corruption that only manifests
    after multiple frames — accumulator overflow, buffer index drift,
    or particle counts diverging over time.
    """

    def test_consecutive_frames_valid(self) -> None:
        """Every frame in a 20-frame burst must satisfy the HSBK contract."""
        zone_count: int = ZONE_COUNT_STRIP
        tested: int = 0

        for name, cls in _renderable_effects():
            effect: Optional[Effect] = _try_create(name)
            if effect is None:
                continue

            with self.subTest(effect=name):
                effect.on_start(zone_count)

                check_len: bool = name not in GRID_EFFECTS

                for frame_idx in range(TIMESERIES_FRAME_COUNT):
                    t: float = frame_idx * TIMESERIES_DT
                    frame: list = effect.render(t, zone_count)
                    _validate_frame(
                        self, frame, name, zone_count, t,
                        check_length=check_len,
                    )
                tested += 1

        self.assertGreater(tested, 0, "No effects tested in time series")


class TestEffectContractReturnLength(unittest.TestCase):
    """Verify render() returns exactly zone_count elements.

    2D grid effects are tested separately — they produce width*height
    pixels which may differ from the zone_count argument.
    """

    def test_non_grid_effects_return_exact_length(self) -> None:
        """Non-grid effects must return exactly zone_count HSBK tuples.

        Tests four zone counts to catch hardcoded sizes and off-by-one
        errors.
        """
        zone_counts: list[int] = [
            ZONE_COUNT_SINGLE,
            ZONE_COUNT_SHORT,
            ZONE_COUNT_STRIP,
            ZONE_COUNT_LUNA,
        ]
        tested: int = 0

        for name, cls in _renderable_effects():
            # Skip 2D grid effects — their length is width*height.
            if name in GRID_EFFECTS:
                continue

            effect: Optional[Effect] = _try_create(name)
            if effect is None:
                continue

            with self.subTest(effect=name):
                for zone_count in zone_counts:
                    effect.on_start(zone_count)
                    frame: list = effect.render(TIME_ONE, zone_count)

                    self.assertIsInstance(
                        frame, list,
                        f"[{name}] render() returned {type(frame).__name__}",
                    )
                    self.assertEqual(
                        len(frame), zone_count,
                        f"[{name}] zones={zone_count}: "
                        f"got {len(frame)} elements",
                    )
                tested += 1

        self.assertGreater(tested, 0, "No non-grid effects tested for length")

    def test_grid_effects_return_nonempty(self) -> None:
        """2D grid effects must return a non-empty list of valid HSBK tuples.

        The exact length depends on the effect's internal grid dimensions,
        so only non-emptiness and HSBK validity are checked.
        """
        tested: int = 0

        for name, cls in _renderable_effects():
            if name not in GRID_EFFECTS:
                continue

            effect: Optional[Effect] = _try_create(name)
            if effect is None:
                continue

            with self.subTest(effect=name):
                effect.on_start(ZONE_COUNT_STRIP)
                frame: list = effect.render(TIME_ONE, ZONE_COUNT_STRIP)

                self.assertIsInstance(frame, list, f"[{name}] not a list")
                self.assertGreater(
                    len(frame), 0,
                    f"[{name}] grid effect returned empty frame",
                )

                # Validate pixel contents even though length may differ.
                _validate_frame(
                    self, frame, name, ZONE_COUNT_STRIP, TIME_ONE,
                    check_length=False,
                )
                tested += 1

        # Grid effects are optional — don't fail if none are registered.
        logger.info(
            "TestEffectContractReturnLength (grid): tested=%d", tested,
        )


if __name__ == "__main__":
    unittest.main()
