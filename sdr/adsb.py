"""GlowUp ADS-B service — dump1090 aircraft tracking published to hub MQTT.

Service-pattern producer running on a dedicated Pi with an RTL-SDR
dongle tuned to 1090 MHz.  Spawns ``dump1090-mutability`` (or
``dump1090-fa``) and polls its JSON endpoint for aircraft data,
then publishes to the hub mosquitto.

Architecture::

    SDR Pi: RTL-SDR dongle → dump1090 (1090 MHz, ADS-B)
                              ↓
                       localhost:8080/data/aircraft.json
                              ↓
                        this adsb.py (poll + publish)
                              ↓
                        cross-host MQTT publish
                              ↓
                          hub mosquitto (.214)
                              ↓
               glowup/sdr/adsb/aircraft   → full aircraft list
               glowup/signals/{icao}:*    → per-aircraft signals

The dashboard consumes glowup/sdr/adsb/aircraft for the map/table
view.  Individual aircraft signals feed the SOE pipeline for
automations (e.g., alert when a specific tail number appears).

Usage::

    python3 -m sdr.adsb --config /etc/glowup/adsb_config.json
    python3 -m sdr.adsb --hub-broker <broker-host>

Requires:
    - dump1090-mutability or dump1090-fa installed and running
    - RTL-SDR dongle plugged in, dedicated to 1090 MHz
    - paho-mqtt (pip install paho-mqtt)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.sdr_adsb")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hub broker — read from GLB_HUB_BROKER, no fallback IP.  Set via
# EnvironmentFile=-/etc/default/glowup-adsb in the systemd unit.
DEFAULT_HUB_BROKER: str | None = os.environ.get("GLB_HUB_BROKER") or None
DEFAULT_MQTT_PORT: int = int(os.environ.get("GLB_HUB_PORT", "1883"))

# dump1090 JSON endpoint — default for dump1090-mutability.
DEFAULT_DUMP1090_URL: str = "http://localhost:8080/data/aircraft.json"

# Poll interval — how often to fetch aircraft.json from dump1090.
# 1 second matches dump1090's update rate.
POLL_INTERVAL_S: float = 1.0

# MQTT topics.
AIRCRAFT_TOPIC: str = "glowup/sdr/adsb/aircraft"
SIGNAL_PREFIX: str = "glowup/signals"

# Maximum aircraft age (seconds) before dropping from the published
# list.  dump1090 keeps stale aircraft for ~60s; we mirror that.
MAX_AIRCRAFT_AGE_S: float = 60.0

# Minimum seconds between per-aircraft signal publishes (dedup).
DEDUP_INTERVAL_S: float = 5.0


# ---------------------------------------------------------------------------
# MQTT publisher
# ---------------------------------------------------------------------------

class AdsbPublisher:
    """Publishes ADS-B aircraft data to the hub mosquitto."""

    def __init__(
        self,
        hub_broker: str = DEFAULT_HUB_BROKER,
        hub_port: int = DEFAULT_MQTT_PORT,
    ) -> None:
        self._broker: str = hub_broker
        self._port: int = hub_port
        self._client: Any = None
        self._connected: bool = False
        self._last_pub: dict[str, float] = {}

    def connect(self) -> None:
        """Open the MQTT client."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError("pip install paho-mqtt")

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"glowup-adsb-{int(time.time())}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()
        logger.info(
            "ADS-B→hub publisher connecting to %s:%d",
            self._broker, self._port,
        )

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected to %s:%d", self._broker, self._port)
        else:
            logger.warning("MQTT connect failed rc=%s", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            logger.warning("MQTT disconnected rc=%d", rc)

    def disconnect(self) -> None:
        """Clean shutdown."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    def publish_aircraft_list(self, aircraft: list[dict]) -> None:
        """Publish the full aircraft list as a single JSON message."""
        payload: str = json.dumps({
            "aircraft": aircraft,
            "count": len(aircraft),
            "timestamp": time.time(),
        })
        self._pub(AIRCRAFT_TOPIC, payload)

    def publish_aircraft_signals(self, ac: dict) -> None:
        """Publish individual aircraft signals for SOE pipeline.

        Only publishes if the aircraft has a hex (ICAO) identifier.
        Deduplicates per ICAO to avoid flooding.
        """
        icao: Optional[str] = ac.get("hex")
        if not icao:
            return

        now: float = time.time()
        last: float = self._last_pub.get(icao, 0.0)
        if now - last < DEDUP_INTERVAL_S:
            return
        self._last_pub[icao] = now

        label: str = f"adsb_{icao.strip().lower()}"

        # Publish altitude, speed, heading as individual signals.
        for field, prop in [
            ("alt_baro", "altitude"),
            ("gs", "speed"),
            ("track", "heading"),
            ("lat", "latitude"),
            ("lon", "longitude"),
        ]:
            value: Any = ac.get(field)
            if value is not None:
                self._pub(
                    f"{SIGNAL_PREFIX}/{label}:{prop}",
                    str(value),
                )

    def _pub(self, topic: str, payload: str) -> None:
        """Low-level publish."""
        if not self._client:
            return
        try:
            info = self._client.publish(topic, payload, qos=0, retain=False)
            if info.rc != 0:
                logger.warning("publish %s rc=%s", topic, info.rc)
        except Exception as exc:
            logger.warning("publish %s: %s", topic, exc)


# ---------------------------------------------------------------------------
# dump1090 manager
# ---------------------------------------------------------------------------

class Dump1090Runner:
    """Manages the dump1090 subprocess and polls aircraft data."""

    def __init__(
        self,
        dump1090_url: str = DEFAULT_DUMP1090_URL,
        device_index: int = 0,
        gain: Optional[str] = None,
        launch: bool = True,
    ) -> None:
        self._url: str = dump1090_url
        self._device_index: int = device_index
        self._gain: Optional[str] = gain
        self._launch: bool = launch
        self._proc: Optional[subprocess.Popen] = None
        self._running: bool = False

    def start(self) -> None:
        """Start dump1090 if launch=True."""
        self._running = True
        if self._launch:
            self._spawn()

    def stop(self) -> None:
        """Stop dump1090."""
        self._running = False
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
            except Exception as exc:
                logger.debug("Error stopping dump1090 process: %s", exc)

    def _spawn(self) -> None:
        """Spawn the dump1090 process."""
        # Try dump1090-mutability first, then dump1090-fa, then dump1090.
        for binary in ["dump1090-mutability", "dump1090-fa", "dump1090"]:
            cmd: list[str] = [
                binary,
                "--device-index", str(self._device_index),
                "--net",
                "--quiet",
            ]
            if self._gain is not None:
                cmd.extend(["--gain", self._gain])
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                # Log stderr in background.
                t = threading.Thread(
                    target=self._stderr_reader, daemon=True,
                )
                t.start()
                logger.info(
                    "Started %s (pid=%d, device=%d)",
                    binary, self._proc.pid, self._device_index,
                )
                return
            except FileNotFoundError:
                continue
        logger.error(
            "dump1090 not found — install with: "
            "sudo apt install dump1090-mutability"
        )
        self._running = False

    def _stderr_reader(self) -> None:
        """Drain dump1090 stderr."""
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            line = line.strip()
            if line:
                logger.debug("[dump1090] %s", line)

    def poll_aircraft(self) -> list[dict]:
        """Fetch current aircraft from dump1090's JSON endpoint.

        Returns:
            List of aircraft dicts, or empty list on error.
        """
        try:
            req = urllib.request.Request(self._url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data: dict = json.loads(resp.read())
                aircraft: list[dict] = data.get("aircraft", [])
                # Filter stale aircraft.
                now_s: float = data.get("now", time.time())
                return [
                    ac for ac in aircraft
                    if ac.get("seen", 999) < MAX_AIRCRAFT_AGE_S
                ]
        except Exception as exc:
            logger.debug("dump1090 poll failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

def run_daemon(
    config_path: Optional[str] = None,
    hub_broker: str = DEFAULT_HUB_BROKER,
    hub_port: int = DEFAULT_MQTT_PORT,
    dump1090_url: str = DEFAULT_DUMP1090_URL,
    device_index: int = 0,
    gain: Optional[str] = None,
    launch: bool = True,
) -> None:
    """Run the ADS-B service daemon.

    Args:
        config_path:  Optional JSON config file.
        hub_broker:   Hub mosquitto address.
        hub_port:     Hub mosquitto port.
        dump1090_url: dump1090 aircraft.json URL.
        device_index: RTL-SDR device index.
        gain:         RTL-SDR gain (None = auto).
        launch:       If True, spawn dump1090. If False, assume it's
                      already running (e.g., as a separate service).
    """
    if config_path is not None:
        with open(config_path) as f:
            config: dict = json.load(f)
        hub_broker = config.get("hub_broker", hub_broker)
        hub_port = config.get("hub_port", hub_port)
        dump1090_url = config.get("dump1090_url", dump1090_url)
        device_index = config.get("device_index", device_index)
        gain = config.get("gain", gain)
        launch = config.get("launch_dump1090", launch)

    publisher = AdsbPublisher(hub_broker=hub_broker, hub_port=hub_port)
    publisher.connect()

    runner = Dump1090Runner(
        dump1090_url=dump1090_url,
        device_index=device_index,
        gain=gain,
        launch=launch,
    )
    runner.start()

    # Give dump1090 a moment to start its HTTP server.
    time.sleep(3)

    logger.info(
        "ADS-B service starting — polling %s every %.0fs, "
        "hub=%s:%d",
        dump1090_url, POLL_INTERVAL_S, hub_broker, hub_port,
    )

    peak_aircraft: int = 0

    try:
        while True:
            aircraft: list[dict] = runner.poll_aircraft()
            if aircraft:
                publisher.publish_aircraft_list(aircraft)
                for ac in aircraft:
                    publisher.publish_aircraft_signals(ac)
                if len(aircraft) > peak_aircraft:
                    peak_aircraft = len(aircraft)
                    logger.info(
                        "New peak: %d aircraft overhead", peak_aircraft,
                    )
            time.sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        publisher.disconnect()
        logger.info(
            "ADS-B service stopped — peak %d aircraft", peak_aircraft,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="GlowUp ADS-B service — dump1090 → MQTT",
    )
    parser.add_argument("--config", default=None, help="Config JSON path")
    parser.add_argument(
        "--hub-broker", default=DEFAULT_HUB_BROKER,
        help=(
            "Hub broker.  Default from GLB_HUB_BROKER env var (set "
            "via EnvironmentFile=-/etc/default/glowup-adsb in the "
            "systemd unit).  No hardcoded fallback — required."
        ),
    )
    parser.add_argument(
        "--hub-port", type=int, default=DEFAULT_MQTT_PORT,
        help=f"Hub port (default: {DEFAULT_MQTT_PORT})",
    )
    parser.add_argument(
        "--dump1090-url", default=DEFAULT_DUMP1090_URL,
        help=f"dump1090 JSON URL (default: {DEFAULT_DUMP1090_URL})",
    )
    parser.add_argument(
        "--device", "-d", type=int, default=0, dest="device_index",
        help="RTL-SDR device index (default: 0)",
    )
    parser.add_argument(
        "--gain", "-g", default=None,
        help="RTL-SDR gain (default: auto)",
    )
    parser.add_argument(
        "--no-launch", action="store_true",
        help="Don't spawn dump1090 (assume it's already running)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Debug logging",
    )
    args = parser.parse_args()

    # Fail fast — no broker means we can't run.
    if not args.hub_broker:
        parser.error(
            "no hub broker configured: set GLB_HUB_BROKER (typically "
            "via /etc/default/glowup-adsb) or pass --hub-broker"
        )

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    def _shutdown(sig, frame):
        logger.info("Shutting down (signal %d)", sig)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run_daemon(
        config_path=args.config,
        hub_broker=args.hub_broker,
        hub_port=args.hub_port,
        dump1090_url=args.dump1090_url,
        device_index=args.device_index,
        gain=args.gain,
        launch=not args.no_launch,
    )


if __name__ == "__main__":
    main()
