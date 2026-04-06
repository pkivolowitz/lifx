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

__version__ = "1.3"

import asyncio
import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any, Optional

from adapters.adapter_base import AsyncPollingAdapterBase
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

# Refresh token file path — written by vivint_setup.py, read by this adapter.
# Token file — check home directory first (Daedalus/macOS), fall back to
# /etc/glowup/ (Pi/systemd).  Adapter writes to whichever path is writable.
_HOME_TOKEN: Path = Path.home() / ".vivint_token"
_ETC_TOKEN: Path = Path("/etc/glowup/.vivint_token")
TOKEN_FILE: Path = _HOME_TOKEN if _HOME_TOKEN.exists() else (
    _ETC_TOKEN if _ETC_TOKEN.exists() else _HOME_TOKEN
)

# File permissions: owner read/write only.
TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR

# Proactive token refresh interval (seconds).  Vivint refresh tokens have
# a ~5-6 hour lifetime.  Refreshing every 90 minutes keeps them alive with
# comfortable margin.
TOKEN_REFRESH_INTERVAL: float = 5400.0

# ---------------------------------------------------------------------------
# Optional dependency check
# ---------------------------------------------------------------------------

try:
    import vivintpy  # noqa: F401
    from vivintpy.account import Account
    from vivintpy.devices.door_lock import DoorLock
    from vivintpy.devices.wireless_sensor import WirelessSensor
    from vivintpy.entity import UPDATE as VIVINT_UPDATE_EVENT
    _HAS_VIVINTPY: bool = True
except ImportError:
    _HAS_VIVINTPY = False
    Account = None  # type: ignore[assignment,misc]
    DoorLock = None  # type: ignore[assignment,misc]
    WirelessSensor = None  # type: ignore[assignment,misc]
    VIVINT_UPDATE_EVENT = "update"  # type: ignore[assignment]

# Alarm panel armed states — raw integer values from Vivint API.
ALARM_STATE_DISARMED: int = 0
ALARM_STATE_ARMED_STAY: int = 3
ALARM_STATE_ARMED_AWAY: int = 4


# ---------------------------------------------------------------------------
# VivintAdapter
# ---------------------------------------------------------------------------

