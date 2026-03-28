"""Vivint cloud adapter — reads lock state from Vivint's cloud API.

KwikSet door locks remain paired to the Vivint security system.  A Zigbee
device can only pair to one coordinator — removing locks from Vivint would
break the household security system.  This adapter reads lock state via
Vivint's cloud API and writes signals to the :class:`~media.SignalBus`.

The adapter is **read-only** — GlowUp does not send lock/unlock commands.
Locks are controlled via the Vivint app or physical key.

Requires the ``vivintpy`` library (optional dependency)::

    pip install vivintpy

If ``vivintpy`` is not installed, the adapter logs a warning and does not
start.

Signal output (per configured lock)::

    vivint:{lock_name}:lock_state  — 1.0 (locked) / 0.0 (unlocked)
    vivint:{lock_name}:battery     — 0.0 to 1.0 (normalized from 0-100%)

MQTT output (for remote subscribers)::

    glowup/vivint/{lock_name}/lock_state  — "1" / "0"
    glowup/vivint/{lock_name}/battery     — "0.88" (normalized)

Cloud dependency: if internet is down, lock state goes stale.  The adapter
preserves last-known values — it does NOT flip signals to unknown on timeout.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from media import SignalMeta

logger: logging.Logger = logging.getLogger("glowup.vivint")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix for Vivint signals.
MQTT_TOPIC_PREFIX: str = "glowup/vivint"

# Transport identifier for metadata registration.
TRANSPORT: str = "vivint"

# MQTT QoS for lock state messages (at-least-once).
MQTT_QOS: int = 1

# Default poll interval (seconds).  Vivint's PubNub subscription provides
# real-time updates, but we also poll periodically as a fallback in case
# the subscription silently drops (known issue ~15-20 min timeout).
DEFAULT_POLL_INTERVAL: float = 60.0

# Minimum poll interval to avoid hammering the API.
MIN_POLL_INTERVAL: float = 10.0

# Battery normalization divisor (Vivint reports 0-100 integer).
BATTERY_SCALE: float = 100.0

# Reconnect delay after auth failure (seconds).
RECONNECT_DELAY: float = 300.0

# Maximum reconnect delay (seconds).
MAX_RECONNECT_DELAY: float = 3600.0

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------

try:
    import vivintpy  # noqa: F401
    from vivintpy.account import Account
    from vivintpy.devices.door_lock import DoorLock
    _HAS_VIVINTPY: bool = True
except ImportError:
    _HAS_VIVINTPY = False
    Account = None  # type: ignore[assignment,misc]
    DoorLock = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# VivintAdapter
# ---------------------------------------------------------------------------

class VivintAdapter:
    """Reads lock state from Vivint cloud and writes to SignalBus + MQTT.

    Args:
        config:     The ``"vivint"`` section of server.json.
        bus:        The shared :class:`~media.SignalBus`.
        mqtt_client: Optional paho MQTT client for publishing to MQTT.
    """

    def __init__(
        self,
        config: dict[str, Any],
        bus: Any,
        mqtt_client: Any = None,
    ) -> None:
        """Initialize the Vivint adapter.

        Args:
            config:      Vivint config section from server.json.
            bus:         SignalBus instance for signal writes.
            mqtt_client: Optional paho MQTT client for MQTT publishing.
        """
        self._config: dict[str, Any] = config
        self._bus: Any = bus
        self._mqtt_client: Any = mqtt_client
        self._username: str = config.get("username", "")
        self._password: str = config.get("password", "")
        self._poll_interval: float = max(
            float(config.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)),
            MIN_POLL_INTERVAL,
        )
        # Configurable MQTT topic prefix.
        self._topic_prefix: str = config.get(
            "mqtt_topic_prefix", MQTT_TOPIC_PREFIX,
        )
        # Map of vivint device name → config name for signal naming.
        # Config format: {"locks": {"front_door_lock": "Front Door", ...}}
        self._lock_names: dict[str, str] = config.get("locks", {})

        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._account: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Last known states — preserved across reconnects and stale periods.
        self._last_lock_state: dict[str, float] = {}
        self._last_battery: dict[str, float] = {}

    def start(self) -> None:
        """Start the Vivint adapter in a background thread."""
        if not _HAS_VIVINTPY:
            logger.warning(
                "vivintpy not installed — Vivint adapter disabled. "
                "Install with: pip install vivintpy"
            )
            return

        if not self._username or not self._password:
            logger.error(
                "Vivint adapter requires username and password in config"
            )
            return

        if not self._lock_names:
            logger.warning("No locks configured in vivint section")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="vivint-adapter",
        )
        self._thread.start()
        logger.info(
            "Vivint adapter started — %d lock(s) configured",
            len(self._lock_names),
        )

    def stop(self) -> None:
        """Stop the adapter and disconnect from Vivint."""
        self._running = False
        if self._loop:
            # Schedule disconnect on the event loop.
            try:
                asyncio.run_coroutine_threadsafe(
                    self._disconnect(), self._loop,
                )
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10.0)
        logger.info("Vivint adapter stopped")

    # --- Internal event loop -----------------------------------------------

    def _run_loop(self) -> None:
        """Background thread: runs asyncio event loop for vivintpy."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as exc:
            logger.error("Vivint adapter loop crashed: %s", exc)
        finally:
            self._loop.close()

    async def _main_loop(self) -> None:
        """Async main loop: connect, subscribe, poll."""
        reconnect_delay: float = RECONNECT_DELAY

        while self._running:
            try:
                await self._connect()
                reconnect_delay = RECONNECT_DELAY  # Reset on success.
                await self._poll_loop()
            except Exception as exc:
                logger.error(
                    "Vivint connection error: %s — retrying in %.0fs",
                    exc, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                # Exponential backoff, capped.
                reconnect_delay = min(
                    reconnect_delay * 2.0, MAX_RECONNECT_DELAY,
                )

    async def _connect(self) -> None:
        """Authenticate and connect to Vivint cloud."""
        logger.info("Connecting to Vivint cloud...")
        self._account = Account(
            username=self._username,
            password=self._password,
        )
        await self._account.connect(
            load_devices=True,
            subscribe_for_realtime_updates=True,
        )
        logger.info("Connected to Vivint cloud")
        # Do an immediate read after connecting.
        await self._read_locks()

    async def _disconnect(self) -> None:
        """Disconnect from Vivint cloud."""
        if self._account:
            try:
                await self._account.disconnect()
            except Exception:
                pass
            self._account = None

    async def _poll_loop(self) -> None:
        """Periodically read lock state as fallback to PubNub updates."""
        while self._running:
            await asyncio.sleep(self._poll_interval)
            if not self._running:
                break
            try:
                await self._read_locks()
            except Exception as exc:
                logger.warning("Vivint poll error: %s", exc)

    async def _read_locks(self) -> None:
        """Read all lock devices and update signals."""
        if not self._account:
            return

        for system in self._account.systems:
            for alarm_panel in system.alarm_panels:
                for device in alarm_panel.devices:
                    if not isinstance(device, DoorLock):
                        continue
                    self._process_lock(device)

    def _process_lock(self, device: Any) -> None:
        """Process a single lock device — write signals and MQTT.

        Args:
            device: A vivintpy DoorLock device instance.
        """
        vivint_name: str = device.name
        # Find the config name for this lock.
        config_name: Optional[str] = None
        for cfg_name, display_name in self._lock_names.items():
            if display_name == vivint_name or cfg_name == vivint_name:
                config_name = cfg_name
                break

        if config_name is None:
            # Lock not in our config — skip silently.
            return

        # --- Lock state ---
        locked: bool = device.is_locked
        lock_value: float = 1.0 if locked else 0.0
        signal_name: str = f"{config_name}:lock_state"

        prev: Optional[float] = self._last_lock_state.get(config_name)
        self._last_lock_state[config_name] = lock_value

        # Register with transport metadata and write to SignalBus.
        if hasattr(self._bus, 'register'):
            self._bus.register(signal_name, SignalMeta(
                signal_type="scalar",
                description=f"Vivint {config_name} lock state",
                source_name=config_name,
                transport=TRANSPORT,
            ))
        self._bus.write(signal_name, lock_value)

        # Publish to MQTT.
        if self._mqtt_client:
            try:
                topic: str = f"{self._topic_prefix}/{config_name}/lock_state"
                self._mqtt_client.publish(
                    topic, str(int(lock_value)), qos=MQTT_QOS,
                )
            except Exception as exc:
                logger.debug("MQTT publish error (lock_state): %s", exc)

        if prev is None or prev != lock_value:
            state_str: str = "locked" if locked else "unlocked"
            logger.info(
                "Lock %s (%s): %s",
                config_name, vivint_name, state_str,
            )

        # --- Battery ---
        battery_raw: Optional[int] = device.battery_level
        if battery_raw is not None:
            battery_norm: float = float(battery_raw) / BATTERY_SCALE
            battery_signal: str = f"{config_name}:battery"
            self._last_battery[config_name] = battery_norm
            if hasattr(self._bus, 'register'):
                self._bus.register(battery_signal, SignalMeta(
                    signal_type="scalar",
                    description=f"Vivint {config_name} battery",
                    source_name=config_name,
                    transport=TRANSPORT,
                ))
            self._bus.write(battery_signal, battery_norm)

            if self._mqtt_client:
                try:
                    btopic: str = f"{self._topic_prefix}/{config_name}/battery"
                    self._mqtt_client.publish(
                        btopic, f"{battery_norm:.2f}", qos=MQTT_QOS,
                    )
                except Exception as exc:
                    logger.debug("MQTT publish error (battery): %s", exc)

    # --- Introspection -----------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return adapter status for API responses.

        Returns:
            Dict with connection state and last-known lock values.
        """
        return {
            "connected": self._account is not None,
            "lock_count": len(self._lock_names),
            "poll_interval_seconds": self._poll_interval,
            "locks": {
                name: {
                    "lock_state": self._last_lock_state.get(name),
                    "battery": self._last_battery.get(name),
                }
                for name in self._lock_names
            },
        }
