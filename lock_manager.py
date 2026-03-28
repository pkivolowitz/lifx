"""Lock manager — presentation layer for the /home dashboard.

Bridges Vivint cloud lock signals and the ``/api/home/locks`` REST
endpoint.  Subscribes to MQTT lock topics, maps Vivint device names to
dashboard abbreviations, populates the server's ``_lock_state`` dict,
and persists state to SQLite for restart resilience.

This module does NOT derive occupancy — that is the
:class:`~operators.occupancy.OccupancyOperator`'s job.  This module
handles the user-facing presentation: lock circles, battery display,
and abbreviation mapping.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.lock_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefixes to subscribe to for lock data.
VIVINT_TOPIC_PREFIX: str = "glowup/vivint"
ZIGBEE_TOPIC_PREFIX: str = "glowup/zigbee"

# MQTT QoS for subscriptions.
MQTT_QOS: int = 1

# SQLite DDL for lock state persistence.
_LOCK_STATE_DDL: str = """
CREATE TABLE IF NOT EXISTS lock_state (
    abbr       TEXT    NOT NULL PRIMARY KEY,
    name       TEXT,
    locked     INTEGER,
    battery    INTEGER,
    updated_at TEXT    NOT NULL
)
"""

# ---------------------------------------------------------------------------
# Optional dependency
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
    _PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:
    _HAS_PAHO = False
    _PAHO_V2 = False
    mqtt = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# LockManager
# ---------------------------------------------------------------------------

class LockManager:
    """Manage lock display state for the /home dashboard.

    Subscribes to MQTT lock/battery topics from Vivint and Zigbee adapters,
    maps device names to lock abbreviations, and maintains the server's
    ``_lock_state`` dict that ``/api/home/locks`` reads.

    Args:
        config:  Full server config dict (reads ``"locks"`` section).
        server:  The HTTP server object (sets ``server._lock_state``).
        db_path: Path to SQLite state database.
        broker:  MQTT broker address.
        port:    MQTT broker port.
        bus:     Optional SignalBus for reading occupancy state.
    """

    def __init__(
        self,
        config: dict[str, Any],
        server: Any,
        db_path: str,
        broker: str = "localhost",
        port: int = 1883,
        bus: Any = None,
    ) -> None:
        """Initialize the lock manager.

        Args:
            config: Full server config dict.
            server: Server object whose ``_lock_state`` dict we populate.
            db_path: Path to SQLite database.
            broker: MQTT broker address.
            port: MQTT broker port.
            bus: Optional SignalBus for occupancy reads.
        """
        self._config: dict[str, Any] = config
        self._server: Any = server
        self._db_path: str = db_path
        self._broker: str = broker
        self._port: int = port
        self._bus: Any = bus
        self._client: Any = None
        self._db: Optional[sqlite3.Connection] = None

        # Lock config: list of {"abbr": "FD", "name": "Front Door",
        #                        "zigbee_name": "front_door_lock"}
        self._lock_defs: list[dict[str, Any]] = config.get("locks", [])

        # Build mapping: zigbee/vivint name → lock abbreviation.
        self._name_to_abbr: dict[str, str] = {}
        for lock in self._lock_defs:
            z_name: str = lock.get("zigbee_name", "")
            abbr: str = lock.get("abbr", "")
            if z_name and abbr:
                self._name_to_abbr[z_name] = abbr

        # Battery state: abbr → percentage (0-100 integer).
        self._battery: dict[str, int] = {}

    def start(self) -> None:
        """Open SQLite, restore state, start MQTT subscriber."""
        # Initialize server's _lock_state dict.
        if not hasattr(self._server, "_lock_state"):
            self._server._lock_state = {}

        # Open SQLite.
        try:
            self._db = sqlite3.connect(
                self._db_path, check_same_thread=False,
            )
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(_LOCK_STATE_DDL)
            self._db.commit()
            self._restore_from_db()
            logger.info("Lock state DB opened: %s", self._db_path)
        except Exception as exc:
            logger.error("Failed to open lock state DB: %s", exc)
            self._db = None

        # Start MQTT subscriber.
        if not _HAS_PAHO:
            logger.warning(
                "paho-mqtt not installed — LockManager MQTT disabled"
            )
            return
        if not self._lock_defs:
            logger.info("No locks configured — LockManager idle")
            return

        if _PAHO_V2:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"glowup-locks-{int(time.time())}",
            )
        else:
            self._client = mqtt.Client(
                client_id=f"glowup-locks-{int(time.time())}",
            )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()

        logger.info(
            "LockManager started — %d lock(s)", len(self._lock_defs),
        )

    def stop(self) -> None:
        """Stop MQTT subscriber and close SQLite."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        logger.info("LockManager stopped")

    # --- Public API --------------------------------------------------------

    def get_battery(self, abbr: str) -> Optional[int]:
        """Get battery percentage for a lock.

        Args:
            abbr: Lock abbreviation (e.g., ``"FD"``).

        Returns:
            Battery percentage (0-100) or None if unknown.
        """
        return self._battery.get(abbr)

    def get_occupancy_state(self) -> str:
        """Get current occupancy state from the SignalBus.

        Returns:
            ``"HOME"``, ``"AWAY"``, or ``"UNKNOWN"``.
        """
        if not self._bus:
            return "UNKNOWN"
        value: float = float(self._bus.read("house:occupancy:state", -1.0))
        if value == 1.0:
            return "HOME"
        elif value == 0.0:
            return "AWAY"
        return "UNKNOWN"

    # --- MQTT callbacks ----------------------------------------------------

    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, rc: int,
        properties: Any = None,
    ) -> None:
        """Subscribe to lock topics on connect.

        Args:
            client:     The paho MQTT client.
            userdata:   Unused.
            flags:      Connection flags.
            rc:         Return code.
            properties: MQTT v5 properties (unused).
        """
        if rc != 0:
            logger.warning("LockManager MQTT connect failed: rc=%d", rc)
            return
        # Subscribe to lock_state and battery from both adapters.
        for prefix in (VIVINT_TOPIC_PREFIX, ZIGBEE_TOPIC_PREFIX):
            client.subscribe(f"{prefix}/+/lock_state", qos=MQTT_QOS)
            client.subscribe(f"{prefix}/+/battery", qos=MQTT_QOS)
        logger.info("LockManager MQTT subscribed")

    def _on_message(
        self, client: Any, userdata: Any, msg: Any,
    ) -> None:
        """Handle lock state and battery MQTT messages.

        Args:
            client:   The paho MQTT client.
            userdata: Unused.
            msg:      The MQTT message.
        """
        try:
            parts: list[str] = msg.topic.split("/")
            if len(parts) != 4:
                return
            # parts: ["glowup", "vivint"|"zigbee", device_name, property]
            device_name: str = parts[2]
            prop: str = parts[3]
            payload: str = msg.payload.decode("utf-8", errors="replace")

            abbr: Optional[str] = self._name_to_abbr.get(device_name)
            if abbr is None:
                return  # Not a configured lock.

            if prop == "lock_state":
                locked: bool = payload.strip() == "1"
                self._server._lock_state[abbr] = locked
                self._persist_lock(abbr, locked)
                logger.debug("Lock %s → %s", abbr, "locked" if locked else "unlocked")

            elif prop == "battery":
                try:
                    # Battery arrives normalized (0.0-1.0) from adapters.
                    battery_norm: float = float(payload)
                    battery_pct: int = int(battery_norm * 100)
                    self._battery[abbr] = battery_pct
                    self._persist_battery(abbr, battery_pct)
                except (ValueError, TypeError):
                    pass

        except Exception as exc:
            logger.error("LockManager message error: %s", exc)

    # --- SQLite persistence ------------------------------------------------

    def _persist_lock(self, abbr: str, locked: bool) -> None:
        """Persist lock state to SQLite.

        Args:
            abbr:   Lock abbreviation.
            locked: Whether the lock is locked.
        """
        if not self._db:
            return
        now_str: str = datetime.now(timezone.utc).isoformat()
        lock_name: str = ""
        for lock in self._lock_defs:
            if lock.get("abbr") == abbr:
                lock_name = lock.get("name", "")
                break
        try:
            self._db.execute(
                "INSERT INTO lock_state (abbr, name, locked, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(abbr) DO UPDATE SET "
                "locked=excluded.locked, updated_at=excluded.updated_at",
                (abbr, lock_name, 1 if locked else 0, now_str),
            )
            self._db.commit()
        except Exception as exc:
            logger.error("Failed to persist lock state: %s", exc)

    def _persist_battery(self, abbr: str, battery_pct: int) -> None:
        """Persist battery level to SQLite.

        Args:
            abbr:        Lock abbreviation.
            battery_pct: Battery percentage (0-100).
        """
        if not self._db:
            return
        now_str: str = datetime.now(timezone.utc).isoformat()
        try:
            self._db.execute(
                "UPDATE lock_state SET battery=?, updated_at=? "
                "WHERE abbr=?",
                (battery_pct, now_str, abbr),
            )
            self._db.commit()
        except Exception as exc:
            logger.error("Failed to persist battery: %s", exc)

    def _restore_from_db(self) -> None:
        """Restore last-known lock states from SQLite."""
        if not self._db:
            return
        try:
            cursor = self._db.execute(
                "SELECT abbr, locked, battery FROM lock_state"
            )
            for row in cursor.fetchall():
                abbr: str = row[0]
                locked_int: Optional[int] = row[1]
                battery: Optional[int] = row[2]

                if locked_int is not None:
                    self._server._lock_state[abbr] = bool(locked_int)
                if battery is not None:
                    self._battery[abbr] = battery

            count: int = len(self._server._lock_state)
            if count:
                logger.info(
                    "Restored %d lock state(s) from DB", count,
                )
        except Exception as exc:
            logger.debug("No lock state in DB: %s", exc)