class VivintAdapter(AsyncPollingAdapterBase):
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
        super().__init__(
            thread_name="vivint-adapter",
            reconnect_delay=RECONNECT_DELAY,
            max_reconnect_delay=MAX_RECONNECT_DELAY,
        )
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

        self._account: Any = None

        # Last known states — preserved across reconnects and stale periods.
        self._last_lock_state: dict[str, float] = {}
        self._last_battery: dict[str, float] = {}

        # Alarm panel state.
        self._alarm_state: Optional[str] = None  # "disarmed", "armed_stay", "armed_away"

        # Wireless sensor states: name → {is_on, battery, sensor_type, ...}.
        self._sensor_states: dict[str, dict[str, Any]] = {}

    def _check_prerequisites(self) -> bool:
        """Check vivintpy, credentials, and lock config."""
        if not _HAS_VIVINTPY:
            logger.warning(
                "vivintpy not installed — Vivint adapter disabled. "
                "Install with: pip install vivintpy"
            )
            return False

        if not self._username or not self._password:
            logger.error(
                "Vivint adapter requires username and password in config"
            )
            return False

        if not self._lock_names:
            logger.warning("No locks configured in vivint section")
            return False

        return True

    # --- AsyncPollingAdapterBase interface ----------------------------------

    async def _connect(self) -> None:
        """Authenticate and connect to Vivint cloud.

        Tries the saved refresh token first (no 2FA needed).  Falls back to
        username/password if the token is missing or expired.  If 2FA is
        required during fallback, logs an error — run vivint_setup.py
        interactively to generate a fresh token.
        """
        refresh_token: Optional[str] = self._load_token()

        if refresh_token:
            logger.info("Connecting to Vivint cloud with saved refresh token...")
            self._account = Account(
                username=self._username,
                password=self._password,
                refresh_token=refresh_token,
            )
        else:
            logger.info("Connecting to Vivint cloud with username/password...")
            self._account = Account(
                username=self._username,
                password=self._password,
            )

        try:
            await self._account.connect(
                load_devices=True,
                subscribe_for_realtime_updates=True,
            )
        except Exception as exc:
            exc_name: str = type(exc).__name__
            if "MfaRequired" in exc_name:
                logger.error(
                    "Vivint requires 2FA — run 'python3 vivint_setup.py' "
                    "on the Pi interactively to generate a refresh token"
                )
                raise
            raise

        logger.info("Connected to Vivint cloud")
        # Persist the (possibly refreshed) token for next startup.
        self._save_token()
        # Register PubNub real-time callbacks on each lock device so state
        # changes publish to MQTT immediately, not just on the 60s poll.
        self._register_pubnub_callbacks()
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

    async def _run_cycle(self) -> None:
        """Periodically read lock state and refresh the auth token.

        Lock state is polled every ``_poll_interval`` seconds.  The refresh
        token is re-saved every ``TOKEN_REFRESH_INTERVAL`` seconds to keep
        it alive across restarts.
        """
        last_token_refresh: float = time.time()

        while self._running:
            await asyncio.sleep(self._poll_interval)
            if not self._running:
                break
            try:
                await self._read_locks()
            except Exception as exc:
                logger.warning("Vivint poll error: %s", exc)

            # Proactively refresh and persist the token.
            now: float = time.time()
            if now - last_token_refresh >= TOKEN_REFRESH_INTERVAL:
                self._save_token()
                last_token_refresh = now
                logger.debug("Vivint refresh token persisted")

    # --- PubNub real-time callbacks ------------------------------------------

    def _register_pubnub_callbacks(self) -> None:
        """Register update callbacks on alarm panel, locks, and sensors.

        vivintpy's Entity base class emits an ``"update"`` event whenever
        PubNub pushes new state.  We hook into that so state changes
        publish to MQTT immediately instead of waiting for the 60s poll.
        """
        if not self._account:
            return

        registered: int = 0
        for system in self._account.systems:
            for alarm_panel in system.alarm_panels:
                # Alarm panel itself.
                alarm_panel.on(
                    VIVINT_UPDATE_EVENT,
                    self._make_panel_handler(alarm_panel),
                )
                registered += 1
                for device in alarm_panel.devices:
                    if isinstance(device, DoorLock):
                        device.on(VIVINT_UPDATE_EVENT, self._make_pubnub_handler(device))
                        registered += 1
                    elif WirelessSensor and isinstance(device, WirelessSensor):
                        device.on(VIVINT_UPDATE_EVENT, self._make_sensor_handler(device))
                        registered += 1

        if registered:
            logger.info(
                "Registered PubNub real-time callbacks on %d lock(s)", registered,
            )

    def _make_pubnub_handler(self, device: Any) -> Any:
        """Create a PubNub update handler bound to a specific device.

        Args:
            device: The DoorLock device instance.

        Returns:
            A callback that re-processes the lock on every PubNub update.
        """
        def _on_pubnub_update(data: dict) -> None:
            """Handle a PubNub real-time update for a lock device."""
            try:
                self._process_lock(device)
                logger.debug(
                    "PubNub real-time update for %s", device.name,
                )
            except Exception as exc:
                logger.warning(
                    "PubNub callback error for %s: %s", device.name, exc,
                )
        return _on_pubnub_update

    def _make_panel_handler(self, panel: Any) -> Any:
        """Create a PubNub handler for alarm panel state changes."""
        def _on_panel_update(data: dict) -> None:
            try:
                self._process_alarm_panel(panel)
                logger.debug("PubNub alarm panel update")
            except Exception as exc:
                logger.warning("PubNub alarm panel error: %s", exc)
        return _on_panel_update

    def _make_sensor_handler(self, device: Any) -> Any:
        """Create a PubNub handler for a wireless sensor."""
        def _on_sensor_update(data: dict) -> None:
            try:
                self._process_sensor(device)
                logger.debug("PubNub sensor update for %s", device.name)
            except Exception as exc:
                logger.warning("PubNub sensor error for %s: %s", device.name, exc)
        return _on_sensor_update

    # --- Token persistence --------------------------------------------------

    def _load_token(self) -> Optional[str]:
        """Load saved refresh token from disk.

        Checks multiple locations: home directory (Daedalus/macOS)
        and /etc/glowup/ (Pi/systemd).

        Returns:
            The refresh token string, or ``None`` if not found.
        """
        candidates: list[Path] = [
            Path.home() / ".vivint_token",
            Path("/etc/glowup/.vivint_token"),
        ]
        for path in candidates:
            if path.exists():
                try:
                    token: str = path.read_text().strip()
                    if token:
                        logger.info("Loaded Vivint refresh token from %s", path)
                        return token
                except Exception as exc:
                    logger.warning("Failed to read %s: %s", path, exc)

        logger.debug("No saved Vivint token found")
        return None

    def _save_token(self) -> None:
        """Extract and persist the current refresh token to disk.

        Writes to ``TOKEN_FILE`` with mode 0600.  Called after successful
        connection and periodically during the poll loop to keep the
        token fresh for next startup.
        """
        if not self._account:
            return

        # After auth, api.tokens is a dict containing OAuth tokens including
        # "refresh_token".  Note: api.refresh_token is a method (for refreshing),
        # not a getter — use api.tokens dict instead.
        refresh_token: str = ""
        if hasattr(self._account, "api") and hasattr(self._account.api, "tokens"):
            tokens: Any = self._account.api.tokens
            if isinstance(tokens, dict):
                refresh_token = tokens.get("refresh_token", "")

        if not refresh_token:
            logger.debug("No refresh token available to persist")
            return

        try:
            TOKEN_FILE.write_text(refresh_token)
            os.chmod(TOKEN_FILE, TOKEN_FILE_MODE)
        except Exception as exc:
            logger.warning("Failed to save Vivint token: %s", exc)

    async def _read_locks(self) -> None:
        """Read all devices: alarm panel, locks, and wireless sensors."""
        if not self._account:
            return

        for system in self._account.systems:
            for alarm_panel in system.alarm_panels:
                self._process_alarm_panel(alarm_panel)
                for device in alarm_panel.devices:
                    if isinstance(device, DoorLock):
                        self._process_lock(device)
                    elif WirelessSensor and isinstance(device, WirelessSensor):
                        self._process_sensor(device)

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

    def _process_alarm_panel(self, panel: Any) -> None:
        """Process alarm panel state — write signal and MQTT.

        Args:
            panel: A vivintpy AlarmPanel instance.
        """
        if panel.is_armed_away:
            state_str: str = "armed_away"
        elif panel.is_armed_stay:
            state_str = "armed_stay"
        else:
            state_str = "disarmed"

        prev: Optional[str] = self._alarm_state
        self._alarm_state = state_str

        signal_name: str = "alarm:state"
        state_value: float = float(panel.state) if panel.state is not None else 0.0

        if hasattr(self._bus, 'register'):
            self._bus.register(signal_name, SignalMeta(
                signal_type="scalar",
                description="Vivint alarm panel state",
                source_name="alarm",
                transport=TRANSPORT,
            ))
        self._bus.write(signal_name, state_value)

        if self._mqtt_client:
            try:
                self._mqtt_client.publish(
                    f"{self._topic_prefix}/alarm/state",
                    state_str, qos=MQTT_QOS,
                )
            except Exception as exc:
                logger.debug("MQTT publish error (alarm): %s", exc)

        if prev != state_str:
            logger.info("Alarm panel: %s", state_str)

    def _process_sensor(self, device: Any) -> None:
        """Process a wireless sensor — write signals and MQTT.

        Publishes is_on (open/triggered), battery, and sensor_type for
        each wireless sensor (door contacts, glass break, motion, smoke/CO).

        Args:
            device: A vivintpy WirelessSensor instance.
        """
        name: str = device.name
        # Normalize to a config-friendly key.
        key: str = name.lower().replace(" ", "_").replace("&", "and")

        is_on: bool = device.is_on if hasattr(device, 'is_on') else False
        on_value: float = 1.0 if is_on else 0.0
        battery: Optional[int] = device.battery_level if hasattr(device, 'battery_level') else None
        sensor_type: str = ""
        if hasattr(device, 'sensor_type') and device.sensor_type is not None:
            st: Any = device.sensor_type
            # May be an enum (SensorType.EXIT_ENTRY_1) or a raw int.
            if hasattr(st, 'name'):
                sensor_type = st.name.lower()
            else:
                sensor_type = str(st)

        # Track state.
        prev: dict[str, Any] = self._sensor_states.get(key, {})
        self._sensor_states[key] = {
            "name": name,
            "is_on": is_on,
            "battery": battery,
            "sensor_type": sensor_type,
        }

        # Signal: {key}:state (1.0 = open/triggered, 0.0 = closed/clear).
        signal_name: str = f"{key}:state"
        if hasattr(self._bus, 'register'):
            self._bus.register(signal_name, SignalMeta(
                signal_type="scalar",
                description=f"Vivint {name} state",
                source_name=key,
                transport=TRANSPORT,
            ))
        self._bus.write(signal_name, on_value)

        # MQTT publish.
        if self._mqtt_client:
            try:
                self._mqtt_client.publish(
                    f"{self._topic_prefix}/sensor/{key}/state",
                    str(int(on_value)), qos=MQTT_QOS,
                )
                if battery is not None:
                    self._mqtt_client.publish(
                        f"{self._topic_prefix}/sensor/{key}/battery",
                        str(battery), qos=MQTT_QOS,
                    )
            except Exception as exc:
                logger.debug("MQTT publish error (sensor %s): %s", key, exc)

        if prev.get("is_on") != is_on:
            state_label: str = "OPEN/ON" if is_on else "closed/off"
            logger.info("Sensor %s (%s): %s", key, name, state_label)

    # --- Introspection -----------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return adapter status for API responses.

        Returns:
            Dict with connection state and last-known values.
        """
        return {
            "connected": self._account is not None,
            "lock_count": len(self._lock_names),
            "poll_interval_seconds": self._poll_interval,
            "alarm_state": self._alarm_state,
            "locks": {
                name: {
                    "lock_state": self._last_lock_state.get(name),
                    "battery": self._last_battery.get(name),
                }
                for name in self._lock_names
            },
            "sensors": self._sensor_states,
        }

    # --- Hooks -------------------------------------------------------------

    def _on_started(self) -> None:
        """Log Vivint-specific start message."""
        logger.info(
            "Vivint adapter started — %d lock(s) configured",
            len(self._lock_names),
        )

    def _on_stopped(self) -> None:
        """Log Vivint-specific stop message."""
        logger.info("Vivint adapter stopped")
