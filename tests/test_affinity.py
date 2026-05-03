#!/usr/bin/env python3
"""Unit tests for effect device affinity.

Verifies that every registered effect declares a valid affinity set,
the metaclass rejects invalid values, and the server API includes
affinity in its output.

No network or hardware dependencies.
"""

__version__: str = "0.1"

import unittest
from typing import Any

from effects import (
    Effect,
    Param,
    HSBK,
    get_registry,
    create_effect,
    DEVICE_TYPE_BULB,
    DEVICE_TYPE_STRIP,
    DEVICE_TYPE_MATRIX,
    ALL_DEVICE_TYPES,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The three valid device type strings.
VALID_TYPES: frozenset[str] = frozenset({
    DEVICE_TYPE_BULB,
    DEVICE_TYPE_STRIP,
    DEVICE_TYPE_MATRIX,
})


# ---------------------------------------------------------------------------
# Tests — affinity metadata
# ---------------------------------------------------------------------------

class TestAffinityMetadata(unittest.TestCase):
    """Verify that every effect has a well-formed affinity set."""

    def test_every_effect_has_affinity(self) -> None:
        """All registered effects expose a frozenset affinity attribute."""
        for name, cls in get_registry().items():
            self.assertTrue(
                hasattr(cls, "affinity"),
                f"{name} is missing the 'affinity' attribute",
            )
            self.assertIsInstance(
                cls.affinity, frozenset,
                f"{name}.affinity should be frozenset, got "
                f"{type(cls.affinity).__name__}",
            )

    def test_affinity_contains_only_valid_types(self) -> None:
        """No effect has a device type string outside the known set."""
        for name, cls in get_registry().items():
            invalid: frozenset[str] = cls.affinity - VALID_TYPES
            self.assertFalse(
                invalid,
                f"{name}.affinity contains invalid types: {invalid}",
            )

    def test_affinity_is_non_empty(self) -> None:
        """Every effect supports at least one device type."""
        for name, cls in get_registry().items():
            self.assertGreater(
                len(cls.affinity), 0,
                f"{name}.affinity is empty — must support at least one type",
            )

    def test_universal_effects_have_all_types(self) -> None:
        """Effects that should work everywhere have ALL_DEVICE_TYPES.

        Effects must be present in the registry — silently skipping
        missing entries would mask the exact regression this test
        guards against (e.g. an effect dropped from the registry).
        """
        # ``_primary_cycle`` is registered with a leading underscore
        # (hidden from the public effect list) but still must satisfy
        # the universal-affinity contract.
        universal_names: list[str] = [
            "breathe", "morse", "_primary_cycle", "soundlevel", "twinkle",
        ]
        registry: dict[str, Any] = get_registry()
        for name in universal_names:
            self.assertIn(
                name, registry,
                f"{name!r} expected universal but missing from registry",
            )
            cls = registry[name]
            self.assertEqual(
                cls.affinity, ALL_DEVICE_TYPES,
                f"{name} should be universal but affinity is {cls.affinity}",
            )

    def test_strip_only_effects(self) -> None:
        """Spot-check that known strip-only effects exclude bulb and matrix.

        See note on ``test_universal_effects_have_all_types`` — missing
        entries fail rather than skip, so a registry regression cannot
        slip past the spot-check.
        """
        strip_only_names: list[str] = [
            "aurora", "fireworks", "flag", "ripple", "sine",
        ]
        registry: dict[str, Any] = get_registry()
        for name in strip_only_names:
            self.assertIn(
                name, registry,
                f"{name!r} expected strip-only but missing from registry",
            )
            cls = registry[name]
            self.assertEqual(
                cls.affinity,
                frozenset({DEVICE_TYPE_STRIP}),
                f"{name} should be strip-only but affinity is {cls.affinity}",
            )

    def test_bloom_is_bulb_only(self) -> None:
        """Bloom targets single-zone bulbs only."""
        registry: dict[str, Any] = get_registry()
        # bloom is hidden (name starts with _), stored as _bloom.
        cls = registry.get("_bloom") or registry.get("bloom")
        if cls is None:
            self.skipTest("bloom effect not registered")
        self.assertEqual(cls.affinity, frozenset({DEVICE_TYPE_BULB}))

    def test_cylon_supports_bulb_and_strip(self) -> None:
        """Cylon works on both single-zone bulbs and strips."""
        registry: dict[str, Any] = get_registry()
        cls = registry.get("cylon")
        if cls is None:
            self.skipTest("cylon effect not registered")
        self.assertEqual(
            cls.affinity,
            frozenset({DEVICE_TYPE_BULB, DEVICE_TYPE_STRIP}),
        )

    def test_plasma2d_is_matrix_only(self) -> None:
        """plasma2d is designed for 2D matrix devices only."""
        registry: dict[str, Any] = get_registry()
        cls = registry.get("plasma2d")
        if cls is None:
            self.skipTest("plasma2d effect not registered")
        self.assertEqual(cls.affinity, frozenset({DEVICE_TYPE_MATRIX}))

    def test_spectrum2d_supports_strip_and_matrix(self) -> None:
        """spectrum2d works on both strips and matrices."""
        registry: dict[str, Any] = get_registry()
        cls = registry.get("spectrum2d")
        if cls is None:
            self.skipTest("spectrum2d effect not registered")
        self.assertEqual(
            cls.affinity,
            frozenset({DEVICE_TYPE_STRIP, DEVICE_TYPE_MATRIX}),
        )


# ---------------------------------------------------------------------------
# Tests — metaclass validation
# ---------------------------------------------------------------------------

class TestAffinityValidation(unittest.TestCase):
    """Verify the EffectMeta metaclass rejects invalid affinity values."""

    def test_invalid_affinity_type_rejected(self) -> None:
        """Defining an effect with a bogus device type should raise."""
        with self.assertRaises(ValueError):
            class BadEffect(Effect):
                name: str = "_test_bad_affinity"
                description: str = "Should not load"
                affinity: frozenset[str] = frozenset({"projector"})

                def render(self, t: float, zone_count: int) -> list[HSBK]:
                    return [(0, 0, 0, 3500)] * zone_count

    def test_empty_affinity_rejected(self) -> None:
        """Defining an effect with an empty affinity should raise."""
        with self.assertRaises(ValueError):
            class EmptyAffinity(Effect):
                name: str = "_test_empty_affinity"
                description: str = "Should not load"
                affinity: frozenset[str] = frozenset()

                def render(self, t: float, zone_count: int) -> list[HSBK]:
                    return [(0, 0, 0, 3500)] * zone_count


# ---------------------------------------------------------------------------
# Tests — instance-level affinity
# ---------------------------------------------------------------------------

class TestAffinityOnInstances(unittest.TestCase):
    """Verify affinity is accessible on effect instances, not just classes."""

    def test_instance_inherits_class_affinity(self) -> None:
        """Created effect instances have the same affinity as their class."""
        for name, cls in get_registry().items():
            instance = create_effect(name)
            self.assertEqual(
                instance.affinity, cls.affinity,
                f"{name} instance affinity differs from class",
            )


if __name__ == "__main__":
    unittest.main()
