"""NDBC buoy → MQTT bridge.

Polls each operator-configured National Data Buoy Center station's
``realtime2/<id>.txt`` plaintext feed every 10 minutes, parses the
fixed-column observation, normalizes units (m/s → knots), and
publishes one JSON payload per station per fresh observation onto
``glowup/maritime/buoy/<station_id>``.

Two consumers on the hub side:
  - :class:`infrastructure.buoy_buffer.BuoyBuffer` for the live
    /maritime map layer (current state + 24h pressure ring).
  - :class:`infrastructure.buoy_logger.BuoyLogger` for postgres
    persistence (drives the /buoys/<station> history page).

Configuration
-------------

The set of stations to track lives in ``/etc/glowup/site.json`` under
``maritime_buoys`` as a list of ``{id, name, lat, lon}`` objects.
The operator copies values from each station's NDBC page; we do
not consult NDBC's master station table at runtime so the scraper
has a single failure surface (the per-station realtime2 fetch),
not two.

NDBC update cadence is ~10 min; we poll every 5 min and dedupe
by observation timestamp at the buffer + logger level (postgres
has a UNIQUE (station_id, obs_ts) constraint).  The 5-minute
poll catches both the on-time tick and the next-tick delivery
when NDBC slips slightly.

Format reference (``#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE``):
  YY MM DD hh mm  date/time UTC
  WDIR            wind direction degrees true
  WSPD / GST      wind speed / gust m/s
  WVHT            significant wave height m
  DPD / APD       dominant / average wave period s
  MWD             mean wave direction degT
  PRES            barometric pressure hPa (mb)
  ATMP / WTMP     air / water temperature °C
  DEWP            dewpoint °C
  VIS             visibility nmi
  PTDY            3-hour pressure tendency hPa (signed)
  TIDE            water level ft

Missing values are encoded "MM" — coerced to None during parse.

Why not push HTTPS straight at postgres?  Two reasons.  The MQTT
indirection mirrors how every other sensor in glowup feeds the hub
(thermal, power, meters, AIS), so all of the existing logger /
retention / dashboard plumbing is reused without one-off paths.
And running the scraper on any internet-connected host (today the
hub itself; tomorrow some other box) requires nothing more than
pointing it at the hub broker via site.json — no postgres
credentials leave the hub.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import argparse
import json
import logging
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

# Site config — hub broker host/port resolution matches every other
# sensor module in the tree (meters/publisher.py, ble/sensor.py, etc).
try:
    from glowup_site import site as _site
except ImportError:
    _site = None


logger: logging.Logger = logging.getLogger("glowup.maritime.buoys")


# ─── Constants ────────────────────────────────────────────────────────

# Default poll interval — half NDBC's update cadence, so we catch
# both the on-time post and the next-tick delivery if NDBC slips.
# 5 min × N stations is well under any reasonable rate-limit (and
# NDBC publishes no rate-limit guidance for these plaintext files).
_DEFAULT_POLL_INTERVAL_S: int = 300

# Per-fetch HTTP timeout.  realtime2 files are small (a few KB)
# but residential / cellular uplinks can be slow during outages —
# 30 s is long enough to ride through a weak link without blocking
# the whole loop indefinitely.
_HTTP_TIMEOUT_S: int = 30

# Realtime2 base URL.  Public NDBC service, no key, no auth.
_NDBC_REALTIME2_BASE: str = "https://www.ndbc.noaa.gov/data/realtime2"

# User-Agent for NDBC fetches.  Plain HTTP libraries' default UAs
# (``Python-urllib/x.y``) are sometimes blocked by federal HTTP
# layers; identify the project + contact for reachability.  The
# contact half pulls from site.json to keep the public repo clean.
_USER_AGENT_NAME: str = "glowup-maritime-buoys/1.0"

# MQTT topic prefix.  Per-station suffix appended at publish time.
_TOPIC_PREFIX: str = "glowup/maritime/buoy"

# QoS for buoy publishes.  At-least-once is right: a missed obs is
# a missed row in the history dashboard, and the cost of an
# occasional duplicate is zero given the UNIQUE (station_id, obs_ts)
# constraint at the postgres logger.
_MQTT_QOS: int = 1

# m/s → knots conversion factor.  Keeping wind in knots throughout
# the maritime stack (vessels' AIS speed is also kt) matches the
# unit displayed on every chartplotter and AIS receiver.
_MS_TO_KNOTS: float = 1.94384

# How many recent observation timestamps to retain per station for
# duplicate suppression at the publish layer.  A typical realtime2
# file carries ~720 rows (5-day window at 10-min cadence) — but we
# only ever look at the newest, so a small dedup window suffices.
_DEDUP_WINDOW: int = 8

# Sentinel string NDBC uses for missing values across every column.
# Coerce to None at parse time.
_MISSING_TOKEN: str = "MM"

# Column-name regex for the header line.  NDBC files start with a
# ``#YY  MM DD hh mm ...`` header — first ``#`` line.  We index by
# header name rather than column position so a future column
# addition doesn't shift our parse.
_HEADER_RE: re.Pattern[str] = re.compile(r"^#YY\s")


# ─── Parser ───────────────────────────────────────────────────────────

def _parse_float(token: str) -> Optional[float]:
    """Coerce an NDBC numeric token to float, or None for missing.

    Returns None for ``MM`` (the documented missing-value sentinel)
    and for anything that doesn't parse as a finite float.  Logging
    is at debug — these are routine data gaps, not failures.
    """
    if token == _MISSING_TOKEN or not token:
        return None
    try:
        v: float = float(token)
    except ValueError:
        logger.debug("non-numeric NDBC token %r", token)
        return None
    return v


def _parse_realtime2(text: str) -> Optional[dict[str, Any]]:
    """Parse the newest observation row out of a realtime2 file.

    Returns ``None`` if the file lacks a recognisable header or any
    data row (404-style HTML bodies, empty files, half-truncated
    fetches).  The newest row is the FIRST data row after the two
    leading ``#``-prefixed header lines — NDBC publishes
    descending-time.
    """
    lines: list[str] = text.splitlines()
    header_idx: int = -1
    for i, line in enumerate(lines):
        if _HEADER_RE.match(line):
            header_idx = i
            break
    if header_idx < 0:
        return None
    # Header is ``#YY  MM DD hh mm WDIR ...`` — strip the leading #
    # and tokenize on whitespace.
    header_tokens: list[str] = lines[header_idx].lstrip("#").split()
    # First non-#-prefixed line after the header pair is the newest
    # observation.  The unit row also begins with #yr — skip it.
    data_line: str = ""
    for line in lines[header_idx + 1:]:
        if line.startswith("#"):
            continue
        if line.strip():
            data_line = line
            break
    if not data_line:
        return None
    tokens: list[str] = data_line.split()
    if len(tokens) < len(header_tokens):
        # Truncated row — refuse rather than mis-align columns.
        logger.warning(
            "NDBC row has %d tokens, header expects %d — refusing",
            len(tokens), len(header_tokens),
        )
        return None
    by_name: dict[str, str] = dict(zip(header_tokens, tokens))

    # Date / time → ISO-8601 UTC.  NDBC publishes UTC explicitly.
    try:
        obs_dt: datetime = datetime(
            int(by_name["YY"]),
            int(by_name["MM"]),
            int(by_name["DD"]),
            int(by_name["hh"]),
            int(by_name["mm"]),
            tzinfo=timezone.utc,
        )
    except (KeyError, ValueError) as exc:
        logger.warning("NDBC date parse failed: %s", exc)
        return None

    # Build a unit-normalized observation.  Wind in knots (maritime
    # convention); pressure in mb; everything else passes through.
    wind_speed_ms: Optional[float] = _parse_float(by_name.get("WSPD", ""))
    wind_gust_ms: Optional[float] = _parse_float(by_name.get("GST", ""))
    obs: dict[str, Any] = {
        "obs_ts":          obs_dt.isoformat().replace("+00:00", "Z"),
        "wind_dir_deg":    _parse_float(by_name.get("WDIR", "")),
        "wind_speed_kt":   None if wind_speed_ms is None else round(wind_speed_ms * _MS_TO_KNOTS, 2),
        "wind_gust_kt":    None if wind_gust_ms is None else round(wind_gust_ms * _MS_TO_KNOTS, 2),
        "wave_height_m":   _parse_float(by_name.get("WVHT", "")),
        "wave_period_s":   _parse_float(by_name.get("DPD", "")),
        "wave_period_avg_s": _parse_float(by_name.get("APD", "")),
        "wave_dir_deg":    _parse_float(by_name.get("MWD", "")),
        "pressure_mb":     _parse_float(by_name.get("PRES", "")),
        "pressure_tendency_mb": _parse_float(by_name.get("PTDY", "")),
        "air_temp_c":      _parse_float(by_name.get("ATMP", "")),
        "water_temp_c":    _parse_float(by_name.get("WTMP", "")),
        "dewpoint_c":      _parse_float(by_name.get("DEWP", "")),
        "visibility_nmi":  _parse_float(by_name.get("VIS", "")),
        "tide_ft":         _parse_float(by_name.get("TIDE", "")),
    }
    return obs


# ─── Scraper ──────────────────────────────────────────────────────────

class BuoyScraper:
    """Daemon — polls NDBC stations, publishes new observations.

    Args:
        broker_host:    MQTT broker hostname / IP.  Required.
        broker_port:    MQTT broker TCP port.
        stations:       List of ``{id, name, lat, lon}`` dicts (the
                        operator's site.json maritime_buoys).
        poll_interval_s: Seconds between full-fleet polls.
        user_agent:     HTTP User-Agent for NDBC fetches.  Federal
                        HTTP layers sometimes block the default
                        ``Python-urllib`` UA; including the
                        operator's contact_email in production is
                        the polite NWS / NDBC convention.
    """

    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        stations: list[dict[str, Any]],
        poll_interval_s: int = _DEFAULT_POLL_INTERVAL_S,
        user_agent: str = _USER_AGENT_NAME,
    ) -> None:
        """See class docstring."""
        if not _HAS_PAHO:
            raise RuntimeError("paho-mqtt not installed; install paho-mqtt")
        self._broker_host: str = broker_host
        self._broker_port: int = broker_port
        self._stations: list[dict[str, Any]] = stations
        self._poll_interval_s: int = poll_interval_s
        self._user_agent: str = user_agent
        self._client: "mqtt.Client" = mqtt.Client(
            client_id=f"glowup-buoy-scraper-{int(time.time())}",
        )
        # Per-station: the last obs_ts we successfully published.
        # Suppresses re-publish on each poll cycle when NDBC hasn't
        # rolled forward yet.
        self._last_published_obs_ts: dict[str, str] = {}
        self._stop: bool = False

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> int:
        """Main loop.  Returns process exit code."""
        try:
            self._client.connect(
                self._broker_host, self._broker_port, keepalive=60,
            )
        except Exception as exc:
            logger.error(
                "MQTT connect to %s:%d failed: %s",
                self._broker_host, self._broker_port, exc,
            )
            return 2
        self._client.loop_start()
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        logger.info(
            "buoy scraper started — broker=%s:%d stations=%d interval=%ds",
            self._broker_host, self._broker_port,
            len(self._stations), self._poll_interval_s,
        )
        try:
            while not self._stop:
                self._poll_all()
                # Sleep in 1-second slices so SIGTERM / SIGINT
                # gets a quick exit instead of waiting up to a
                # full poll interval.
                slept: int = 0
                while slept < self._poll_interval_s and not self._stop:
                    time.sleep(1)
                    slept += 1
        finally:
            self._client.loop_stop()
            try:
                self._client.disconnect()
            except Exception as exc:
                logger.debug("MQTT disconnect: %s", exc)
        return 0

    def _on_signal(self, signum: int, _frame: Any) -> None:
        """SIGTERM / SIGINT → drop out of the poll loop cleanly."""
        logger.info("buoy scraper stopping (signal %d)", signum)
        self._stop = True

    # -- polling -------------------------------------------------------------

    def _poll_all(self) -> None:
        """One pass across every configured station."""
        for s in self._stations:
            sid: Any = s.get("id")
            if not isinstance(sid, str) or not sid:
                logger.warning("skipping station with no id: %r", s)
                continue
            try:
                self._poll_station(s)
            except Exception as exc:
                # Never let one station's failure break the loop.
                # NDBC station files come and go (decommissioning,
                # maintenance) and we want the rest of the fleet
                # to keep flowing.
                logger.warning(
                    "buoy %s poll failed: %s", sid, exc,
                )

    def _poll_station(self, station: dict[str, Any]) -> None:
        """Fetch, parse, and publish one station's newest observation."""
        sid: str = station["id"]
        url: str = f"{_NDBC_REALTIME2_BASE}/{sid}.txt"
        req: urllib.request.Request = urllib.request.Request(
            url, headers={"User-Agent": self._user_agent},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                text: str = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # 404 = station has no realtime2 file (CMAN-only / decom).
            # Log once at warning, then settle into debug so the
            # journal doesn't spam every poll.
            if exc.code == 404:
                if sid not in self._last_published_obs_ts:
                    logger.warning(
                        "buoy %s: no realtime2 file (HTTP 404); "
                        "will keep retrying silently",
                        sid,
                    )
                    # Stash a sentinel so we don't re-warn.
                    self._last_published_obs_ts[sid] = ""
                return
            raise
        obs: Optional[dict[str, Any]] = _parse_realtime2(text)
        if obs is None:
            logger.debug("buoy %s: no parseable observation", sid)
            return
        if obs["obs_ts"] == self._last_published_obs_ts.get(sid):
            # Same observation we already published — NDBC hasn't
            # rolled forward.  Silent no-op.
            return
        payload: dict[str, Any] = {
            "station_id": sid,
            "name":       station.get("name") or sid,
            "lat":        station.get("lat"),
            "lon":        station.get("lon"),
            **obs,
        }
        topic: str = f"{_TOPIC_PREFIX}/{sid}"
        info: Any = self._client.publish(
            topic, json.dumps(payload), qos=_MQTT_QOS,
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning(
                "buoy %s MQTT publish rc=%d", sid, info.rc,
            )
            return
        self._last_published_obs_ts[sid] = obs["obs_ts"]
        logger.info(
            "buoy %s @ %s — wind %s/%s kt, pres %s mb, water %s °C",
            sid,
            obs["obs_ts"],
            obs["wind_speed_kt"],
            obs["wind_gust_kt"],
            obs["pressure_mb"],
            obs["water_temp_c"],
        )


# ─── CLI ──────────────────────────────────────────────────────────────

def _resolve_broker(args: argparse.Namespace) -> tuple[str, int]:
    """Resolve hub broker host/port — argv > site.json.

    Mirrors the resolution order in meters/publisher.py and the rest
    of the sensor modules, so the operator gets one consistent rule:
    site.json drives production; --broker / --port override for
    one-off debug.
    """
    host: Optional[str] = args.broker
    port: int = args.port
    if host is None and _site is not None:
        host = _site.get("hub_broker")
        port = int(_site.get("hub_port", port))
    if not host:
        sys.exit(
            "no MQTT broker configured: set hub_broker in /etc/glowup/"
            "site.json or pass --broker"
        )
    return host, port


def _resolve_stations(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Resolve station list — argv > site.json.maritime_buoys.

    site.json carries a list of ``{id, name, lat, lon}`` dicts.  The
    --station CLI flag overrides for ad-hoc dev runs (id-only; lat/
    lon will be None and the dashboard's map layer will skip the
    marker, but data still flows to the buffer + logger).
    """
    if args.station:
        return [{"id": s} for s in args.station]
    if _site is not None:
        cfg: Any = _site.get("maritime_buoys")
        if isinstance(cfg, list):
            return [s for s in cfg if isinstance(s, dict)]
    return []


def main() -> int:
    """CLI entry point."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="glowup-buoy-scraper",
        description="Scrape NDBC realtime2 buoy observations and publish to MQTT.",
    )
    parser.add_argument("--broker", help="MQTT broker host (default: site.json hub_broker)")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--station", action="append",
                        help="NDBC station id (repeatable; default: site.json maritime_buoys)")
    parser.add_argument("--interval", type=int, default=_DEFAULT_POLL_INTERVAL_S,
                        help=f"poll interval seconds (default {_DEFAULT_POLL_INTERVAL_S})")
    parser.add_argument("--log-level", default="INFO")
    args: argparse.Namespace = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    host, port = _resolve_broker(args)
    stations: list[dict[str, Any]] = _resolve_stations(args)
    if not stations:
        logger.warning(
            "no buoy stations configured — set maritime_buoys in "
            "/etc/glowup/site.json or pass --station; idle loop"
        )
    scraper: BuoyScraper = BuoyScraper(
        broker_host=host,
        broker_port=port,
        stations=stations,
        poll_interval_s=args.interval,
    )
    return scraper.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
