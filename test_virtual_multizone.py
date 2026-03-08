"""Test VirtualMultizoneDevice zone mapping and dispatch.

Creates mock devices of all three types (multizone, color single, monochrome
single), builds a virtual group, and verifies that:
  1. Zone map expands multizone devices to their full zone count.
  2. set_zones() batches multizone colors into one call per device.
  3. Single color bulbs receive set_color() with full HSBK.
  4. Monochrome bulbs receive BT.709 luma-converted brightness.
  5. set_color() (fade-to-black) reaches all devices.
  6. set_power() reaches all devices.
"""

from __future__ import annotations

import sys


class MockDevice:
    """Minimal mock implementing the LifxDevice interface."""

    def __init__(
        self,
        ip: str,
        zone_count: int,
        is_multizone: bool,
        is_polychrome: bool,
    ) -> None:
        self.ip = ip
        self.zone_count = zone_count
        self.is_multizone = is_multizone
        self._is_polychrome = is_polychrome
        self.label = f"mock-{ip}"
        self.product_name = "Mock"
        self.mac_str = "00:00:00:00:00:00"
        self.product = 1
        self.group = ""

        # Record calls for assertions.
        self.set_color_calls: list[tuple] = []
        self.set_zones_calls: list[tuple] = []
        self.set_power_calls: list[tuple] = []
        self.close_called: bool = False

    @property
    def is_polychrome(self) -> bool:
        return self._is_polychrome

    def set_color(self, h, s, b, k, duration_ms=0):
        self.set_color_calls.append((h, s, b, k, duration_ms))

    def set_zones(self, colors, duration_ms=0, rapid=True):
        self.set_zones_calls.append((list(colors), duration_ms, rapid))

    def set_power(self, on, duration_ms=0):
        self.set_power_calls.append((on, duration_ms))

    def close(self):
        self.close_called = True


def test_zone_map_expansion():
    """Multizone devices contribute all zones; single bulbs contribute 1."""
    from engine import VirtualMultizoneDevice

    string_light = MockDevice("10.0.0.1", zone_count=6, is_multizone=True, is_polychrome=True)
    color_bulb = MockDevice("10.0.0.2", zone_count=1, is_multizone=False, is_polychrome=True)
    mono_bulb = MockDevice("10.0.0.3", zone_count=1, is_multizone=False, is_polychrome=False)

    vdev = VirtualMultizoneDevice([string_light, color_bulb, mono_bulb])

    assert vdev.zone_count == 8, f"Expected 8 zones (6+1+1), got {vdev.zone_count}"
    assert vdev.is_multizone is True
    print(f"  zone_count = {vdev.zone_count} (6 multizone + 1 color + 1 mono) ... OK")


def test_set_zones_dispatch():
    """set_zones() batches multizone, dispatches singles correctly."""
    from engine import VirtualMultizoneDevice

    string_light = MockDevice("10.0.0.1", zone_count=6, is_multizone=True, is_polychrome=True)
    color_bulb = MockDevice("10.0.0.2", zone_count=1, is_multizone=False, is_polychrome=True)
    mono_bulb = MockDevice("10.0.0.3", zone_count=1, is_multizone=False, is_polychrome=False)

    vdev = VirtualMultizoneDevice([string_light, color_bulb, mono_bulb])

    # 8 colors: zones 0-5 → string light, zone 6 → color bulb, zone 7 → mono bulb.
    colors = [
        (1000 * i, 65535, 32768, 3500) for i in range(8)
    ]
    vdev.set_zones(colors, duration_ms=0, rapid=True)

    # String light should get ONE set_zones() call with 6 colors.
    assert len(string_light.set_zones_calls) == 1, \
        f"Expected 1 set_zones call, got {len(string_light.set_zones_calls)}"
    batch_colors = string_light.set_zones_calls[0][0]
    assert len(batch_colors) == 6, f"Expected 6 colors in batch, got {len(batch_colors)}"
    for i in range(6):
        assert batch_colors[i] == colors[i], \
            f"Zone {i}: expected {colors[i]}, got {batch_colors[i]}"
    print(f"  String light: 1 set_zones() call, 6 colors batched ... OK")

    # Color bulb should get ONE set_color() with full HSBK.
    assert len(color_bulb.set_color_calls) == 1, \
        f"Expected 1 set_color call, got {len(color_bulb.set_color_calls)}"
    assert color_bulb.set_color_calls[0] == (6000, 65535, 32768, 3500, 0)
    assert len(color_bulb.set_zones_calls) == 0
    print(f"  Color bulb: set_color(6000, 65535, 32768, 3500) ... OK")

    # Mono bulb should get ONE set_color() with luma-converted brightness.
    assert len(mono_bulb.set_color_calls) == 1, \
        f"Expected 1 set_color call, got {len(mono_bulb.set_color_calls)}"
    h, s, b, k, dur = mono_bulb.set_color_calls[0]
    assert h == 0 and s == 0, f"Mono should get h=0, s=0; got h={h}, s={s}"
    assert k == 3500, f"Kelvin should pass through; got {k}"
    # Brightness should be > 0 (BT.709 luma of a colored pixel).
    assert b > 0, f"Luma brightness should be > 0, got {b}"
    print(f"  Mono bulb: set_color(0, 0, {b}, 3500) (BT.709 luma) ... OK")


