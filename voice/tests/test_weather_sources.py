"""Tests for weather_sources — NWS/Open-Meteo providers and failover."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import io
import json
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from voice.coordinator.weather_sources import (
    AirQuality,
    CurrentConditions,
    ForecastPeriod,
    NWSSource,
    OpenMeteoAirQuality,
    OpenMeteoSource,
    WeatherClient,
    WeatherSourceError,
)


def _fake_response(payload: dict[str, Any]) -> MagicMock:
    """Return a context-manager mock that yields ``payload`` as JSON."""
    body: bytes = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = None
    return cm


class TestOpenMeteoCurrent(unittest.TestCase):
    """OpenMeteoSource.current parses the /v1/forecast current payload."""

    def test_parses_current(self) -> None:
        payload = {"current": {
            "temperature_2m": 78.0,
            "apparent_temperature": 85.0,
            "relative_humidity_2m": 72.0,
            "weather_code": 2,
            "wind_speed_10m": 6.0,
        }}
        src = OpenMeteoSource(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   return_value=_fake_response(payload)):
            c = src.current()
        self.assertAlmostEqual(c.temp_f, 78.0)
        self.assertAlmostEqual(c.apparent_f, 85.0)
        self.assertAlmostEqual(c.humidity_pct, 72.0)
        self.assertAlmostEqual(c.wind_mph, 6.0)
        self.assertEqual(c.condition, "partly cloudy")
        self.assertEqual(c.source, "Open-Meteo")

    def test_http_failure_raises(self) -> None:
        src = OpenMeteoSource(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   side_effect=OSError("network down")):
            with self.assertRaises(WeatherSourceError):
                src.current()


class TestOpenMeteoForecast(unittest.TestCase):
    """OpenMeteoSource.forecast reshapes daily into day+night periods."""

    def test_reshapes_daily(self) -> None:
        payload = {"daily": {
            "time": ["2026-04-23", "2026-04-24"],
            "temperature_2m_max": [82.0, 80.0],
            "temperature_2m_min": [65.0, 62.0],
            "precipitation_probability_max": [40, 0],
            "weather_code": [61, 0],
        }}
        src = OpenMeteoSource(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   return_value=_fake_response(payload)):
            periods = src.forecast()
        # 2 days × 2 halves = 4 periods.
        self.assertEqual(len(periods), 4)
        today_day = periods[0]
        today_night = periods[1]
        tomorrow_day = periods[2]
        self.assertEqual(today_day.name, "Today")
        self.assertTrue(today_day.is_daytime)
        self.assertEqual(today_day.temperature_f, 82.0)
        self.assertEqual(today_day.condition, "slight rain")
        self.assertEqual(today_day.precip_probability_pct, 40.0)
        self.assertEqual(today_night.name, "Today night")
        self.assertFalse(today_night.is_daytime)
        self.assertEqual(today_night.temperature_f, 65.0)
        self.assertEqual(tomorrow_day.name, "Tomorrow")


class TestNWSSourceCurrent(unittest.TestCase):
    """NWSSource.current performs points → station → observations hops."""

    def test_two_hop_resolution(self) -> None:
        points_payload = {"properties": {
            "forecast": "https://api.weather.gov/gridpoints/MOB/50,52/forecast",
            "observationStations":
                "https://api.weather.gov/gridpoints/MOB/50,52/stations",
        }}
        stations_payload = {"features": [
            {"properties": {"stationIdentifier": "KMOB"}},
            {"properties": {"stationIdentifier": "KPQL"}},
        ]}
        obs_payload = {"properties": {
            "temperature": {"value": 25.0, "unitCode": "wmoUnit:degC"},
            "relativeHumidity": {"value": 70.0, "unitCode": "wmoUnit:percent"},
            "windSpeed": {"value": 16.0934, "unitCode": "wmoUnit:km_h-1"},
            "heatIndex": {"value": 28.0, "unitCode": "wmoUnit:degC"},
            "textDescription": "Mostly Cloudy",
        }}
        responses = iter([
            _fake_response(points_payload),
            _fake_response(stations_payload),
            _fake_response(obs_payload),
        ])
        src = NWSSource(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   side_effect=lambda req, timeout=None: next(responses)):
            c = src.current()
        self.assertAlmostEqual(c.temp_f, 77.0)
        self.assertAlmostEqual(c.apparent_f, 82.4)
        self.assertAlmostEqual(c.humidity_pct, 70.0)
        self.assertAlmostEqual(c.wind_mph, 10.0, places=1)
        self.assertEqual(c.condition, "Mostly Cloudy")
        self.assertEqual(c.source, "NWS")

    def test_null_values_tolerated(self) -> None:
        """NWS QC may null out any measurement; returned as None."""
        points_payload = {"properties": {
            "forecast": "http://example/forecast",
            "observationStations": "http://example/stations",
        }}
        stations_payload = {"features": [
            {"properties": {"stationIdentifier": "KMOB"}},
        ]}
        obs_payload = {"properties": {
            "temperature": {"value": None},
            "relativeHumidity": {"value": None},
            "windSpeed": {"value": None},
            "heatIndex": {"value": None},
            "windChill": {"value": None},
            "textDescription": "",
        }}
        responses = iter([
            _fake_response(points_payload),
            _fake_response(stations_payload),
            _fake_response(obs_payload),
        ])
        src = NWSSource(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   side_effect=lambda req, timeout=None: next(responses)):
            c = src.current()
        self.assertIsNone(c.temp_f)
        self.assertIsNone(c.humidity_pct)
        self.assertEqual(c.condition, "unknown conditions")

    def test_points_cache_reused(self) -> None:
        """Second call to current() reuses the cached points + station."""
        points_payload = {"properties": {
            "forecast": "http://example/forecast",
            "observationStations": "http://example/stations",
        }}
        stations_payload = {"features": [
            {"properties": {"stationIdentifier": "KMOB"}},
        ]}
        obs_payload = {"properties": {
            "temperature": {"value": 20.0},
            "relativeHumidity": {"value": 50.0},
            "windSpeed": {"value": 0.0},
            "textDescription": "Clear",
        }}
        responses = [
            _fake_response(points_payload),
            _fake_response(stations_payload),
            _fake_response(obs_payload),
            _fake_response(obs_payload),  # Second current() only re-fetches obs.
        ]
        it = iter(responses)
        src = NWSSource(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   side_effect=lambda req, timeout=None: next(it)):
            src.current()
            src.current()
        # If caching broke, we would exhaust `responses` and the iterator
        # would raise StopIteration.

    def test_network_failure_raises(self) -> None:
        src = NWSSource(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   side_effect=OSError("DNS failed")):
            with self.assertRaises(WeatherSourceError):
                src.current()


class TestWeatherClientFailover(unittest.TestCase):
    """WeatherClient primary→fallback semantics and retry notification."""

    def _mk_source(self, name: str, current_ret=None, current_exc=None,
                   forecast_ret=None, forecast_exc=None):
        src = MagicMock()
        src.name = name
        if current_exc is not None:
            src.current.side_effect = current_exc
        else:
            src.current.return_value = current_ret
        if forecast_exc is not None:
            src.forecast.side_effect = forecast_exc
        else:
            src.forecast.return_value = forecast_ret
        return src

    def _dummy_current(self, source: str) -> CurrentConditions:
        return CurrentConditions(
            temp_f=70.0, apparent_f=70.0, humidity_pct=50.0,
            wind_mph=5.0, condition="clear", source=source,
        )

    def test_primary_success_no_fallback(self) -> None:
        primary = self._mk_source("A", current_ret=self._dummy_current("A"))
        fallback = self._mk_source("B", current_ret=self._dummy_current("B"))
        notice = MagicMock()
        client = WeatherClient(primary, fallback, on_fallback=notice)
        c = client.current()
        self.assertEqual(c.source, "A")
        fallback.current.assert_not_called()
        notice.assert_not_called()

    def test_primary_failure_notifies_and_falls_back(self) -> None:
        primary = self._mk_source(
            "A", current_exc=WeatherSourceError("boom"),
        )
        fallback = self._mk_source("B", current_ret=self._dummy_current("B"))
        notice = MagicMock()
        client = WeatherClient(primary, fallback, on_fallback=notice)
        c = client.current()
        self.assertEqual(c.source, "B")
        notice.assert_called_once()
        msg: str = notice.call_args[0][0]
        self.assertIn("Retrying", msg)

    def test_fallback_failure_propagates(self) -> None:
        primary = self._mk_source(
            "A", current_exc=WeatherSourceError("primary down"),
        )
        fallback = self._mk_source(
            "B", current_exc=WeatherSourceError("fallback down"),
        )
        client = WeatherClient(primary, fallback)
        with self.assertRaises(WeatherSourceError):
            client.current()

    def test_notify_exception_does_not_break_fallback(self) -> None:
        """A crashing notice callback must not prevent the fallback call."""
        primary = self._mk_source(
            "A", current_exc=WeatherSourceError("down"),
        )
        fallback = self._mk_source(
            "B", current_ret=self._dummy_current("B"),
        )
        bad_notice = MagicMock(side_effect=RuntimeError("TTS exploded"))
        client = WeatherClient(primary, fallback, on_fallback=bad_notice)
        c = client.current()
        self.assertEqual(c.source, "B")

    def test_forecast_failover(self) -> None:
        period = ForecastPeriod(
            name="Today", is_daytime=True, temperature_f=80.0,
            condition="sunny", precip_probability_pct=0.0, wind_mph_desc="",
        )
        primary = self._mk_source(
            "A", forecast_exc=WeatherSourceError("forecast down"),
        )
        fallback = self._mk_source("B", forecast_ret=[period])
        client = WeatherClient(primary, fallback)
        periods = client.forecast()
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0].name, "Today")


class TestOpenMeteoAirQuality(unittest.TestCase):
    """OpenMeteoAirQuality.current parses air-quality + pollen fields."""

    def test_parses_all_fields(self) -> None:
        payload = {"current": {
            "pm2_5": 12.0, "pm10": 25.0, "ozone": 60.0,
            "us_aqi": 42.0, "uv_index": 7.0,
            "alder_pollen": 0.0, "birch_pollen": 5.0,
            "grass_pollen": 120.0, "ragweed_pollen": 0.0,
            # Missing mugwort/olive → absent from result dict.
        }}
        aq_src = OpenMeteoAirQuality(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   return_value=_fake_response(payload)):
            aq = aq_src.current()
        self.assertEqual(aq.pm2_5, 12.0)
        self.assertEqual(aq.us_aqi, 42.0)
        self.assertEqual(aq.uv_index, 7.0)
        self.assertEqual(aq.pollen.get("grass_pollen"), 120.0)
        self.assertNotIn("mugwort_pollen", aq.pollen)

    def test_failure_raises(self) -> None:
        aq_src = OpenMeteoAirQuality(30.69, -88.04)
        with patch("voice.coordinator.weather_sources.urllib.request.urlopen",
                   side_effect=OSError("unreachable")):
            with self.assertRaises(WeatherSourceError):
                aq_src.current()


if __name__ == "__main__":
    unittest.main()
