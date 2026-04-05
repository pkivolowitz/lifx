"""Standalone keepalive process — ARP discovery, UDP ping, and power query.

Runs :class:`BulbKeepAlive` in its own process, isolated from the
GlowUp server's GIL.  Publishes discoveries, power state updates,
and the IP-to-MAC map via MQTT so the server receives device state
without doing any network I/O itself.

MQTT topics published:
    - ``glowup/adapter/keepalive/discovered`` — full IP-to-MAC map
      (retained, QoS 1).  Updated every power query cycle and on
      new bulb discovery.
    - ``glowup/adapter/keepalive/event/new_bulb`` — per-discovery
      event (QoS 1) with ``{"ip": "...", "mac": "..."}``.
    - ``glowup/device_state/{ip}/power`` — per-device power state
      (retained, QoS 1) with ``{"power": true/false, "ts": ...}``.

Usage::

    python -m infrastructure.keepalive_process --config /etc/glowup/server.json
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import concurrent.futures
import json
import logging
import sys
import threading
import time
from typing import Any, Optional

from adapters.process_base import ProcessAdapterBase
from infrastructure.bulb_keepalive import BulbKeepAlive
from transport import LifxDevice

logger: logging.Logger = logging.getLogger("glowup.keepalive_process")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Adapter identifier — must match AdapterProxy in server.py.
ADAPTER_ID: str = "keepalive"

# Default MQTT broker for GlowUp lifecycle.
DEFAULT_BROKER: str = "localhost"

# Default MQTT broker port.
DEFAULT_PORT: int = 1883

# Log format for standalone process.
LOG_FORMAT: str = "%(asctime)s %(name)s %(levelname)s %(message)s"

# MQTT topics.
TOPIC_DISCOVERED: str = "glowup/adapter/keepalive/discovered"
TOPIC_NEW_BULB: str = "glowup/adapter/keepalive/event/new_bulb"
TOPIC_POWER_PREFIX: str = "glowup/device_state"

# QoS levels.
QOS_DISCOVERED: int = 1
QOS_NEW_BULB: int = 1
QOS_POWER: int = 1

# Initial scan timeout (seconds).
INITIAL_SCAN_TIMEOUT: float = 30.0

# Maximum concurrent power queries.  Limits file descriptor usage
# and network burst.  Sufficient for fleets up to ~50 devices.
# For larger fleets, replace ThreadPoolExecutor with a layered
# approach: (1) passive ARP to identify reachable devices,
# (2) batch UDP query (single socket, send all, poll responses)
# targeting only ARP-present devices, (3) targeted unicast for
# known-but-missing devices.  See plan notes.
POWER_QUERY_WORKERS: int = 32


# ---------------------------------------------------------------------------
# KeepaliveProcess
# ---------------------------------------------------------------------------

class KeepaliveProcess(ProcessAdapterBase):
    """Keepalive daemon running as a standalone process.

    Wraps :class:`BulbKeepAlive` and adds MQTT publishing for
    device discovery and power state updates.  The server subscribes
    to these topics instead of querying devices directly.

    Args:
        config:  Full server.json config dict.
        broker:  GlowUp MQTT broker address.
        port:    GlowUp MQTT broker port.
    """

    def __init__(
        self,
        config: dict[str, Any],
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_PORT,
    ) -> None:
        """Initialize the keepalive process."""
        super().__init__(ADAPTER_ID, broker, port)
        self._config: dict[str, Any] = config
        self._keepalive: Optional[BulbKeepAlive] = None

    # ------------------------------------------------------------------
    # ProcessAdapterBase overrides
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start BulbKeepAlive, publish discoveries, block until stopped."""
        lifx_cfg: dict[str, Any] = self._config.get("lifx", {})

        self._keepalive = BulbKeepAlive(
            on_new_bulb=self._on_new_bulb,
            on_power_query=self._on_power_query,
            bind_ip=lifx_cfg.get("bind_ip"),
            sweep_network=lifx_cfg.get("sweep_network"),
        )
        self._keepalive.start()

        logger.info(
            "[keepalive] BulbKeepAlive started, waiting for initial scan",
        )

        # Wait for the first ARP scan so we have a populated device map.
        if self._keepalive.wait_initial_scan(timeout=INITIAL_SCAN_TIMEOUT):
            logger.info(
                "[keepalive] Initial scan complete — %d bulb(s)",
                len(self._keepalive.known_bulbs),
            )
        else:
            logger.warning(
                "[keepalive] Initial scan timed out — some devices may "
                "be missing from the first published map",
            )

        # Publish initial map.
        self._publish_discovered_map()

        # Block until SIGTERM or stop().
        self._stop_event.wait()

        self._keepalive.stop()
        logger.info("[keepalive] Process stopped")

    def get_status_detail(self) -> dict[str, Any]:
        """Return keepalive health for heartbeat.

        The server checks ``initial_scan_done`` in the heartbeat detail
        to know when the IP-to-MAC map is available.

        Returns:
            Status dict with bulb count and scan state.
        """
        if self._keepalive is None:
            return {"running": False, "known_bulbs": 0}
        known: dict[str, str] = self._keepalive.known_bulbs
        return {
            "running": True,
            "known_bulbs": len(known),
            "initial_scan_done": self._keepalive._initial_scan_done.is_set(),
        }

    # ------------------------------------------------------------------
    # BulbKeepAlive callbacks
    # ------------------------------------------------------------------

    def _on_new_bulb(self, ip: str, mac: str) -> None:
        """Handle new bulb discovery — publish event and update map.

        Called from the BulbKeepAlive thread.

        Args:
            ip:  IP address of the discovered bulb.
            mac: MAC address (lowercase, colon-separated).
        """
        event: dict[str, str] = {"ip": ip, "mac": mac}
        self._client.publish(
            TOPIC_NEW_BULB,
            json.dumps(event),
            qos=QOS_NEW_BULB,
        )
        logger.info("[keepalive] New bulb: %s (%s)", ip, mac)

        # Update the retained discovery map.
        self._publish_discovered_map()

    def _on_power_query(self) -> None:
        """Periodic power state query — probe all known bulbs concurrently.

        Called from the BulbKeepAlive thread every Nth ARP cycle
        (default: every 2nd cycle = ~10 seconds).

        Uses a :class:`ThreadPoolExecutor` to query bulbs in parallel.
        Each worker creates a temporary :class:`LifxDevice`, queries
        its power state, and closes the socket.  This avoids holding
        persistent sockets (important at scale: 200+ devices would
        exhaust file descriptors with cached sockets).
        """
        if self._keepalive is None:
            return

        known: dict[str, str] = self._keepalive.known_bulbs
        if not known:
            return

        now: float = time.time()
        results: dict[str, Optional[bool]] = {}

        # Concurrent power queries — POWER_QUERY_WORKERS limits both
        # file descriptors and network burst.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=POWER_QUERY_WORKERS,
            thread_name_prefix="power-probe",
        ) as executor:
            futures: dict[concurrent.futures.Future, str] = {
                executor.submit(self._query_power, ip): ip
                for ip in known
            }
            for future in concurrent.futures.as_completed(futures):
                ip: str = futures[future]
                try:
                    results[ip] = future.result()
                except Exception as exc:
                    logger.debug(
                        "[keepalive] Power probe %s raised: %s",
                        ip, exc,
                    )
                    results[ip] = None

        # Publish results to MQTT.
        for ip, power in results.items():
            if power is not None:
                topic: str = f"{TOPIC_POWER_PREFIX}/{ip}/power"
                payload: dict[str, Any] = {
                    "power": power,
                    "ts": now,
                }
                self._client.publish(
                    topic,
                    json.dumps(payload),
                    qos=QOS_POWER,
                    retain=True,
                )

        # Update the retained discovery map (bulbs may have expired).
        self._publish_discovered_map()

    # ------------------------------------------------------------------
    # Power query helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _query_power(ip: str) -> Optional[bool]:
        """Query a single bulb's power state via temporary UDP socket.

        Creates a :class:`LifxDevice`, queries, closes.  No persistent
        socket is kept — scales to thousands of devices without
        exhausting file descriptors.

        Args:
            ip: Bulb IP address.

        Returns:
            ``True`` (on), ``False`` (off), or ``None`` on failure.
        """
        dev: LifxDevice = LifxDevice(ip)
        try:
            state: Optional[tuple[int, int, int, int, int]] = (
                dev.query_light_state()
            )
            if state is not None:
                # state = (hue, sat, brightness, kelvin, power)
                # power > 0 means the light is on.
                return state[4] > 0
        except Exception as exc:
            logger.debug(
                "[keepalive] Power query failed for %s: %s", ip, exc,
            )
        finally:
            dev.close()
        return None

    # ------------------------------------------------------------------
    # MQTT publishing
    # ------------------------------------------------------------------

    def _publish_discovered_map(self) -> None:
        """Publish the full IP-to-MAC map to the retained discovery topic.

        The server subscribes to this topic on startup and uses the
        retained message for initial device resolution.
        """
        if self._keepalive is None:
            return
        known: dict[str, str] = self._keepalive.known_bulbs
        self._client.publish(
            TOPIC_DISCOVERED,
            json.dumps(known),
            qos=QOS_DISCOVERED,
            retain=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args, load config, and start the keepalive process."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp Keepalive Daemon — standalone process",
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to server.json",
    )
    parser.add_argument(
        "--broker", default=None,
        help="GlowUp MQTT broker (overrides config)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="GlowUp MQTT port (overrides config)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    args: argparse.Namespace = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=LOG_FORMAT,
    )

    # Load config.
    try:
        with open(args.config) as f:
            config: dict[str, Any] = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load config %s: %s", args.config, exc)
        sys.exit(1)

    # Resolve GlowUp MQTT broker from args or config.
    mqtt_cfg: dict[str, Any] = config.get("mqtt", {})
    broker: str = args.broker or mqtt_cfg.get("broker", DEFAULT_BROKER)
    port: int = args.port or mqtt_cfg.get("port", DEFAULT_PORT)

    logger.info(
        "Starting keepalive process — GlowUp broker=%s:%d",
        broker, port,
    )

    process: KeepaliveProcess = KeepaliveProcess(config, broker, port)
    process.start()


if __name__ == "__main__":
    main()
