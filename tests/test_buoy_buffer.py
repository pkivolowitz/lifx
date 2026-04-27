"""Tests for BuoyBuffer's per-station current-state caching."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import json
import unittest
from typing import Any
from unittest.mock import MagicMock

from infrastructure.buoy_buffer import BuoyBuffer


def _fake_msg(payload: dict[str, Any]) -> Any:
    m: Any = MagicMock()
    m.payload = json.dumps(payload).encode("utf-8")
    return m


class BuoyBufferTests(unittest.TestCase):

    def test_records_current_obs_per_station(self) -> None:
        """One observation per station, latest wins."""
        b: BuoyBuffer = BuoyBuffer()
        b._on_message(None, None, _fake_msg({
            "station_id": "42012",
            "name": "Orange Beach",
            "lat": 30.065, "lon": -87.555,
            "obs_ts": "2026-04-27T20:00:00Z",
            "pressure_mb": 1016.7,
        }))
        b._on_message(None, None, _fake_msg({
            "station_id": "42012",
            "name": "Orange Beach",
            "lat": 30.065, "lon": -87.555,
            "obs_ts": "2026-04-27T20:10:00Z",
            "pressure_mb": 1016.5,
        }))
        s: Any = b.station("42012")
        self.assertIsNotNone(s)
        self.assertEqual(s["obs_ts"], "2026-04-27T20:10:00Z")
        self.assertEqual(s["pressure_mb"], 1016.5)

    def test_multiple_stations_independent(self) -> None:
        """Different station ids don't clobber each other."""
        b: BuoyBuffer = BuoyBuffer()
        b._on_message(None, None, _fake_msg({
            "station_id": "42012", "obs_ts": "2026-04-27T20:00:00Z",
            "pressure_mb": 1016.7,
        }))
        b._on_message(None, None, _fake_msg({
            "station_id": "42040", "obs_ts": "2026-04-27T20:00:00Z",
            "pressure_mb": 1018.1,
        }))
        stations: list[dict[str, Any]] = b.stations()
        ids: set[str] = {s["station_id"] for s in stations}
        self.assertEqual(ids, {"42012", "42040"})

    def test_missing_station_id_dropped(self) -> None:
        """Messages without a string station_id are ignored, not crashed."""
        b: BuoyBuffer = BuoyBuffer()
        b._on_message(None, None, _fake_msg({"obs_ts": "2026-04-27T20:00:00Z"}))
        b._on_message(None, None, _fake_msg({"station_id": None}))
        b._on_message(None, None, _fake_msg({"station_id": ""}))
        self.assertEqual(b.stations(), [])

    def test_station_returns_none_for_unknown(self) -> None:
        b: BuoyBuffer = BuoyBuffer()
        self.assertIsNone(b.station("42012"))

    def test_stats_counts_messages(self) -> None:
        b: BuoyBuffer = BuoyBuffer()
        for _ in range(5):
            b._on_message(None, None, _fake_msg({
                "station_id": "42012", "obs_ts": "2026-04-27T20:00:00Z",
            }))
        s: dict[str, Any] = b.stats()
        self.assertEqual(s["msg_count"], 5)
        self.assertEqual(s["n_stations"], 1)
        self.assertIsNotNone(s["last_msg_ts"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
