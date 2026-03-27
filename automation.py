"""Automation manager — sensor-driven light actions with CRUD support.

Subscribes to BLE sensor MQTT topics and triggers GlowUp effects on
device groups based on configurable rules.  Each automation ties a
sensor (BLE motion, temperature, humidity) to a light action (any
non-audio effect) with a configurable off-condition (watchdog timeout
or sensor value match).

Replaces the hardcoded ``ble_trigger.py`` with a general-purpose
automation system that supports full CRUD via REST API and dashboard
visibility.

Data model::

    {
        "name": "Living room motion",
        "enabled": true,
        "sensor": {"type": "ble", "label": "onvis_motion",
                   "characteristic": "motion"},
        "trigger": {"condition": "eq", "value": 1},
        "action": {"group": "living_room", "effect": "on",
                   "params": {"brightness": 70}},
        "off_trigger": {"type": "watchdog", "minutes": 30},
        "off_action": {"effect": "off", "params": {}},
        "schedule_conflict": "defer"
    }
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import operator
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.automation")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix matching ble/sensor.py.
MQTT_PREFIX: str = "glowup/ble"

# Minimum seconds between repeated trigger actions to avoid hammering
# bulbs while a sensor fires continuously.
DEBOUNCE_SECONDS: float = 2.0

# How often the watchdog thread checks for stale sensors (seconds).
WATCHDOG_CHECK_INTERVAL: float = 60.0

# Default watchdog timeout (minutes) when not specified.
DEFAULT_WATCHDOG_MINUTES: float = 30.0

# Seconds-per-minute conversion factor.
SECONDS_PER_MINUTE: float = 60.0

# Valid trigger condition operators.
_CONDITION_OPS: dict[str, Callable] = {
    "eq":  operator.eq,
    "gt":  operator.gt,
    "lt":  operator.lt,
    "gte": operator.ge,
    "lte": operator.le,
}

# Valid sensor characteristics (MQTT subtopics).
VALID_CHARACTERISTICS: frozenset[str] = frozenset({
    "motion", "temperature", "humidity",
})

# Valid schedule-conflict policies.
VALID_CONFLICT_POLICIES: frozenset[str] = frozenset({
    "defer", "override", "coexist",
})

# Group identifier prefix (matches server.py GROUP_PREFIX).
_GROUP_PREFIX: str = "group:"


# ---------------------------------------------------------------------------
# Sensor data store (carried over from ble_trigger.py)
# ---------------------------------------------------------------------------

class BleSensorData:
    """Thread-safe store for the latest BLE sensor readings.

    Available to REST endpoints for querying current values regardless
    of whether any automations are configured.
    """

    def __init__(self) -> None:
        """Initialize with empty data."""
        self._lock: threading.Lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}

    def update(self, label: str, key: str, value: Any) -> None:
        """Update a sensor value.

        Args:
            label: Sensor label (e.g., ``"onvis_motion"``).
            key:   Data key (e.g., ``"motion"``, ``"temperature"``).
            value: The new value.
        """
        with self._lock:
            if label not in self._data:
                self._data[label] = {}
            self._data[label][key] = value
            self._data[label]["last_update"] = time.time()

    def get(self, label: str) -> dict[str, Any]:
        """Get all values for a sensor.

        Args:
            label: Sensor label.

        Returns:
            A copy of the sensor's data dict, or empty if unknown.
        """
        with self._lock:
            return dict(self._data.get(label, {}))

    def get_all(self) -> dict[str, dict[str, Any]]:
        """Get all sensor data.

        Returns:
            A copy of ``{label: {key: value, ...}}``.
        """
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}


# Singleton sensor data store — imported by server REST handlers.
sensor_data: BleSensorData = BleSensorData()


# ---------------------------------------------------------------------------
# Per-automation runtime state
# ---------------------------------------------------------------------------

class _AutomationState:
    """Tracks runtime state for a single automation rule.

    Attributes:
        active:         Whether the action is currently running.
        last_trigger:   Timestamp of the last trigger event.
        last_action:    Timestamp of the last action execution.
        last_off:       Timestamp of the last off-action execution.
    """

    def __init__(self) -> None:
        """Initialize with idle state."""
        self.active: bool = False
        self.last_trigger: float = 0.0
        self.last_action: float = 0.0
        self.last_off: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _device_id_for_group(group_name: str) -> str:
    """Build a ``group:name`` device identifier.

    Args:
        group_name: The group name without prefix.

    Returns:
        The prefixed identifier (e.g., ``"group:living_room"``).
    """
    if group_name.startswith(_GROUP_PREFIX):
        return group_name
    return _GROUP_PREFIX + group_name


def _evaluate_condition(
    op_name: str,
    threshold: Any,
    value: Any,
) -> bool:
    """Evaluate a trigger condition.

    Args:
        op_name:   Operator name (``"eq"``, ``"gt"``, etc.).
        threshold: The threshold value from the automation config.
        value:     The sensor value to test.

    Returns:
        ``True`` if the condition is satisfied.
    """
    op_fn: Optional[Callable] = _CONDITION_OPS.get(op_name)
    if op_fn is None:
        logger.warning("Unknown condition operator: %s", op_name)
        return False
    try:
        return op_fn(value, threshold)
    except (TypeError, ValueError) as exc:
        logger.debug("Condition eval error: %s", exc)
        return False


def validate_automation(
    entry: dict[str, Any],
    known_groups: set[str],
    known_effects: set[str],
    media_effects: set[str],
) -> list[str]:
    """Validate an automation entry, returning a list of error strings.

    An empty list means the entry is valid.

    Args:
        entry:          The automation dict to validate.
        known_groups:   Set of valid group names.
        known_effects:  Set of registered effect names.
        media_effects:  Set of MediaEffect subclass names (not allowed).

    Returns:
        List of human-readable error strings (empty if valid).
    """
    errors: list[str] = []

    # Required top-level fields.
    if not entry.get("name"):
        errors.append("Missing 'name'")

    # Sensor validation.
    sensor: dict = entry.get("sensor", {})
    if not sensor.get("label"):
        errors.append("Missing sensor.label")
    if sensor.get("characteristic") not in VALID_CHARACTERISTICS:
        errors.append(
            f"Invalid sensor.characteristic: {sensor.get('characteristic')!r}"
        )

    # Trigger validation.
    trigger: dict = entry.get("trigger", {})
    if trigger.get("condition") not in _CONDITION_OPS:
        errors.append(
            f"Invalid trigger.condition: {trigger.get('condition')!r}"
        )
    if "value" not in trigger:
        errors.append("Missing trigger.value")

    # Action validation.
    action: dict = entry.get("action", {})
    group_name: str = action.get("group", "")
    if group_name and group_name not in known_groups:
        errors.append(f"Unknown group: {group_name!r}")
    if not group_name:
        errors.append("Missing action.group")

    effect: str = action.get("effect", "")
    if effect and effect not in known_effects:
        errors.append(f"Unknown effect: {effect!r}")
    elif effect in media_effects:
        errors.append(f"Audio/media effects not allowed: {effect!r}")
    if not effect:
        errors.append("Missing action.effect")

    # Off-trigger validation.
    off_trigger: dict = entry.get("off_trigger", {})
    off_type: str = off_trigger.get("type", "watchdog")
    if off_type == "watchdog":
        minutes = off_trigger.get("minutes", DEFAULT_WATCHDOG_MINUTES)
        if not isinstance(minutes, (int, float)) or minutes <= 0:
            errors.append(f"Invalid off_trigger.minutes: {minutes!r}")
    elif off_type == "condition":
        if off_trigger.get("condition") not in _CONDITION_OPS:
            errors.append(
                f"Invalid off_trigger.condition: "
                f"{off_trigger.get('condition')!r}"
            )
    else:
        errors.append(f"Invalid off_trigger.type: {off_type!r}")

    # Off-action validation (optional — defaults to stop/power-off).
    off_action: dict = entry.get("off_action", {})
    off_effect: str = off_action.get("effect", "off")
    if off_effect and off_effect not in known_effects:
        errors.append(f"Unknown off_action effect: {off_effect!r}")
    elif off_effect in media_effects:
        errors.append(
            f"Audio/media effects not allowed in off_action: {off_effect!r}"
        )

    # Schedule conflict policy.
    policy: str = entry.get("schedule_conflict", "defer")
    if policy not in VALID_CONFLICT_POLICIES:
        errors.append(f"Invalid schedule_conflict: {policy!r}")

    return errors


def migrate_ble_triggers(config: dict[str, Any]) -> bool:
    """Auto-migrate ``ble_triggers`` to ``automations`` format.

    If the config has ``ble_triggers`` but no ``automations``, converts
    each ble_trigger entry into an automation entry and saves.

    Args:
        config: The full server config dict (modified in place).

    Returns:
        ``True`` if migration was performed, ``False`` otherwise.
    """
    if "automations" in config:
        return False
    old: dict[str, Any] = config.get("ble_triggers", {})
    if not old:
        return False

    automations: list[dict[str, Any]] = []
    for label, cfg in old.items():
        group: str = cfg.get("group", "")
        # Strip "group:" prefix if present — the new format uses bare names.
        if group.startswith(_GROUP_PREFIX):
            group = group[len(_GROUP_PREFIX):]

        on_motion: dict = cfg.get("on_motion", {})
        brightness: int = on_motion.get("brightness", 70)
        watchdog: float = cfg.get(
            "watchdog_minutes", DEFAULT_WATCHDOG_MINUTES,
        )

        entry: dict[str, Any] = {
            "name": f"{label} (migrated)",
            "enabled": True,
            "sensor": {
                "type": "ble",
                "label": label,
                "characteristic": "motion",
            },
            "trigger": {"condition": "eq", "value": 1},
            "action": {
                "group": group,
                "effect": "on",
                "params": {"brightness": brightness},
            },
            "off_trigger": {
                "type": "watchdog",
                "minutes": watchdog,
            },
            "off_action": {"effect": "off", "params": {}},
            "schedule_conflict": "defer",
        }
        automations.append(entry)

    config["automations"] = automations
    logger.info(
        "Migrated %d ble_trigger(s) to automations format",
        len(automations),
    )
    return True


# ---------------------------------------------------------------------------
# AutomationManager
# ---------------------------------------------------------------------------

class AutomationManager:
    """Manages sensor-driven automations via MQTT.

    Subscribes to BLE sensor topics, evaluates trigger conditions,
    and drives light groups through the DeviceManager.  Runs as
    background threads alongside the HTTP server.

    Args:
        config:          The full server config dict (contains
                         ``"automations"``, ``"location"``, ``"schedule"``).
        device_manager:  The server's :class:`DeviceManager` instance.
        broker:          MQTT broker address.
        port:            MQTT broker port.
    """

    def __init__(
        self,
        config: dict[str, Any],
        device_manager: Any,
        broker: str = "10.0.0.48",
        port: int = 1883,
    ) -> None:
        """Initialize the automation manager.

        Args:
            config:         Full server config dict.
            device_manager: Shared DeviceManager.
            broker:         MQTT broker address.
            port:           MQTT broker port.
        """
        self._config: dict[str, Any] = config
        self._dm: Any = device_manager
        self._broker: str = broker
        self._port: int = port
        self._client: Any = None
        self._running: bool = False
        self._lock: threading.Lock = threading.Lock()

        # Per-automation runtime state, keyed by index.
        self._states: dict[int, _AutomationState] = {}

        self._watchdog_thread: Optional[threading.Thread] = None

    @property
    def automations(self) -> list[dict[str, Any]]:
        """Return the current automations list from config."""
        return self._config.get("automations", [])

    def start(self) -> None:
        """Start the MQTT subscriber and watchdog thread."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning(
                "Automations require paho-mqtt — automations disabled"
            )
            return

        if not self.automations:
            logger.info("No automations configured — manager idle")
            return

        self._running = True

        # Initialize per-automation state.
        for i in range(len(self.automations)):
            self._states[i] = _AutomationState()

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"glowup-automation-{int(time.time())}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()

        # Watchdog thread checks for stale sensors.
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="auto-watchdog",
        )
        self._watchdog_thread.start()

        logger.info(
            "Automation manager started — %d automation(s)",
            len(self.automations),
        )

    def stop(self) -> None:
        """Stop the subscriber and watchdog."""
        self._running = False
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        logger.info("Automation manager stopped")

    def reload(self) -> None:
        """Reload automations after a CRUD operation.

        Re-subscribes to MQTT topics for any new sensors and
        initializes state for new automations.
        """
        with self._lock:
            # Add state for new automations.
            for i in range(len(self.automations)):
                if i not in self._states:
                    self._states[i] = _AutomationState()
            # Remove state for deleted automations.
            valid_indices: set[int] = set(range(len(self.automations)))
            stale: list[int] = [
                k for k in self._states if k not in valid_indices
            ]
            for k in stale:
                del self._states[k]

        # Re-subscribe to topics.
        if self._client and self._client.is_connected():
            self._subscribe_all(self._client)

        logger.info("Automation manager reloaded — %d automation(s)",
                     len(self.automations))

    def get_status(self) -> list[dict[str, Any]]:
        """Return status for all automations.

        Returns:
            List of dicts with ``index``, ``name``, ``active``,
            ``last_triggered`` for each automation.
        """
        result: list[dict[str, Any]] = []
        with self._lock:
            for i, auto in enumerate(self.automations):
                state: _AutomationState = self._states.get(
                    i, _AutomationState(),
                )
                result.append({
                    "index": i,
                    "name": auto.get("name", ""),
                    "enabled": auto.get("enabled", True),
                    "active": state.active,
                    "last_triggered": state.last_trigger,
                })
        return result

    # --- MQTT callbacks ---------------------------------------------------

    def _subscribe_all(self, client: Any) -> None:
        """Subscribe to MQTT topics for all configured automations.

        Args:
            client: The paho MQTT client.
        """
        # Collect unique (label, characteristic) pairs.
        seen: set[tuple[str, str]] = set()
        for auto in self.automations:
            sensor: dict = auto.get("sensor", {})
            label: str = sensor.get("label", "")
            char: str = sensor.get("characteristic", "")
            if label and char:
                seen.add((label, char))

        # Also subscribe to all sensor subtopics for the data store.
        labels: set[str] = {label for label, _ in seen}
        for label in labels:
            for subtopic in ("motion", "temperature", "humidity", "status"):
                topic: str = f"{MQTT_PREFIX}/{label}/{subtopic}"
                client.subscribe(topic)
                logger.debug("Subscribed to %s", topic)

        logger.info(
            "Subscribed to %d sensor label(s)", len(labels),
        )

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, rc: int,
        properties: Any = None,
    ) -> None:
        """Subscribe to BLE sensor topics on connect.

        Args:
            client:     The paho MQTT client.
            userdata:   User data (unused).
            flags:      Connection flags.
            rc:         Return code (0 = success).
            properties: MQTT v5 properties (unused).
        """
        if rc != 0:
            logger.warning("Automation MQTT connect failed: rc=%d", rc)
            return
        self._subscribe_all(client)
        logger.info("Automation MQTT connected")

    def _on_message(
        self, client: Any, userdata: Any, msg: Any,
    ) -> None:
        """Handle incoming MQTT messages from BLE sensors.

        Args:
            client:   The paho MQTT client.
            userdata: User data (unused).
            msg:      The MQTT message.
        """
        try:
            parts: list[str] = msg.topic.split("/")
            if len(parts) != 4:
                return
            label: str = parts[2]
            subtopic: str = parts[3]
            payload: str = msg.payload.decode("utf-8", errors="replace")

            # Always update the sensor data store.
            if subtopic == "temperature":
                sensor_data.update(label, "temperature", float(payload))
            elif subtopic == "humidity":
                sensor_data.update(label, "humidity", float(payload))
            elif subtopic == "status":
                sensor_data.update(label, "status", json.loads(payload))
            elif subtopic == "motion":
                sensor_data.update(label, "motion", int(payload))

            # Evaluate automations that match this sensor + characteristic.
            self._evaluate_automations(label, subtopic, payload)

        except Exception as exc:
            logger.error(
                "Automation message error: %s", exc, exc_info=True,
            )

    # --- Trigger evaluation -----------------------------------------------

    def _evaluate_automations(
        self, label: str, characteristic: str, raw_payload: str,
    ) -> None:
        """Check all automations against an incoming sensor value.

        Args:
            label:          Sensor label.
            characteristic: Sensor characteristic (motion, temperature, etc.).
            raw_payload:    Raw MQTT payload string.
        """
        with self._lock:
            for i, auto in enumerate(self.automations):
                if not auto.get("enabled", True):
                    continue

                sensor: dict = auto.get("sensor", {})
                if sensor.get("label") != label:
                    continue
                if sensor.get("characteristic") != characteristic:
                    continue

                state: _AutomationState = self._states.get(
                    i, _AutomationState(),
                )

                # Parse payload to the appropriate type for comparison.
                trigger: dict = auto.get("trigger", {})
                try:
                    value: Any = self._parse_value(
                        raw_payload, trigger.get("value"),
                    )
                except (ValueError, TypeError):
                    continue

                condition: str = trigger.get("condition", "eq")
                threshold: Any = trigger.get("value")
                matched: bool = _evaluate_condition(
                    condition, threshold, value,
                )

                if matched:
                    # Reset watchdog timer on every matching event —
                    # e.g., each motion=1 pushes the off-timeout forward.
                    state.last_trigger = time.time()

                if matched and not state.active:
                    self._fire_action(i, auto, state)
                elif not matched and state.active:
                    # Check if the off_trigger is condition-based and
                    # this value satisfies the off condition.
                    off_trigger: dict = auto.get("off_trigger", {})
                    if off_trigger.get("type") == "condition":
                        off_cond: str = off_trigger.get("condition", "eq")
                        off_val: Any = off_trigger.get("value")
                        if _evaluate_condition(off_cond, off_val, value):
                            self._fire_off_action(i, auto, state)

    def _parse_value(self, raw: str, reference: Any) -> Any:
        """Parse a raw MQTT payload to match the reference type.

        Args:
            raw:       Raw string payload.
            reference: The trigger value (determines target type).

        Returns:
            Parsed value as int or float.
        """
        if isinstance(reference, int):
            return int(raw.strip())
        return float(raw.strip())

    def _fire_action(
        self,
        index: int,
        auto: dict[str, Any],
        state: _AutomationState,
    ) -> None:
        """Execute an automation's on-action.

        Args:
            index: Automation index.
            auto:  The automation config dict.
            state: The automation's runtime state.
        """
        now: float = time.time()

        # Debounce.
        if now - state.last_action < DEBOUNCE_SECONDS:
            return

        action: dict = auto.get("action", {})
        group: str = action.get("group", "")
        effect: str = action.get("effect", "on")
        params: dict = action.get("params", {})

        if not group:
            return

        device_id: str = _device_id_for_group(group)

        # Schedule conflict check.
        policy: str = auto.get("schedule_conflict", "defer")
        if policy == "defer" and self._is_schedule_active(group):
            logger.info(
                "Automation '%s' deferred — schedule active for %s",
                auto.get("name", "?"), group,
            )
            return

        # Fire the action.
        try:
            if effect == "off":
                # "Off" means stop + power off.
                self._dm.stop(device_id)
            else:
                self._dm.play(
                    device_id, effect, params, source="automation",
                )
            state.active = True
            state.last_action = now
            logger.info(
                "Automation '%s' fired: %s → %s (%s)",
                auto.get("name", "?"), effect, group,
                json.dumps(params),
            )
        except Exception as exc:
            logger.error(
                "Automation '%s' action failed: %s",
                auto.get("name", "?"), exc,
            )

    def _fire_off_action(
        self,
        index: int,
        auto: dict[str, Any],
        state: _AutomationState,
    ) -> None:
        """Execute an automation's off-action.

        Args:
            index: Automation index.
            auto:  The automation config dict.
            state: The automation's runtime state.
        """
        off_action: dict = auto.get("off_action", {})
        effect: str = off_action.get("effect", "off")
        params: dict = off_action.get("params", {})
        group: str = auto.get("action", {}).get("group", "")

        if not group:
            return

        device_id: str = _device_id_for_group(group)

        try:
            if effect == "off":
                self._dm.stop(device_id)
            else:
                self._dm.play(
                    device_id, effect, params, source="automation",
                )
            state.active = False
            state.last_off = time.time()
            logger.info(
                "Automation '%s' off-action: %s → %s",
                auto.get("name", "?"), effect, group,
            )
        except Exception as exc:
            logger.error(
                "Automation '%s' off-action failed: %s",
                auto.get("name", "?"), exc,
            )

    # --- Schedule conflict detection --------------------------------------

    def _is_schedule_active(self, group_name: str) -> bool:
        """Check if a schedule entry is active for a group.

        Uses the server's _find_active_entry if available.

        Args:
            group_name: The bare group name (no prefix).

        Returns:
            ``True`` if a schedule entry is currently active.
        """
        try:
            # Import here to avoid circular imports at module level.
            from server import _find_active_entry
            specs: list = self._config.get("schedule", [])
            loc: dict = self._config.get("location", {})
            lat: float = loc.get("latitude", 0.0)
            lon: float = loc.get("longitude", 0.0)
            now: datetime = datetime.now(timezone.utc).astimezone()
            active = _find_active_entry(specs, lat, lon, now, group_name)
            return active is not None
        except (ImportError, Exception) as exc:
            logger.debug("Schedule conflict check failed: %s", exc)
            return False

    # --- Watchdog ---------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Background thread: fire off-actions when sensors go stale.

        Checks every 60 seconds whether any watchdog-type automation's
        last trigger event exceeds its configured timeout.
        """
        while self._running:
            time.sleep(WATCHDOG_CHECK_INTERVAL)

            now: float = time.time()
            with self._lock:
                for i, auto in enumerate(self.automations):
                    if not auto.get("enabled", True):
                        continue

                    off_trigger: dict = auto.get("off_trigger", {})
                    if off_trigger.get("type") != "watchdog":
                        continue

                    state: _AutomationState = self._states.get(
                        i, _AutomationState(),
                    )
                    if not state.active:
                        continue

                    timeout_min: float = off_trigger.get(
                        "minutes", DEFAULT_WATCHDOG_MINUTES,
                    )
                    timeout_sec: float = timeout_min * SECONDS_PER_MINUTE
                    elapsed: float = now - state.last_trigger

                    if elapsed >= timeout_sec:
                        logger.info(
                            "Watchdog '%s': no trigger for %.0f min",
                            auto.get("name", "?"), elapsed / SECONDS_PER_MINUTE,
                        )
                        self._fire_off_action(i, auto, state)
