"""Tests for LifxSubdevice + matrix mask + fixture_db disambiguation.

Covers the 2026-04-30 SuperColor Ceiling support:

- ``LifxDevice.set_tile_zones`` masks cells declared in
  :attr:`mask_cells` (dead corners + uplight slots).
- ``LifxDevice._set_tile_zones_raw`` (private) does NOT mask — the
  sub-device path is the legitimate writer for masked cells.
- ``LifxSubdevice`` capability flags, label composition, last-color
  cache, and the ``set_power(True)`` parent-forward that prevents
  Set64 frames from landing in a powered-off buffer.
- ``fixture_db.get_mask_cells`` fail-soft for unknown fixtures.
- ``fixture_db.lookup_by_pid`` returns None on multi-fixture pid
  collisions instead of silently picking the wrong geometry.

LifxDevice is constructed via ``__new__`` (skipping ``__init__``) so
no UDP socket is opened; ``_send_set64`` and ``_send_copy_frame_buffer``
are patched to capture frames in-memory.  No hardware required.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import unittest
from unittest.mock import MagicMock

from infrastructure import fixture_db
from transport import (
    HSBK_BLACK_DEFAULT, HSBK_MAX, LifxDevice, LifxSubdevice,
)


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

# Standard SuperColor Ceiling geometry — 8x8 grid, single tile.
CEILING_W: int = 8
CEILING_H: int = 8
CEILING_TILES: int = 1
CEILING_TOTAL: int = CEILING_W * CEILING_H * CEILING_TILES

# A non-black HSBK so masked-cell assertions are unambiguous (any cell
# left as the all-zeros tuple isn't proof the mask did anything;
# checking against this red value is).
RED: tuple[int, int, int, int] = (0, HSBK_MAX, HSBK_MAX, 3500)
BLUE: tuple[int, int, int, int] = (HSBK_MAX // 2, HSBK_MAX, HSBK_MAX, 3500)


def _make_device(
    width: int = CEILING_W,
    height: int = CEILING_H,
    tiles: int = CEILING_TILES,
    mask_cells: set[int] = frozenset(),
) -> LifxDevice:
    """Build a ``LifxDevice`` without opening a socket; patch the wire path.

    Captures every ``_send_set64`` and ``_send_copy_frame_buffer`` call
    on ``dev._sent`` so tests can assert exact frames.
    """
    dev: LifxDevice = LifxDevice.__new__(LifxDevice)
    dev.matrix_width = width
    dev.matrix_height = height
    dev.tile_count = tiles
    dev.mask_cells = set(mask_cells)
    dev.subdevices = []
    dev.label = "Test Ceiling"
    dev.group = "Test Group"
    dev.ip = "127.0.0.1"
    dev.mac = b'\x00' * 8
    dev._sent: list[dict] = []  # type: ignore[attr-defined]

    def _capture_set64(  # noqa: ANN001 — mock signature
        tile_index, colors, width, duration_ms=0,
        fb_index=0, x=0, y=0,
    ):
        dev._sent.append({  # type: ignore[attr-defined]
            "kind": "set64",
            "tile": tile_index, "colors": list(colors),
            "width": width, "duration_ms": duration_ms,
            "fb": fb_index, "x": x, "y": y,
        })

    def _capture_copy(  # noqa: ANN001
        tile_index, width, height, duration_ms=0,
    ):
        dev._sent.append({  # type: ignore[attr-defined]
            "kind": "copy",
            "tile": tile_index, "width": width, "height": height,
            "duration_ms": duration_ms,
        })

    dev._send_set64 = _capture_set64  # type: ignore[assignment]
    dev._send_copy_frame_buffer = _capture_copy  # type: ignore[assignment]
    dev.set_power = MagicMock()
    return dev


# ---------------------------------------------------------------------------
# set_tile_zones / _set_tile_zones_raw
# ---------------------------------------------------------------------------


class TestSetTileZonesMask(unittest.TestCase):
    """``set_tile_zones`` must force masked cells to black."""

    def test_no_mask_passes_colors_through(self) -> None:
        """Empty mask_cells → frame is sent verbatim."""
        dev: LifxDevice = _make_device(mask_cells=set())
        colors: list[tuple[int, int, int, int]] = [RED] * CEILING_TOTAL
        dev.set_tile_zones(colors, duration_ms=100)

        self.assertEqual(len(dev._sent), 1)  # type: ignore[attr-defined]
        sent = dev._sent[0]  # type: ignore[attr-defined]
        self.assertEqual(sent["kind"], "set64")
        self.assertTrue(all(c == RED for c in sent["colors"]))

    def test_masked_cells_forced_to_black(self) -> None:
        """Cells in mask_cells become HSBK_BLACK_DEFAULT, others survive."""
        # Mask the four corners — exactly the SuperColor Ceiling pattern
        # (in real fixture, more cells; the principle is identical).
        masked: set[int] = {0, 7, 56, 63}
        dev: LifxDevice = _make_device(mask_cells=masked)
        colors: list[tuple[int, int, int, int]] = [RED] * CEILING_TOTAL
        dev.set_tile_zones(colors)

        sent = dev._sent[0]  # type: ignore[attr-defined]
        for idx in range(CEILING_TOTAL):
            if idx in masked:
                self.assertEqual(
                    sent["colors"][idx], HSBK_BLACK_DEFAULT,
                    f"masked cell {idx} should be black",
                )
            else:
                self.assertEqual(
                    sent["colors"][idx], RED,
                    f"unmasked cell {idx} should be RED",
                )

    def test_mask_does_not_mutate_caller_list(self) -> None:
        """Frame reuse — caller's render output must survive masking unchanged."""
        masked: set[int] = {0, 1, 2}
        dev: LifxDevice = _make_device(mask_cells=masked)
        colors: list[tuple[int, int, int, int]] = [RED] * CEILING_TOTAL
        original: list[tuple[int, int, int, int]] = list(colors)
        dev.set_tile_zones(colors)
        self.assertEqual(colors, original)

    def test_raw_path_skips_mask(self) -> None:
        """``_set_tile_zones_raw`` must NOT zero masked cells."""
        masked: set[int] = {0, 7, 56, 63}
        dev: LifxDevice = _make_device(mask_cells=masked)
        colors: list[tuple[int, int, int, int]] = [RED] * CEILING_TOTAL
        dev._set_tile_zones_raw(colors)  # pylint: disable=protected-access

        sent = dev._sent[0]  # type: ignore[attr-defined]
        for idx in range(CEILING_TOTAL):
            self.assertEqual(
                sent["colors"][idx], RED,
                f"raw path should not have touched cell {idx}",
            )

    def test_empty_colors_raises(self) -> None:
        """Empty list → ValueError on both paths."""
        dev: LifxDevice = _make_device()
        with self.assertRaises(ValueError):
            dev.set_tile_zones([])
        with self.assertRaises(ValueError):
            dev._set_tile_zones_raw([])  # pylint: disable=protected-access

    def test_negative_duration_raises(self) -> None:
        """Negative duration → ValueError on both paths."""
        dev: LifxDevice = _make_device()
        with self.assertRaises(ValueError):
            dev.set_tile_zones([RED], duration_ms=-1)
        with self.assertRaises(ValueError):
            dev._set_tile_zones_raw([RED], duration_ms=-1)  # pylint: disable=protected-access

    def test_pad_short_color_list_with_black(self) -> None:
        """Short input is padded to total pixel count with black."""
        dev: LifxDevice = _make_device()
        dev.set_tile_zones([RED, RED])  # 2 colors for a 64-pixel device
        sent = dev._sent[0]  # type: ignore[attr-defined]
        self.assertEqual(len(sent["colors"]), CEILING_TOTAL)
        self.assertEqual(sent["colors"][0], RED)
        self.assertEqual(sent["colors"][1], RED)
        for idx in range(2, CEILING_TOTAL):
            self.assertEqual(sent["colors"][idx], HSBK_BLACK_DEFAULT)

    def test_trim_oversized_color_list(self) -> None:
        """Oversized input is trimmed to total pixel count."""
        dev: LifxDevice = _make_device()
        oversized: list[tuple[int, int, int, int]] = [RED] * (CEILING_TOTAL + 16)
        dev.set_tile_zones(oversized)
        sent = dev._sent[0]  # type: ignore[attr-defined]
        self.assertEqual(len(sent["colors"]), CEILING_TOTAL)


