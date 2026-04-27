"""Test VirtualMultizoneEmitter zone mapping and dispatch.

Creates mock emitters of multiple types (multizone, single-zone), builds
a virtual group, and verifies that:
  1. Zone map expands multizone emitters to their full zone count.
  2. send_zones() batches multizone colors into one call per emitter.
  3. Single-zone emitters receive send_color() with full HSBK.
  4. send_color() (fade-to-black) reaches all emitters.
  5. power_on/power_off reaches all emitters.

Monochrome luma conversion is now internal to LifxEmitter, not the
virtual group.  These tests verify the dispatch layer only.

The backward-compatible import ``from engine import VirtualMultizoneDevice``
is tested alongside the canonical ``VirtualMultizoneEmitter`` import.
"""

from __future__ import annotations

import sys
from typing import Optional

from effects import HSBK
from emitters import Emitter


# ---------------------------------------------------------------------------
# Mock emitter for testing
# ---------------------------------------------------------------------------

class MockEmitter(Emitter):
    """Minimal mock implementing the Emitter ABC."""

    def __init__(
        self,
        emitter_id: str,
        zone_count: int,
        is_multizone: bool,
    ) -> None:
        self._emitter_id: str = emitter_id
        self._zone_count: int = zone_count
        self._is_multizone: bool = is_multizone
        self._label: str = f"mock-{emitter_id}"
        self._product_name: str = "Mock"

        # Record calls for assertions.
        self.send_color_calls: list[tuple] = []
        self.send_zones_calls: list[tuple] = []
        self.power_on_calls: list[tuple] = []
        self.power_off_calls: list[tuple] = []
        self.close_called: bool = False
        self.prepare_called: bool = False
        self.prepare_skip_wake: bool = False

    @property
    def zone_count(self) -> Optional[int]:
        return self._zone_count

    @property
    def is_multizone(self) -> bool:
        return self._is_multizone

    @property
    def emitter_id(self) -> str:
        return self._emitter_id

    @property
    def label(self) -> str:
        return self._label

    @property
    def product_name(self) -> str:
        return self._product_name

    def send_zones(self, colors: list[HSBK], duration_ms: int = 0,
                   mode: object = None) -> None:
        self.send_zones_calls.append((list(colors), duration_ms, mode))

    def send_color(self, hue: int, sat: int, bri: int, kelvin: int,
                   duration_ms: int = 0) -> None:
        self.send_color_calls.append((hue, sat, bri, kelvin, duration_ms))

    def prepare_for_rendering(self, *, skip_wake: bool = False) -> None:
        """Record that prepare was called and whether wake was skipped."""
        self.prepare_called = True
        self.prepare_skip_wake = skip_wake

    def power_on(self, duration_ms: int = 0) -> None:
        self.power_on_calls.append((duration_ms,))

    def power_off(self, duration_ms: int = 0) -> None:
        self.power_off_calls.append((duration_ms,))

    def close(self) -> None:
        self.close_called = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_zone_map_expansion():
    """Multizone emitters contribute all zones; single emitters contribute 1."""
    from emitters.virtual import VirtualMultizoneEmitter

    strip = MockEmitter("192.0.2.1", zone_count=6, is_multizone=True)
    bulb_a = MockEmitter("192.0.2.2", zone_count=1, is_multizone=False)
    bulb_b = MockEmitter("192.0.2.3", zone_count=1, is_multizone=False)

    vem = VirtualMultizoneEmitter([strip, bulb_a, bulb_b])

    assert vem.zone_count == 8, f"Expected 8 zones (6+1+1), got {vem.zone_count}"
    assert vem.is_multizone is True
    print(f"  zone_count = {vem.zone_count} (6 multizone + 1 + 1) ... OK")


def test_send_zones_dispatch():
    """send_zones() batches multizone, dispatches singles correctly."""
    from emitters.virtual import VirtualMultizoneEmitter

    strip = MockEmitter("192.0.2.1", zone_count=6, is_multizone=True)
    bulb_a = MockEmitter("192.0.2.2", zone_count=1, is_multizone=False)
    bulb_b = MockEmitter("192.0.2.3", zone_count=1, is_multizone=False)

    vem = VirtualMultizoneEmitter([strip, bulb_a, bulb_b])

    # 8 colors: zones 0-5 → strip, zone 6 → bulb_a, zone 7 → bulb_b.
    colors: list[HSBK] = [
        (1000 * i, 65535, 32768, 3500) for i in range(8)
    ]
    vem.send_zones(colors, duration_ms=0)

    # Strip should get ONE send_zones() call with 6 colors.
    assert len(strip.send_zones_calls) == 1, \
        f"Expected 1 send_zones call, got {len(strip.send_zones_calls)}"
    batch_colors = strip.send_zones_calls[0][0]
    assert len(batch_colors) == 6, f"Expected 6 colors in batch, got {len(batch_colors)}"
    for i in range(6):
        assert batch_colors[i] == colors[i], \
            f"Zone {i}: expected {colors[i]}, got {batch_colors[i]}"
    print(f"  Strip: 1 send_zones() call, 6 colors batched ... OK")

    # Bulb A should get ONE send_color() with full HSBK.
    assert len(bulb_a.send_color_calls) == 1, \
        f"Expected 1 send_color call, got {len(bulb_a.send_color_calls)}"
    assert bulb_a.send_color_calls[0] == (6000, 65535, 32768, 3500, 0)
    assert len(bulb_a.send_zones_calls) == 0
    print(f"  Bulb A: send_color(6000, 65535, 32768, 3500) ... OK")

    # Bulb B should get ONE send_color() with full HSBK.
    assert len(bulb_b.send_color_calls) == 1, \
        f"Expected 1 send_color call, got {len(bulb_b.send_color_calls)}"
    assert bulb_b.send_color_calls[0] == (7000, 65535, 32768, 3500, 0)
    print(f"  Bulb B: send_color(7000, 65535, 32768, 3500) ... OK")


