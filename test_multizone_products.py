"""Test MULTIZONE_PRODUCTS registry and is_multizone property.

Validates the product ID set against the official LIFX product registry
(https://github.com/LIFX/products) and verifies that the LifxDevice
is_multizone property correctly classifies devices without requiring
any physical hardware.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Optional

from transport import MULTIZONE_PRODUCTS, MONOCHROME_PRODUCTS, LifxDevice

__version__: str = "1.0"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: URL of the official LIFX product registry.
LIFX_PRODUCTS_URL: str = (
    "https://raw.githubusercontent.com/LIFX/products/master/products.json"
)

#: Timeout in seconds for fetching the product registry.
FETCH_TIMEOUT_SECONDS: int = 10

#: Known multizone product IDs and their names (for offline validation).
EXPECTED_MULTIZONE: dict[int, str] = {
    31:  "LIFX Z",
    32:  "LIFX Z",
    38:  "LIFX Beam",
    117: "LIFX Z US",
    118: "LIFX Z Intl",
    119: "LIFX Beam US",
    120: "LIFX Beam Intl",
    141: "LIFX Neon US",
    142: "LIFX Neon Intl",
    143: "LIFX String US",
    144: "LIFX String Intl",
    161: "LIFX Outdoor Neon US",
    162: "LIFX Outdoor Neon Intl",
    203: "LIFX String US",
    204: "LIFX String Intl",
    205: "LIFX Indoor Neon US",
    206: "LIFX Indoor Neon Intl",
    213: "LIFX Permanent Outdoor US",
    214: "LIFX Permanent Outdoor Intl",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_device(product_id: Optional[int]) -> LifxDevice:
    """Create a LifxDevice with just a product ID set (no network).

    Args:
        product_id: The LIFX product ID, or ``None`` to simulate
            an un-queried device.

    Returns:
        A ``LifxDevice`` instance with the product field set.
    """
    dev = LifxDevice.__new__(LifxDevice)
    dev.ip = "0.0.0.0"
    dev.port = 56700
    dev.mac = b"\x00" * 6
    dev.label = "test"
    dev.group = ""
    dev.vendor = 1
    dev.product = product_id
    dev.product_name = None
    dev.zone_count = None
    return dev


def _fetch_official_registry() -> Optional[list[dict]]:
    """Fetch the official LIFX products.json from GitHub.

    Returns:
        Parsed JSON as a list of vendor objects, or ``None`` on failure.
    """
    try:
        req = urllib.request.Request(LIFX_PRODUCTS_URL)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        print(f"  (could not fetch registry: {exc})")
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_expected_ids_present():
    """Every expected multizone product ID is in MULTIZONE_PRODUCTS."""
    missing = set(EXPECTED_MULTIZONE.keys()) - MULTIZONE_PRODUCTS
    assert not missing, (
        f"Missing from MULTIZONE_PRODUCTS: "
        f"{', '.join(f'{pid} ({EXPECTED_MULTIZONE[pid]})' for pid in sorted(missing))}"
    )
    print(f"  All {len(EXPECTED_MULTIZONE)} expected IDs present ... OK")


def test_no_unexpected_ids():
    """MULTIZONE_PRODUCTS contains no IDs beyond what we expect."""
    extra = MULTIZONE_PRODUCTS - set(EXPECTED_MULTIZONE.keys())
    assert not extra, (
        f"Unexpected IDs in MULTIZONE_PRODUCTS: {sorted(extra)}"
    )
    print(f"  No unexpected IDs in set ... OK")


def test_no_overlap_with_monochrome():
    """Multizone and monochrome sets must be disjoint."""
    overlap = MULTIZONE_PRODUCTS & MONOCHROME_PRODUCTS
    assert not overlap, (
        f"IDs in both MULTIZONE and MONOCHROME: {sorted(overlap)}"
    )
    print(f"  No overlap with MONOCHROME_PRODUCTS ... OK")


def test_is_multizone_true():
    """is_multizone returns True for every multizone product ID."""
    for pid in sorted(MULTIZONE_PRODUCTS):
        dev = _make_device(pid)
        assert dev.is_multizone is True, (
            f"pid={pid}: is_multizone should be True"
        )
    print(f"  is_multizone=True for all {len(MULTIZONE_PRODUCTS)} IDs ... OK")


def test_is_multizone_false():
    """is_multizone returns False for non-multizone product IDs."""
    non_multizone = [1, 10, 27, 50, 123, 124, 125, 163, 164, 999]
    for pid in non_multizone:
        dev = _make_device(pid)
        assert dev.is_multizone is False, (
            f"pid={pid}: is_multizone should be False"
        )
    print(f"  is_multizone=False for {len(non_multizone)} non-multizone IDs ... OK")


def test_is_multizone_none():
    """is_multizone returns None when product ID is unknown."""
    dev = _make_device(None)
    assert dev.is_multizone is None, "is_multizone should be None for unqueried device"
    print(f"  is_multizone=None for unqueried device ... OK")


def test_against_official_registry():
    """Validate our set against the live LIFX product registry.

    This test fetches the official products.json from GitHub and
    checks that every product marked multizone=True is in our set,
    and vice versa.  Skipped (not failed) if the fetch fails.
    """
    data = _fetch_official_registry()
    if data is None:
        print("  SKIPPED (network unavailable)")
        return

    # Build the official set of multizone product IDs.
    official: set[int] = set()
    for vendor in data:
        for prod in vendor.get("products", []):
            features = prod.get("features", {})
            if features.get("multizone") or features.get("extended_multizone"):
                official.add(prod["pid"])

    missing = official - MULTIZONE_PRODUCTS
    extra = MULTIZONE_PRODUCTS - official

    if missing:
        print(f"  WARNING: Official registry has IDs we're missing: {sorted(missing)}")
    if extra:
        print(f"  WARNING: We have IDs not in official registry: {sorted(extra)}")

    assert not missing, (
        f"MULTIZONE_PRODUCTS is missing official IDs: {sorted(missing)}"
    )
    assert not extra, (
        f"MULTIZONE_PRODUCTS has IDs not in official registry: {sorted(extra)}"
    )
    print(f"  Matches official registry ({len(official)} multizone products) ... OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    """Run all tests and report results."""
    tests = [
        ("Expected IDs present", test_expected_ids_present),
        ("No unexpected IDs", test_no_unexpected_ids),
        ("No overlap with monochrome", test_no_overlap_with_monochrome),
        ("is_multizone True", test_is_multizone_true),
        ("is_multizone False", test_is_multizone_false),
        ("is_multizone None", test_is_multizone_none),
        ("Official registry validation", test_against_official_registry),
    ]

    print("Multizone product ID tests\n" + "=" * 40)
    passed: int = 0
    failed: int = 0

    for name, fn in tests:
        print(f"\n{name}:")
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"  FAILED: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {exc}")
            failed += 1

    print(f"\n{'=' * 40}")
    print(f"{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