# ---------------------------------------------------------------------------
# LifxSubdevice
# ---------------------------------------------------------------------------


class TestLifxSubdeviceCapabilities(unittest.TestCase):
    """Sub-device duck-types LifxDevice — flags must be stable."""

    def setUp(self) -> None:
        self.parent: LifxDevice = _make_device(
            mask_cells={56, 57, 63},  # pretend uplight lives here
        )
        self.sub: LifxSubdevice = LifxSubdevice(
            parent=self.parent,
            component_id="uplight",
            kind="single_color",
            cells=[(7, 0), (7, 1), (7, 7)],
            label_suffix="Uplight",
        )

    def test_capability_flags(self) -> None:
        """is_polychrome=True (color-capable), zone_count=1, not matrix/multizone."""
        self.assertTrue(self.sub.is_polychrome)
        self.assertFalse(self.sub.is_matrix)
        self.assertFalse(self.sub.is_multizone)
        self.assertEqual(self.sub.zone_count, 1)

    def test_label_composition(self) -> None:
        """Label = parent label + space + suffix."""
        self.assertEqual(self.sub.label, "Test Ceiling Uplight")

    def test_label_falls_back_when_no_suffix(self) -> None:
        """No suffix → sub-device's label is just the parent's."""
        bare: LifxSubdevice = LifxSubdevice(
            parent=self.parent,
            component_id="x",
            kind="single_color",
            cells=[(0, 0)],
            label_suffix="",
        )
        self.assertEqual(bare.label, "Test Ceiling")

    def test_inherits_ip_mac_group(self) -> None:
        """Network identity is the parent's — sub-devices share transport."""
        self.assertEqual(self.sub.ip, self.parent.ip)
        self.assertEqual(self.sub.mac, self.parent.mac)
        self.assertEqual(self.sub.group, self.parent.group)

    def test_empty_cells_rejected(self) -> None:
        """A sub-device must own at least one cell."""
        with self.assertRaises(ValueError):
            LifxSubdevice(
                parent=self.parent,
                component_id="empty",
                kind="single_color",
                cells=[],
                label_suffix="x",
            )