def test_set_color_broadcast():
    """set_color() reaches all devices (used for fade-to-black)."""
    from engine import VirtualMultizoneDevice

    devs = [
        MockDevice(f"10.0.0.{i}", zone_count=(6 if i == 1 else 1),
                   is_multizone=(i == 1), is_polychrome=True)
        for i in range(1, 4)
    ]
    vdev = VirtualMultizoneDevice(devs)
    vdev.set_color(0, 0, 0, 3500, duration_ms=500)

    for dev in devs:
        assert len(dev.set_color_calls) == 1, \
            f"{dev.ip}: expected 1 set_color call, got {len(dev.set_color_calls)}"
        assert dev.set_color_calls[0] == (0, 0, 0, 3500, 500)
    print(f"  set_color(0,0,0,3500) broadcast to all 3 devices ... OK")


def test_set_power_broadcast():
    """set_power() reaches all devices."""
    from engine import VirtualMultizoneDevice

    devs = [
        MockDevice(f"10.0.0.{i}", zone_count=1, is_multizone=False, is_polychrome=True)
        for i in range(1, 6)
    ]
    vdev = VirtualMultizoneDevice(devs)
    vdev.set_power(on=True, duration_ms=0)

    for dev in devs:
        assert len(dev.set_power_calls) == 1
        assert dev.set_power_calls[0] == (True, 0)
    print(f"  set_power(on=True) broadcast to all 5 devices ... OK")


def test_two_multizone_devices():
    """Two multizone devices in one group batch independently."""
    from engine import VirtualMultizoneDevice

    strip_a = MockDevice("10.0.0.1", zone_count=4, is_multizone=True, is_polychrome=True)
    strip_b = MockDevice("10.0.0.2", zone_count=3, is_multizone=True, is_polychrome=True)

    vdev = VirtualMultizoneDevice([strip_a, strip_b])
    assert vdev.zone_count == 7, f"Expected 7, got {vdev.zone_count}"

    colors = [(i * 1000, 65535, 65535, 3500) for i in range(7)]
    vdev.set_zones(colors)

    # Strip A: 4 zones.
    assert len(strip_a.set_zones_calls) == 1
    assert len(strip_a.set_zones_calls[0][0]) == 4
    for i in range(4):
        assert strip_a.set_zones_calls[0][0][i] == colors[i]

    # Strip B: 3 zones.
    assert len(strip_b.set_zones_calls) == 1
    assert len(strip_b.set_zones_calls[0][0]) == 3
    for i in range(3):
        assert strip_b.set_zones_calls[0][0][i] == colors[4 + i]

    print(f"  Two strips (4+3=7 zones): each gets 1 batched set_zones() ... OK")


def test_all_singles():
    """Pure single-bulb group (original use case) still works."""
    from engine import VirtualMultizoneDevice

    devs = [
        MockDevice(f"10.0.0.{i}", zone_count=1, is_multizone=False, is_polychrome=True)
        for i in range(5)
    ]
    vdev = VirtualMultizoneDevice(devs)
    assert vdev.zone_count == 5

    colors = [(i * 10000, 65535, 65535, 3500) for i in range(5)]
    vdev.set_zones(colors)

    for i, dev in enumerate(devs):
        assert len(dev.set_color_calls) == 1
        assert dev.set_color_calls[0][:4] == colors[i][:4]
    print(f"  5 single bulbs: 5 individual set_color() calls ... OK")


def main() -> int:
    tests = [
        ("Zone map expansion", test_zone_map_expansion),
        ("set_zones dispatch", test_set_zones_dispatch),
        ("set_color broadcast", test_set_color_broadcast),
        ("set_power broadcast", test_set_power_broadcast),
        ("Two multizone devices", test_two_multizone_devices),
        ("All singles (regression)", test_all_singles),
    ]

    print("VirtualMultizoneDevice tests\n" + "=" * 40)
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
