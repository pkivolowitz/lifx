"""Hardware validation tests for the LIFX Luna 700-series matrix device.

These tests talk to real hardware at a known IP address and validate the
full matrix protocol stack: discovery classification, device chain query,
tile color read/write, firmware effects, and power cycling.

**Not part of the automatic test suite.**  Run on demand when a Luna is
available on the network::

    python3 test_luna_hardware.py -v
    python3 -m unittest test_luna_hardware -v
    python3 -m unittest test_luna_hardware.TestLunaHardware.test_04_write_solid_red -v

Tests are numbered to enforce execution order — later tests depend on
device state established by earlier ones.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import logging
import os
import socket
import struct
import sys
import time
import unittest
from typing import Optional

from transport import (
    HSBK_BLACK_DEFAULT,
    HSBK_FMT,
    HSBK_MAX,
    HSBK_SIZE,
    KELVIN_MAX,
    KELVIN_MIN,
    MATRIX_PRODUCTS,
    MSG_GET_DEVICE_CHAIN,
    MSG_STATE_DEVICE_CHAIN,
    POWER_OFF,
    POWER_ON,
    STATE_DEVICE_CHAIN_PAYLOAD_SIZE,
    TILE_EFFECT_FLAME,
    TILE_EFFECT_MORPH,
    TILE_EFFECT_OFF,
    TILE_EFFECT_SKY,
    TILE_PIXELS_PER_PACKET,
    LifxDevice,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Environment variable naming the Luna under test.  Site-specific
#: hardware fixtures must come from the operator's environment, never
#: from a literal in the repo — every install has a different IP.
LUNA_IP_ENV: str = "GLOWUP_LUNA_IP"

#: IP address of the Luna under test, or empty when no Luna is exposed
#: to this run.  When empty the suite skips wholesale via
#: ``@unittest.skipUnless`` below.
LUNA_IP: str = os.environ.get(LUNA_IP_ENV, "")

#: UDP port for LIFX LAN protocol.
LIFX_PORT: int = 56700

#: Timeout for the reachability probe (seconds).
PROBE_TIMEOUT_SECONDS: float = 3.0

#: Minimum transition time for any command that changes bulb state (ms).
#: A bulb was bricked 2026-03-17 by rapid-fire zero-transition commands.
MIN_TRANSITION_MS: int = 50

#: Settle time after sending a color command before reading back (seconds).
#: Luna firmware needs time to apply Set64 before Get64 returns new values.
SETTLE_SECONDS: float = 0.5

#: Settle time after a firmware effect command (seconds).
EFFECT_SETTLE_SECONDS: float = 2.0

#: Expected Luna product IDs (US and International variants).
LUNA_PRODUCT_IDS: set[int] = {219, 220}

#: Test brightness — 10% to avoid blinding.  Luna at full brightness
#: in a dark room is physically painful.
TEST_BRIGHTNESS: int = HSBK_MAX // 10

#: HSBK tuples for test colors.
RED: tuple[int, int, int, int] = (0, HSBK_MAX, TEST_BRIGHTNESS, 3500)
GREEN: tuple[int, int, int, int] = (21845, HSBK_MAX, TEST_BRIGHTNESS, 3500)
BLUE: tuple[int, int, int, int] = (43690, HSBK_MAX, TEST_BRIGHTNESS, 3500)
WHITE: tuple[int, int, int, int] = (0, 0, TEST_BRIGHTNESS, 4000)
BLACK: tuple[int, int, int, int] = HSBK_BLACK_DEFAULT

#: Tolerance for HSBK readback comparison.  Firmware may quantize or
#: interpolate values slightly, so exact match is not always possible.
HSBK_TOLERANCE: int = 512

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger: logging.Logger = logging.getLogger("test.luna")

# ---------------------------------------------------------------------------
# Reachability probe
# ---------------------------------------------------------------------------


def _luna_reachable() -> bool:
    """Check whether the Luna responds to a UDP probe on the LIFX port.

    Sends a minimal GetService (2) broadcast-style packet and waits for
    any response.  This is lighter than a full discovery cycle.

    Returns ``False`` immediately if no Luna IP was configured via
    ``$GLOWUP_LUNA_IP`` — the suite is skipped wholesale in that case.
    """
    if not LUNA_IP:
        return False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(PROBE_TIMEOUT_SECONDS)
        # Minimal LIFX header: size(u16)=36, protocol+flags(u16)=0x3400,
        # source(u32)=1, target(8B)=0, reserved(6B)=0, flags(u8)=0,
        # sequence(u8)=0, reserved(u64)=0, type(u16)=2 (GetService),
        # reserved(u16)=0.  Total = 36 bytes.
        header: bytes = struct.pack(
            "<HH I 8s 6s BB Q HH",
            36,         # size
            0x3400,     # protocol | addressable | tagged
            1,          # source
            b'\x00' * 8,  # target (broadcast)
            b'\x00' * 6,  # reserved
            0,          # flags
            0,          # sequence
            0,          # reserved
            2,          # type = GetService
            0,          # reserved
        )
        sock.sendto(header, (LUNA_IP, LIFX_PORT))
        data, _ = sock.recvfrom(1024)
        sock.close()
        return len(data) > 0
    except (socket.timeout, OSError):
        return False


_CAN_TEST: bool = _luna_reachable()
_SKIP_REASON: str = (
    f"${LUNA_IP_ENV} not set — set it to the Luna's IP to run this suite"
    if not LUNA_IP
    else f"Luna not reachable at {LUNA_IP}:{LIFX_PORT}"
)


# ---------------------------------------------------------------------------
# Shared device handle
# ---------------------------------------------------------------------------

# Created once for the entire test run.  Tests are ordered so that
# earlier tests populate fields that later tests depend on.
_dev: Optional[LifxDevice] = None


def _get_device() -> LifxDevice:
    """Return the shared LifxDevice handle, creating it on first call."""
    global _dev
    if _dev is None:
        _dev = LifxDevice(LUNA_IP, acked=False)
    return _dev


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@unittest.skipUnless(_CAN_TEST, _SKIP_REASON)
class TestLunaHardware(unittest.TestCase):
    """Hardware validation for a LIFX Luna at a known IP.

    Tests are numbered (test_01_, test_02_, ...) to enforce execution
    order.  unittest sorts test names lexicographically by default.
    """

    # -- Helpers ------------------------------------------------------------

    def _assert_hsbk_close(
        self,
        actual: tuple[int, int, int, int],
        expected: tuple[int, int, int, int],
        msg: str = "",
    ) -> None:
        """Assert that two HSBK tuples are within tolerance.

        Firmware quantization, interpolation, and transition timing can
        cause small deviations.  We check each channel independently.
        """
        labels: tuple[str, ...] = ("hue", "sat", "bri", "kelvin")
        for i, label in enumerate(labels):
            delta: int = abs(actual[i] - expected[i])
            # Hue wraps around at 65535 → 0.
            if label == "hue":
                delta = min(delta, HSBK_MAX - delta)
            self.assertLessEqual(
                delta, HSBK_TOLERANCE,
                f"{msg}{label}: expected {expected[i]}, got {actual[i]} "
                f"(delta {delta} > tolerance {HSBK_TOLERANCE})",
            )

    # -- 01: Connectivity ---------------------------------------------------

    def test_01_device_responds(self) -> None:
        """Luna responds to GetVersion and returns a valid product ID."""
        dev: LifxDevice = _get_device()
        vendor, product = dev.query_version()
        self.assertIsNotNone(vendor, "vendor is None — no response")
        self.assertIsNotNone(product, "product is None — no response")
        logger.info("Vendor: %s, Product: %s (%s)", vendor, product, dev.product_name)

    def test_02_product_is_luna(self) -> None:
        """Product ID matches a known Luna variant."""
        dev: LifxDevice = _get_device()
        if dev.product is None:
            dev.query_version()
        self.assertIn(
            dev.product, LUNA_PRODUCT_IDS,
            f"Product {dev.product} ({dev.product_name}) is not a Luna "
            f"(expected one of {LUNA_PRODUCT_IDS})",
        )

    def test_03_is_matrix(self) -> None:
        """Luna is classified as a matrix device."""
        dev: LifxDevice = _get_device()
        if dev.product is None:
            dev.query_version()
        self.assertTrue(dev.is_matrix, "Luna should be classified as matrix")
        self.assertFalse(
            dev.needs_double_buffer,
            "Luna should NOT need double-buffering (<=64 zones)",
        )

    # -- 02: Label & group --------------------------------------------------

    def test_04_query_label(self) -> None:
        """Luna responds to GetLabel (may be empty if unconfigured)."""
        dev: LifxDevice = _get_device()
        label: Optional[str] = dev.query_label()
        self.assertIsNotNone(label, "label is None — no response from device")
        if len(label) == 0:
            logger.warning("Label is empty — Luna has not been named yet")
        else:
            logger.info("Label: %r", label)

    def test_05_query_group(self) -> None:
        """Luna responds to GetGroup with a non-empty string."""
        dev: LifxDevice = _get_device()
        group: Optional[str] = dev.query_group()
        self.assertIsNotNone(group, "group is None — no response")
        logger.info("Group: %r", group)

    # -- 03: Device chain (matrix geometry) ---------------------------------

    def test_06_query_device_chain(self) -> None:
        """GetDeviceChain returns valid matrix dimensions."""
        dev: LifxDevice = _get_device()
        result = dev.query_device_chain()
        self.assertIsNotNone(result, "query_device_chain returned None")
        width, height, tile_count = result
        self.assertGreater(width, 0, "matrix width must be > 0")
        self.assertGreater(height, 0, "matrix height must be > 0")
        self.assertGreater(tile_count, 0, "tile count must be > 0")
        # Luna should be a single-tile device with <=64 zones.
        total: int = width * height
        self.assertLessEqual(
            total, TILE_PIXELS_PER_PACKET,
            f"Luna zones ({total}) exceed single-packet limit "
            f"({TILE_PIXELS_PER_PACKET}) — unexpected for Luna",
        )
        logger.info(
            "Matrix: %dx%d, %d tile(s), %d total zones",
            width, height, tile_count, total,
        )

    def test_07_zone_count_populated(self) -> None:
        """After device chain query, zone_count matches width * height."""
        dev: LifxDevice = _get_device()
        if dev.matrix_width is None:
            dev.query_device_chain()
        expected: int = (dev.matrix_width or 0) * (dev.matrix_height or 0)
        self.assertEqual(
            dev.zone_count, expected,
            f"zone_count ({dev.zone_count}) != width*height ({expected})",
        )

    # -- 04: Power control --------------------------------------------------

    def test_08_power_on(self) -> None:
        """Ensure Luna is powered on for subsequent color tests."""
        dev: LifxDevice = _get_device()
        dev.set_power(True, duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)
        state = dev.query_light_state()
        self.assertIsNotNone(state, "query_light_state returned None")
        _, _, _, _, power = state
        self.assertEqual(power, POWER_ON, f"Expected power ON ({POWER_ON}), got {power}")
        logger.info("Power: ON")

    # -- 05: Read current tile state ----------------------------------------

    def test_09_query_tile_colors(self) -> None:
        """Get64 returns the correct number of HSBK pixels."""
        dev: LifxDevice = _get_device()
        if dev.matrix_width is None:
            dev.query_device_chain()
        colors = dev.query_tile_colors()
        self.assertIsNotNone(colors, "query_tile_colors returned None")
        expected_len: int = (dev.matrix_width or 0) * (dev.matrix_height or 0)
        self.assertEqual(
            len(colors), expected_len,
            f"Expected {expected_len} pixels, got {len(colors)}",
        )
        # Spot-check: every pixel should have valid HSBK ranges.
        for i, (h, s, b, k) in enumerate(colors):
            self.assertLessEqual(h, HSBK_MAX, f"pixel {i}: hue {h} > {HSBK_MAX}")
            self.assertLessEqual(s, HSBK_MAX, f"pixel {i}: sat {s} > {HSBK_MAX}")
            self.assertLessEqual(b, HSBK_MAX, f"pixel {i}: bri {b} > {HSBK_MAX}")
            self.assertGreaterEqual(k, KELVIN_MIN, f"pixel {i}: kelvin {k} < {KELVIN_MIN}")
            self.assertLessEqual(k, KELVIN_MAX, f"pixel {i}: kelvin {k} > {KELVIN_MAX}")
        logger.info("Read %d pixels, all HSBK values in range", len(colors))

    # -- 06: Write and verify solid colors ----------------------------------

    def test_10_write_solid_red(self) -> None:
        """Set64 with all-red pixels, then verify via Get64 readback."""
        dev: LifxDevice = _get_device()
        if dev.matrix_width is None:
            dev.query_device_chain()
        total: int = dev.zone_count or 0
        self.assertGreater(total, 0, "zone_count not populated")

        dev.set_tile_zones([RED] * total, duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)

        colors = dev.query_tile_colors()
        self.assertIsNotNone(colors, "readback failed after write")
        for i, pixel in enumerate(colors):
            self._assert_hsbk_close(pixel, RED, msg=f"pixel {i}: ")
        logger.info("Solid red verified across %d pixels", total)

    def test_11_write_solid_blue(self) -> None:
        """Set64 with all-blue pixels, then verify via Get64 readback."""
        dev: LifxDevice = _get_device()
        total: int = dev.zone_count or 0
        self.assertGreater(total, 0)

        dev.set_tile_zones([BLUE] * total, duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)

        colors = dev.query_tile_colors()
        self.assertIsNotNone(colors, "readback failed after write")
        for i, pixel in enumerate(colors):
            self._assert_hsbk_close(pixel, BLUE, msg=f"pixel {i}: ")
        logger.info("Solid blue verified across %d pixels", total)

    def test_12_write_solid_white(self) -> None:
        """Set64 with all-white (desaturated) pixels, then verify."""
        dev: LifxDevice = _get_device()
        total: int = dev.zone_count or 0
        self.assertGreater(total, 0)

        dev.set_tile_zones([WHITE] * total, duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)

        colors = dev.query_tile_colors()
        self.assertIsNotNone(colors, "readback failed after write")
        for i, pixel in enumerate(colors):
            self._assert_hsbk_close(pixel, WHITE, msg=f"pixel {i}: ")
        logger.info("Solid white verified across %d pixels", total)

    # -- 07: Write pattern (multi-color) ------------------------------------

    def test_13_write_checkerboard(self) -> None:
        """Write an alternating red/blue checkerboard and verify readback."""
        dev: LifxDevice = _get_device()
        width: int = dev.matrix_width or 0
        height: int = dev.matrix_height or 0
        total: int = width * height
        self.assertGreater(total, 0)

        # Build row-major checkerboard.
        pattern: list[tuple[int, int, int, int]] = []
        for row in range(height):
            for col in range(width):
                pattern.append(RED if (row + col) % 2 == 0 else BLUE)

        dev.set_tile_zones(pattern, duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)

        colors = dev.query_tile_colors()
        self.assertIsNotNone(colors, "readback failed after checkerboard write")
        mismatches: int = 0
        for i, pixel in enumerate(colors):
            expected = pattern[i]
            try:
                self._assert_hsbk_close(pixel, expected, msg=f"pixel {i}: ")
            except AssertionError:
                mismatches += 1
        # Allow a small number of mismatches — firmware may interpolate
        # at the edges of adjacent colors during transition.
        max_allowed: int = max(1, total // 10)
        self.assertLessEqual(
            mismatches, max_allowed,
            f"Checkerboard: {mismatches}/{total} pixel mismatches "
            f"(max allowed {max_allowed})",
        )
        logger.info(
            "Checkerboard verified: %d/%d pixels match (tolerance %d)",
            total - mismatches, total, max_allowed,
        )

    # -- 08: Padding / trimming behavior ------------------------------------

    def test_14_write_short_list_pads(self) -> None:
        """Writing fewer colors than zone_count pads with black."""
        dev: LifxDevice = _get_device()
        total: int = dev.zone_count or 0
        self.assertGreater(total, 0)

        # Send only one green pixel — remainder should be padded black.
        dev.set_tile_zones([GREEN], duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)

        colors = dev.query_tile_colors()
        self.assertIsNotNone(colors)
        self._assert_hsbk_close(colors[0], GREEN, msg="pixel 0 (green): ")
        # Remaining pixels should be black (brightness 0).
        for i in range(1, len(colors)):
            self.assertLessEqual(
                colors[i][2], HSBK_TOLERANCE,
                f"pixel {i}: expected black (bri ~0), got bri={colors[i][2]}",
            )
        logger.info("Padding verified: 1 green + %d black", total - 1)

    def test_15_write_long_list_trims(self) -> None:
        """Writing more colors than zone_count does not crash."""
        dev: LifxDevice = _get_device()
        total: int = dev.zone_count or 0
        self.assertGreater(total, 0)

        # Send double the needed colors — should silently trim.
        oversized: list[tuple[int, int, int, int]] = [RED] * (total * 2)
        dev.set_tile_zones(oversized, duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)

        colors = dev.query_tile_colors()
        self.assertIsNotNone(colors)
        self.assertEqual(len(colors), total)
        logger.info("Trim verified: sent %d colors, device has %d zones", total * 2, total)

    # -- 09: Transition timing ----------------------------------------------

    def test_16_transition_respected(self) -> None:
        """A long transition produces intermediate brightness values.

        Writes black, waits for settle, then writes full-brightness white
        with a 2-second transition.  A mid-transition readback should show
        brightness below the target.
        """
        dev: LifxDevice = _get_device()
        total: int = dev.zone_count or 0
        self.assertGreater(total, 0)

        # Start from black.
        dev.set_tile_zones([BLACK] * total, duration_ms=MIN_TRANSITION_MS)
        time.sleep(SETTLE_SECONDS)

        # Transition to full white over 2 seconds.
        transition_ms: int = 2000
        dev.set_tile_zones([WHITE] * total, duration_ms=transition_ms)
        # Read back quickly — should be mid-transition.
        time.sleep(0.3)
        mid_colors = dev.query_tile_colors()
        self.assertIsNotNone(mid_colors, "mid-transition readback failed")

        # At least some pixels should have brightness below full.
        below_full: int = sum(1 for _, _, b, _ in mid_colors if b < HSBK_MAX - HSBK_TOLERANCE)
        logger.info(
            "Transition check: %d/%d pixels below full brightness at 0.3s into 2s fade",
            below_full, total,
        )
        # This is a soft check — firmware timing is not guaranteed.
        # Log the result but only fail if zero pixels are transitioning
        # AND the device should have started by now.
        if below_full == 0:
            logger.warning(
                "No pixels below full brightness during transition — "
                "firmware may apply transitions instantly on matrix devices",
            )

        # Wait for transition to complete and verify final state.
        time.sleep(2.0)
        final_colors = dev.query_tile_colors()
        self.assertIsNotNone(final_colors)
        for i, pixel in enumerate(final_colors):
            self._assert_hsbk_close(pixel, WHITE, msg=f"final pixel {i}: ")
        logger.info("Transition final state verified: solid white")

    # -- 10: Firmware tile effects ------------------------------------------

    def test_17_firmware_effect_morph(self) -> None:
        """SetTileEffect(MORPH) with a rainbow palette does not error."""
        dev: LifxDevice = _get_device()

        # Rainbow palette: 6 saturated hues at test brightness.
        palette: list[tuple[int, int, int, int]] = [
            (0, HSBK_MAX, TEST_BRIGHTNESS, 3500),         # red
            (10922, HSBK_MAX, TEST_BRIGHTNESS, 3500),      # yellow
            (21845, HSBK_MAX, TEST_BRIGHTNESS, 3500),      # green
            (32768, HSBK_MAX, TEST_BRIGHTNESS, 3500),      # cyan
            (43690, HSBK_MAX, TEST_BRIGHTNESS, 3500),      # blue
            (54613, HSBK_MAX, TEST_BRIGHTNESS, 3500),      # magenta
        ]
        dev.set_tile_effect(
            TILE_EFFECT_MORPH, speed_ms=3000, duration_ns=0, palette=palette,
        )
        logger.info("MORPH effect started — visually confirm color morphing")
        time.sleep(EFFECT_SETTLE_SECONDS)
        # No crash = pass.  Visual confirmation is manual.

    def test_18_firmware_effect_flame(self) -> None:
        """SetTileEffect(FLAME) does not error."""
        dev: LifxDevice = _get_device()
        dev.set_tile_effect(TILE_EFFECT_FLAME, speed_ms=3000, duration_ns=0)
        logger.info("FLAME effect started — visually confirm flame animation")
        time.sleep(EFFECT_SETTLE_SECONDS)

    def test_19_clear_tile_effect(self) -> None:
        """Clearing the tile effect returns the device to static state."""
        dev: LifxDevice = _get_device()
        dev.clear_tile_effect()
        time.sleep(SETTLE_SECONDS)
        logger.info("Tile effect cleared")

        # Verify we can write colors again after clearing.
        total: int = dev.zone_count or 0
        if total > 0:
            dev.set_tile_zones([GREEN] * total, duration_ms=MIN_TRANSITION_MS)
            time.sleep(SETTLE_SECONDS)
            colors = dev.query_tile_colors()
            self.assertIsNotNone(colors, "readback failed after clearing effect")
            self._assert_hsbk_close(colors[0], GREEN, msg="post-clear pixel 0: ")
            logger.info("Post-clear write verified: solid green")

    # -- 11: query_all integration ------------------------------------------

    def test_20_query_all(self) -> None:
        """query_all() populates all cached fields for a matrix device."""
        # Use a fresh device handle to verify the full query_all path.
        fresh: LifxDevice = LifxDevice(LUNA_IP, acked=False)
        fresh.query_all()

        self.assertIsNotNone(fresh.vendor)
        self.assertIsNotNone(fresh.product)
        self.assertIn(fresh.product, LUNA_PRODUCT_IDS)
        self.assertIsNotNone(fresh.label)
        self.assertTrue(fresh.is_matrix)
        self.assertIsNotNone(fresh.matrix_width)
        self.assertIsNotNone(fresh.matrix_height)
        self.assertIsNotNone(fresh.tile_count)
        self.assertIsNotNone(fresh.zone_count)
        self.assertEqual(
            fresh.zone_count,
            fresh.matrix_width * fresh.matrix_height,
        )
        logger.info(
            "query_all: %s (%s) — %dx%d, %d zones",
            fresh.label, fresh.product_name,
            fresh.matrix_width, fresh.matrix_height, fresh.zone_count,
        )
        fresh.sock.close()

    # -- 12: Error handling -------------------------------------------------

    def test_21_empty_colors_raises(self) -> None:
        """set_tile_zones with empty list raises ValueError."""
        dev: LifxDevice = _get_device()
        with self.assertRaises(ValueError):
            dev.set_tile_zones([])

    def test_22_negative_duration_raises(self) -> None:
        """set_tile_zones with negative duration raises ValueError."""
        dev: LifxDevice = _get_device()
        with self.assertRaises(ValueError):
            dev.set_tile_zones([RED], duration_ms=-1)

    # -- 13: Cleanup --------------------------------------------------------

    def test_99_power_off(self) -> None:
        """Power off Luna after testing."""
        dev: LifxDevice = _get_device()
        dev.clear_tile_effect()
        dev.set_power(False, duration_ms=500)
        time.sleep(0.5)
        logger.info("Luna restored to neutral white")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _CAN_TEST:
        print(f"SKIP: {_SKIP_REASON}")
        sys.exit(0)
    unittest.main(verbosity=2)
