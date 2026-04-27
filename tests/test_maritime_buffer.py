"""Tests for MaritimeBuffer's per-vessel enrichment.

Covers the additions in v1.1:

- Sticky ``seen_local`` / ``seen_external`` flags (once true, stay
  true across subsequent messages from the other source).
- Distance enrichment (nautical miles, haversine, computed only
  when ``maritime_reference`` is set + the vessel has a position).
- Reference validation (rejects bad lat/lon shapes, tolerates
  missing display labels).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import json
import math
import unittest
from typing import Any
from unittest.mock import MagicMock

from infrastructure.maritime_buffer import MaritimeBuffer


# Reference point used in the distance tests — downtown Mobile, AL.
# Picked because it is both the operator's actual reference value and
# convenient for hand-checked distances.
_REF_MOBILE: dict[str, Any] = {
    "postal_code": "36602",
    "country":     "US",
    "lat":         30.6944,
    "lon":         -88.0431,
}


def _fake_msg(payload: dict[str, Any]) -> Any:
    """Build a minimal mock paho MQTTMessage with a JSON payload."""
    m: Any = MagicMock()
    m.payload = json.dumps(payload).encode("utf-8")
    return m


class StickySourceFlagsTests(unittest.TestCase):
    """``seen_local`` and ``seen_external`` must never clear once set."""

    def test_local_then_external_keeps_local_true(self) -> None:
        """Vessel first heard locally, then via aisstream — both true."""
        b: MaritimeBuffer = MaritimeBuffer()
        # Local message — no ``source`` field on the AIS-catcher path.
        b._on_message(None, None, _fake_msg({
            "mmsi": 367015290, "lat": 30.7, "lon": -88.0,
        }))
        v: dict[str, Any] = b.vessel(367015290)
        self.assertTrue(v["seen_local"])
        self.assertFalse(v["seen_external"])

        # Now an aisstream message for the same MMSI.
        b._on_message(None, None, _fake_msg({
            "mmsi": 367015290, "lat": 30.71, "lon": -88.01,
            "source": "aisstream",
        }))
        v = b.vessel(367015290)
        self.assertTrue(v["seen_local"], "local must remain sticky")
        self.assertTrue(v["seen_external"])

    def test_external_then_local_keeps_external_true(self) -> None:
        """Reverse order — external sticky across a later local hit."""
        b: MaritimeBuffer = MaritimeBuffer()
        b._on_message(None, None, _fake_msg({
            "mmsi": 232123456, "lat": 28.0, "lon": -90.0,
            "source": "aisstream",
        }))
        b._on_message(None, None, _fake_msg({
            "mmsi": 232123456, "lat": 28.01, "lon": -90.01,
        }))
        v: dict[str, Any] = b.vessel(232123456)
        self.assertTrue(v["seen_local"])
        self.assertTrue(v["seen_external"], "external must remain sticky")


class DistanceEnrichmentTests(unittest.TestCase):
    """``distance_nmi`` is set iff a reference is configured + position known."""

    def test_no_reference_means_no_distance(self) -> None:
        """Buffers built without a reference report distance_nmi None."""
        b: MaritimeBuffer = MaritimeBuffer()
        b._on_message(None, None, _fake_msg({
            "mmsi": 366999999, "lat": 30.7, "lon": -88.0,
        }))
        self.assertIsNone(b.vessel(366999999)["distance_nmi"])
        self.assertIsNone(b.reference)

    def test_reference_yields_haversine_distance(self) -> None:
        """A vessel ~1 nmi from downtown Mobile reads ~1 nmi."""
        b: MaritimeBuffer = MaritimeBuffer(reference=_REF_MOBILE)
        # 1 minute of arc north of the reference is exactly 1 nmi by
        # the definition of nautical mile.
        north_one_nmi: dict[str, Any] = {
            "mmsi": 367111111,
            "lat":  _REF_MOBILE["lat"] + (1.0 / 60.0),
            "lon":  _REF_MOBILE["lon"],
        }
        b._on_message(None, None, _fake_msg(north_one_nmi))
        d: float = b.vessel(367111111)["distance_nmi"]
        # Sphere-vs-WGS-84 + numerical noise; tolerate ~0.5 % drift.
        self.assertAlmostEqual(d, 1.0, places=2)

    def test_reference_without_position_leaves_distance_null(self) -> None:
        """A vessel heard but with no fix yet still has no distance."""
        b: MaritimeBuffer = MaritimeBuffer(reference=_REF_MOBILE)
        # Static-only message — no lat/lon.
        b._on_message(None, None, _fake_msg({
            "mmsi": 367222222, "shipname": "TEST",
        }))
        # vessels() filters to "with position only" by default; use
        # vessel() to inspect a no-position record directly.
        v: Any = b.vessel(367222222)
        self.assertIsNone(v["distance_nmi"])

    def test_invalid_reference_disables_distance(self) -> None:
        """Bad lat/lon types fall through cleanly — no per-vessel crash."""
        bad_ref: dict[str, Any] = {"lat": "thirty", "lon": "minus eighty"}
        b: MaritimeBuffer = MaritimeBuffer(reference=bad_ref)
        self.assertIsNone(b.reference)
        b._on_message(None, None, _fake_msg({
            "mmsi": 367333333, "lat": 30.7, "lon": -88.0,
        }))
        self.assertIsNone(b.vessel(367333333)["distance_nmi"])

    def test_out_of_range_reference_rejected(self) -> None:
        """Lat > 90 / lon > 180 must be rejected at construction."""
        b: MaritimeBuffer = MaritimeBuffer(
            reference={"lat": 95.0, "lon": -88.0},
        )
        self.assertIsNone(b.reference)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
