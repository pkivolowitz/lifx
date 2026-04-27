"""Tests for the NDBC realtime2 parser in maritime/buoy_scraper.py."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import unittest
from typing import Any

from maritime.buoy_scraper import _parse_realtime2, _parse_float


# A real fragment from NDBC station 42012 (Orange Beach, AL) — kept
# verbatim so a future header-column reorder by NDBC fails this test
# loudly rather than silently.
_REAL_NDBC_42012: str = (
    "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE\n"
    "#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft\n"
    "2026 04 27 20 10 110  6.0  7.0    MM    MM    MM  MM 1016.7  23.3  22.9  21.5   MM   MM    MM\n"
    "2026 04 27 20 00 110  6.0  7.0    MM    MM    MM  MM 1016.7  23.3  22.9  21.4   MM -0.3    MM\n"
    "2026 04 27 19 50 110  6.0  7.0   0.3     2   2.8  86 1016.8  23.2  22.9  21.2   MM   MM    MM\n"
)


class ParseFloatTests(unittest.TestCase):

    def test_missing_token_is_none(self) -> None:
        """Documented MM sentinel coerces to None, not 0."""
        self.assertIsNone(_parse_float("MM"))

    def test_empty_is_none(self) -> None:
        """Empty token from a truncated row coerces to None."""
        self.assertIsNone(_parse_float(""))

    def test_numeric_passthrough(self) -> None:
        self.assertEqual(_parse_float("23.5"), 23.5)
        self.assertEqual(_parse_float("0"), 0.0)
        self.assertEqual(_parse_float("-1.2"), -1.2)

    def test_garbage_is_none(self) -> None:
        """Unparseable tokens fall through to None — never raise."""
        self.assertIsNone(_parse_float("abc"))


class ParseRealtime2Tests(unittest.TestCase):

    def test_parses_newest_observation(self) -> None:
        """The first data row after the headers is the result."""
        obs: Any = _parse_realtime2(_REAL_NDBC_42012)
        self.assertIsNotNone(obs)
        # Newest row is 2026-04-27 20:10 UTC.
        self.assertEqual(obs["obs_ts"], "2026-04-27T20:10:00Z")
        # 6.0 m/s ≈ 11.66 kt; 7.0 m/s ≈ 13.61 kt.
        self.assertAlmostEqual(obs["wind_speed_kt"], 11.66, places=2)
        self.assertAlmostEqual(obs["wind_gust_kt"], 13.61, places=2)
        self.assertEqual(obs["wind_dir_deg"], 110.0)
        self.assertEqual(obs["pressure_mb"], 1016.7)
        self.assertEqual(obs["air_temp_c"], 23.3)
        self.assertEqual(obs["water_temp_c"], 22.9)
        # MM-marked fields fall through to None.
        self.assertIsNone(obs["wave_height_m"])
        self.assertIsNone(obs["tide_ft"])

    def test_empty_input_returns_none(self) -> None:
        """No header line, no observation — clean None."""
        self.assertIsNone(_parse_realtime2(""))

    def test_html_404_returns_none(self) -> None:
        """A 404-style HTML body has no #YY header — must return None.

        Real failure mode hit during development: stations without a
        realtime2 file return an HTML 404 page.  The HTTP layer
        already routes those, but the parser must also be safe in
        case some future station returns a body that confuses the
        Content-Type check upstream.
        """
        html: str = (
            "<!DOCTYPE HTML PUBLIC \"-//IETF//DTD HTML 2.0//EN\">\n"
            "<html><head><title>404 Not Found</title></head>\n"
            "<body><h1>Not Found</h1></body></html>\n"
        )
        self.assertIsNone(_parse_realtime2(html))

    def test_truncated_row_refused(self) -> None:
        """A short row (fewer tokens than the header) is rejected."""
        text: str = (
            "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD\n"
            "#yr  mo dy hr mn degT m/s  m/s     m   sec\n"
            "2026 04 27 20 10 110  6.0  7.0\n"
        )
        self.assertIsNone(_parse_realtime2(text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