class TestLifxSubdeviceWriteCells(unittest.TestCase):
    """Frame composition — sub-device cells get *color*, others get black."""

    def setUp(self) -> None:
        # Mask the cells the sub-device owns so that the bypass-mask
        # behavior is observable: the parent's masked frame would zero
        # them, but the raw path leaves them alone.
        self.cells: list[tuple[int, int]] = [(7, 0), (7, 7)]
        masked_indices: set[int] = {
            r * CEILING_W + c for r, c in self.cells
        }
        self.parent: LifxDevice = _make_device(mask_cells=masked_indices)
        self.sub: LifxSubdevice = LifxSubdevice(
            parent=self.parent,
            component_id="uplight",
            kind="single_color",
            cells=self.cells,
            label_suffix="Uplight",
        )

    def test_write_paints_only_owned_cells(self) -> None:
        """Owned cells become *color*; everything else is HSBK_BLACK_DEFAULT."""
        self.sub.set_color(RED[0], RED[1], RED[2], RED[3])
        self.assertEqual(len(self.parent._sent), 1)  # type: ignore[attr-defined]
        sent = self.parent._sent[0]  # type: ignore[attr-defined]
        self.assertEqual(sent["kind"], "set64")
        owned: set[int] = {r * CEILING_W + c for r, c in self.cells}
        for idx in range(CEILING_TOTAL):
            if idx in owned:
                self.assertEqual(
                    sent["colors"][idx], RED,
                    f"owned cell {idx} should be RED — raw path bypasses mask",
                )
            else:
                self.assertEqual(
                    sent["colors"][idx], HSBK_BLACK_DEFAULT,
                    f"non-owned cell {idx} should be black",
                )

    def test_set_color_updates_last_color_cache(self) -> None:
        """``_last_color`` becomes the latest set; power-cycle restores it."""
        self.sub.set_color(BLUE[0], BLUE[1], BLUE[2], BLUE[3])
        self.assertEqual(
            self.sub._last_color, BLUE,  # pylint: disable=protected-access
        )

    def test_set_color_validates_ranges(self) -> None:
        """Out-of-range HSBK raises ValueError before any frame is sent."""
        with self.assertRaises(ValueError):
            self.sub.set_color(HSBK_MAX + 1, 0, 0, 3500)
        with self.assertRaises(ValueError):
            self.sub.set_color(0, 0, 0, 0)  # kelvin too low
        self.assertEqual(len(self.parent._sent), 0)  # type: ignore[attr-defined]