def test_send_color_broadcast():
    """send_color() reaches all emitters (used for fade-to-black)."""
    from emitters.virtual import VirtualMultizoneEmitter

    emitters = [
        MockEmitter(f"192.0.2.{i}", zone_count=(6 if i == 1 else 1),
                    is_multizone=(i == 1))
        for i in range(1, 4)
    ]
    vem = VirtualMultizoneEmitter(emitters)
    vem.send_color(0, 0, 0, 3500, duration_ms=500)

    for em in emitters:
        assert len(em.send_color_calls) == 1, \
            f"{em.emitter_id}: expected 1 send_color call, got {len(em.send_color_calls)}"
        assert em.send_color_calls[0] == (0, 0, 0, 3500, 500)
    print(f"  send_color(0,0,0,3500) broadcast to all 3 emitters ... OK")


def test_power_broadcast():
    """power_on/power_off reaches all emitters."""
    from emitters.virtual import VirtualMultizoneEmitter

    emitters = [
        MockEmitter(f"192.0.2.{i}", zone_count=1, is_multizone=False)
        for i in range(1, 6)
    ]
    vem = VirtualMultizoneEmitter(emitters)
    vem.power_on(duration_ms=0)

    for em in emitters:
        assert len(em.power_on_calls) == 1
        assert em.power_on_calls[0] == (0,)
    print(f"  power_on() broadcast to all 5 emitters ... OK")

    vem.power_off(duration_ms=500)
    for em in emitters:
        assert len(em.power_off_calls) == 1
        assert em.power_off_calls[0] == (500,)
    print(f"  power_off(500) broadcast to all 5 emitters ... OK")


def test_two_multizone_emitters():
    """Two multizone emitters in one group batch independently."""
    from emitters.virtual import VirtualMultizoneEmitter

    strip_a = MockEmitter("192.0.2.1", zone_count=4, is_multizone=True)
    strip_b = MockEmitter("192.0.2.2", zone_count=3, is_multizone=True)

    vem = VirtualMultizoneEmitter([strip_a, strip_b])
    assert vem.zone_count == 7, f"Expected 7, got {vem.zone_count}"

    colors: list[HSBK] = [(i * 1000, 65535, 65535, 3500) for i in range(7)]
    vem.send_zones(colors)

    # Strip A: 4 zones.
    assert len(strip_a.send_zones_calls) == 1
    assert len(strip_a.send_zones_calls[0][0]) == 4
    for i in range(4):
        assert strip_a.send_zones_calls[0][0][i] == colors[i]

    # Strip B: 3 zones.
    assert len(strip_b.send_zones_calls) == 1
    assert len(strip_b.send_zones_calls[0][0]) == 3
    for i in range(3):
        assert strip_b.send_zones_calls[0][0][i] == colors[4 + i]

    print(f"  Two strips (4+3=7 zones): each gets 1 batched send_zones() ... OK")


def test_all_singles():
    """Pure single-emitter group (original use case) still works."""
    from emitters.virtual import VirtualMultizoneEmitter

    emitters = [
        MockEmitter(f"192.0.2.{i}", zone_count=1, is_multizone=False)
        for i in range(5)
    ]
    vem = VirtualMultizoneEmitter(emitters)
    assert vem.zone_count == 5

    colors: list[HSBK] = [(i * 10000, 65535, 65535, 3500) for i in range(5)]
    vem.send_zones(colors)

    for i, em in enumerate(emitters):
        assert len(em.send_color_calls) == 1
        assert em.send_color_calls[0][:4] == colors[i][:4]
    print(f"  5 single emitters: 5 individual send_color() calls ... OK")


def test_backward_compat_import():
    """The engine.VirtualMultizoneDevice re-export still works."""
    from engine import VirtualMultizoneDevice
    from emitters.virtual import VirtualMultizoneEmitter

    assert VirtualMultizoneDevice is VirtualMultizoneEmitter, \
        "engine.VirtualMultizoneDevice should be emitters.virtual.VirtualMultizoneEmitter"
    print(f"  engine.VirtualMultizoneDevice is VirtualMultizoneEmitter ... OK")


def test_prepare_for_rendering():
    """prepare_for_rendering() fans out to all members with skip_wake=True."""
    from emitters.virtual import VirtualMultizoneEmitter

    emitters = [
        MockEmitter(f"192.0.2.{i}", zone_count=1, is_multizone=False)
        for i in range(3)
    ]
    vem = VirtualMultizoneEmitter(emitters)
    vem.prepare_for_rendering()

    for em in emitters:
        assert em.prepare_called, f"{em.emitter_id}: prepare not called"
        assert em.prepare_skip_wake is True, (
            f"{em.emitter_id}: expected skip_wake=True (virtual does one "
            f"broadcast wake for the group), got {em.prepare_skip_wake}"
        )
    print(f"  prepare_for_rendering() reached all 3 emitters with skip_wake=True ... OK")


def main() -> int:
    tests = [
        ("Zone map expansion", test_zone_map_expansion),
        ("send_zones dispatch", test_send_zones_dispatch),
        ("send_color broadcast", test_send_color_broadcast),
        ("Power broadcast", test_power_broadcast),
        ("Two multizone emitters", test_two_multizone_emitters),
        ("All singles (regression)", test_all_singles),
        ("Backward compat import", test_backward_compat_import),
        ("prepare_for_rendering", test_prepare_for_rendering),
    ]

    print("VirtualMultizoneEmitter tests\n" + "=" * 40)
    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n{name}:")
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1

    print(f"\n{'=' * 40}")
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