class TestLifxSubdevicePower(unittest.TestCase):
    """``set_power`` semantics — parent-forward on True, never on False."""

    def setUp(self) -> None:
        self.parent: LifxDevice = _make_device(mask_cells={0, 7})
        self.sub: LifxSubdevice = LifxSubdevice(
            parent=self.parent,
            component_id="uplight",
            kind="single_color",
            cells=[(0, 0), (0, 7)],
            label_suffix="Uplight",
        )

    def test_power_on_forwards_parent_power_then_writes_last_color(self) -> None:
        """Without parent power, Set64 lands in a buffer no LED is reading."""
        self.sub.set_power(True, duration_ms=200)
        self.parent.set_power.assert_called_once_with(True, duration_ms=200)
        # One frame written, painting the seed color (bright white).
        self.assertEqual(len(self.parent._sent), 1)  # type: ignore[attr-defined]
        sent = self.parent._sent[0]  # type: ignore[attr-defined]
        seed: tuple[int, int, int, int] = (0, 0, HSBK_MAX, 3500)
        # Owned cells should hold the seed color.
        self.assertEqual(sent["colors"][0 * CEILING_W + 0], seed)
        self.assertEqual(sent["colors"][0 * CEILING_W + 7], seed)

    def test_power_off_does_not_touch_parent_power(self) -> None:
        """Coexisting matrix effects need the parent to stay up."""
        self.sub.set_power(False)
        self.parent.set_power.assert_not_called()
        # Frame is sent (writes black to owned cells).
        self.assertEqual(len(self.parent._sent), 1)  # type: ignore[attr-defined]
        sent = self.parent._sent[0]  # type: ignore[attr-defined]
        self.assertEqual(sent["colors"][0 * CEILING_W + 0], HSBK_BLACK_DEFAULT)
        self.assertEqual(sent["colors"][0 * CEILING_W + 7], HSBK_BLACK_DEFAULT)


# ---------------------------------------------------------------------------
# fixture_db
# ---------------------------------------------------------------------------


class TestFixtureDbFailSoft(unittest.TestCase):
    """Unknown fixtures must not crash discovery — fail-soft contract."""

    def test_get_mask_cells_unknown_returns_empty(self) -> None:
        """Unknown (vendor, pid, w, h) → empty mask, not exception."""
        # Use a bogus pid that no fixture file claims.
        result: set[int] = fixture_db.get_mask_cells(1, 99999, 8, 8)
        self.assertEqual(result, set())

    def test_get_mask_cells_with_none_args_returns_empty(self) -> None:
        """Pre-query state (None args) → empty mask."""
        self.assertEqual(fixture_db.get_mask_cells(None, None, None, None), set())

    def test_get_components_unknown_returns_empty_list(self) -> None:
        """Unknown fixture → empty components list."""
        self.assertEqual(fixture_db.get_components(1, 99999, 8, 8), [])


class TestLookupByPidDisambiguation(unittest.TestCase):
    """``lookup_by_pid`` must return None when geometry can't be resolved."""

    def setUp(self) -> None:
        # Snapshot and clear the cache so we control the test fixture set.
        # Both attributes are module-private; restore in tearDown.
        self._saved_cache: dict = dict(fixture_db._cache)
        self._saved_loaded: bool = fixture_db._cache_loaded
        fixture_db._cache.clear()
        fixture_db._cache_loaded = True  # skip disk scan

    def tearDown(self) -> None:
        fixture_db._cache.clear()
        fixture_db._cache.update(self._saved_cache)
        fixture_db._cache_loaded = self._saved_loaded

    def test_no_match_returns_none(self) -> None:
        """Empty cache → None for any (vendor, pid)."""
        self.assertIsNone(fixture_db.lookup_by_pid(1, 176))

    def test_single_match_returns_fixture(self) -> None:
        """Exactly one (vendor, pid) match → return that fixture dict."""
        fx: dict = {"protocol": {"vendor": 1, "pid": 176}, "tag": "ceiling-15"}
        fixture_db._cache[(1, 176, 8, 8)] = fx
        self.assertIs(fixture_db.lookup_by_pid(1, 176), fx)

    def test_multi_match_returns_none(self) -> None:
        """Two fixtures share pid 176 (different geometry) → refuse to pick."""
        fx_15: dict = {"protocol": {"vendor": 1, "pid": 176}, "tag": "ceiling-15"}
        fx_11: dict = {"protocol": {"vendor": 1, "pid": 176}, "tag": "ceiling-11"}
        fixture_db._cache[(1, 176, 8, 8)] = fx_15
        fixture_db._cache[(1, 176, 6, 6)] = fx_11
        self.assertIsNone(fixture_db.lookup_by_pid(1, 176))

    def test_none_args_short_circuit(self) -> None:
        """None vendor or pid → None immediately."""
        self.assertIsNone(fixture_db.lookup_by_pid(None, 176))
        self.assertIsNone(fixture_db.lookup_by_pid(1, None))


if __name__ == "__main__":
    unittest.main()
