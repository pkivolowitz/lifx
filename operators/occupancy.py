"""OccupancyOperator — derive HOME/AWAY from aggregate lock state.

Reads ``*:*:lock_state`` signals from the bus and derives a single
``house:occupancy:state`` signal: ``1.0`` (HOME) or ``0.0`` (AWAY).

Logic:
    - Any lock unlocked → HOME immediately.
    - All locks locked for ``away_confirm_seconds`` → AWAY.
    - The debounce prevents false AWAY during normal activity (lock the
      front door while walking to the back door).

KwikSet/Vivint locks don't report lock direction (inside vs outside).
The debounce window is the heuristic — when leaving, you lock the last
door and walk away.  The 120-second default eliminates false positives
from normal locking/unlocking during daily activity.

The household has 3 dogs.  Motion sensors fire constantly when humans
are away.  Lock state is the only clean human/pet discriminator — dogs
can't work a deadbolt.

Persistence: writes occupancy transitions to SQLite so state survives
server restart.  On startup, restores last-known state from the DB.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

from operators import Operator, TICK_BOTH, SignalValue
from param import Param

logger: logging.Logger = logging.getLogger("glowup.operators.occupancy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Output signal name.
OCCUPANCY_SIGNAL: str = "house:occupancy:state"

# Occupancy values.
HOME: float = 1.0
AWAY: float = 0.0

# Default debounce before transitioning to AWAY (seconds).
DEFAULT_AWAY_CONFIRM_SECONDS: float = 120.0

# Minimum debounce to prevent accidental AWAY flickers.
MIN_AWAY_CONFIRM_SECONDS: float = 30.0

# Maximum debounce.
MAX_AWAY_CONFIRM_SECONDS: float = 600.0

# SQLite table for occupancy persistence.
_OCCUPANCY_DDL: str = """
CREATE TABLE IF NOT EXISTS occupancy (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    state      TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
)
"""

# Singleton row ID.
_OCCUPANCY_ROW_ID: int = 1


# ---------------------------------------------------------------------------
# OccupancyOperator
# ---------------------------------------------------------------------------

class OccupancyOperator(Operator):
    """Derive HOME/AWAY from aggregate lock state.

    Reads lock signals from the bus, applies a debounce timer for AWAY
    transitions, and writes a single occupancy signal.  Persists state
    to SQLite so the dashboard doesn't show UNKNOWN after restart.

    Config example::

        {
            "type": "occupancy",
            "name": "house_occupancy",
            "tick_hz": 1.0,
            "away_confirm_seconds": 120,
            "db_path": "/etc/glowup/state.db"
        }
    """

    operator_type: str = "occupancy"
    description: str = "Derive HOME/AWAY from aggregate lock state"

    input_signals: list[str] = ["*:lock_state"]
    output_signals: list[str] = [OCCUPANCY_SIGNAL]

    tick_mode: str = TICK_BOTH
    tick_hz: float = 1.0

    away_confirm_seconds = Param(
        DEFAULT_AWAY_CONFIRM_SECONDS,
        min=MIN_AWAY_CONFIRM_SECONDS,
        max=MAX_AWAY_CONFIRM_SECONDS,
        description="Seconds all locks must remain locked before AWAY",
    )

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        """Initialize the occupancy operator.

        Args:
            name:   Instance name.
            config: Operator config dict.
            bus:    SignalBus instance.
        """
        super().__init__(name, config, bus)

        # Per-lock last-known state: signal_name → float (1.0=locked, 0.0=unlocked).
        self._lock_states: dict[str, float] = {}

        # Monotonic timestamp when all locks first became locked.
        # None if any lock is unlocked.
        self._all_locked_since: Optional[float] = None

        # Current occupancy state.
        self._occupancy: float = HOME

        # SQLite persistence.
        self._db_path: str = config.get("db_path", "")
        self._db: Optional[sqlite3.Connection] = None

    def on_configure(self, config: dict[str, Any]) -> None:
        """Open SQLite and restore last-known occupancy.

        Args:
            config: Full server config.
        """
        if not self._db_path:
            # Try to derive from config location.
            import os
            config_path: str = config.get("_config_path", "")
            if config_path:
                self._db_path = os.path.join(
                    os.path.dirname(os.path.abspath(config_path)),
                    "state.db",
                )

        if self._db_path:
            try:
                self._db = sqlite3.connect(
                    self._db_path,
                    check_same_thread=False,
                )
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute(_OCCUPANCY_DDL)
                self._db.commit()
                self._restore_from_db()
                logger.info(
                    "Occupancy state DB opened: %s", self._db_path,
                )
            except Exception as exc:
                logger.error(
                    "Failed to open occupancy DB at %s: %s",
                    self._db_path, exc,
                )
                self._db = None

    def on_start(self) -> None:
        """Write initial occupancy signal to bus."""
        self.write(OCCUPANCY_SIGNAL, self._occupancy)
        state_str: str = "HOME" if self._occupancy == HOME else "AWAY"
        logger.info("OccupancyOperator started — initial state: %s", state_str)

    def on_signal(self, name: str, value: SignalValue) -> None:
        """Handle a lock state change.

        Args:
            name:  Signal name (e.g., ``"vivint:front_door_lock:lock_state"``).
            value: Lock state: ``1.0`` (locked) or ``0.0`` (unlocked).
        """
        # Coerce to float.
        try:
            fval: float = float(value) if not isinstance(value, list) else 0.0
        except (ValueError, TypeError):
            return

        prev: Optional[float] = self._lock_states.get(name)
        self._lock_states[name] = fval

        if fval == 0.0:
            # --- Unlocked: transition to HOME immediately ---
            self._all_locked_since = None
            if self._occupancy != HOME:
                self._set_occupancy(HOME)
                logger.info(
                    "Occupancy → HOME (lock unlocked: %s)", name,
                )
        else:
            # --- Locked: check if ALL locks are now locked ---
            if self._all_are_locked():
                if self._all_locked_since is None:
                    self._all_locked_since = time.monotonic()
                    logger.debug(
                        "All locks locked — AWAY debounce started "
                        "(%.0fs)", self.away_confirm_seconds,
                    )
            else:
                self._all_locked_since = None

    def on_tick(self, dt: float) -> None:
        """Check the AWAY debounce timer.

        Args:
            dt: Seconds since last tick.
        """
        if self._all_locked_since is None:
            return
        if self._occupancy == AWAY:
            return  # Already AWAY, nothing to do.

        elapsed: float = time.monotonic() - self._all_locked_since
        if elapsed >= self.away_confirm_seconds:
            self._set_occupancy(AWAY)
            logger.info(
                "Occupancy → AWAY (all locks locked for %.0fs)", elapsed,
            )

    def on_stop(self) -> None:
        """Close SQLite connection."""
        if self._db:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None

    # --- Public query ------------------------------------------------------

    @property
    def occupancy_state(self) -> str:
        """Return human-readable occupancy state.

        Returns:
            ``"HOME"``, ``"AWAY"``, or ``"UNKNOWN"``.
        """
        if self._occupancy == HOME:
            return "HOME"
        elif self._occupancy == AWAY:
            return "AWAY"
        return "UNKNOWN"

    def is_home(self) -> bool:
        """Return whether the household is home.

        Returns:
            ``True`` if occupancy is HOME.
        """
        return self._occupancy == HOME

    # --- Helpers -----------------------------------------------------------

    def _all_are_locked(self) -> bool:
        """Check if every known lock is in the locked state.

        Returns:
            ``True`` if all locks report 1.0 (locked).
            ``False`` if any lock is unlocked or no locks are known.
        """
        if not self._lock_states:
            return False
        return all(v == 1.0 for v in self._lock_states.values())

    def _set_occupancy(self, state: float) -> None:
        """Update occupancy state, write to bus, and persist.

        Args:
            state: ``HOME`` (1.0) or ``AWAY`` (0.0).
        """
        self._occupancy = state
        self.write(OCCUPANCY_SIGNAL, state)
        self._persist_to_db(state)

    def _persist_to_db(self, state: float) -> None:
        """Write occupancy state to SQLite.

        Args:
            state: ``HOME`` (1.0) or ``AWAY`` (0.0).
        """
        if not self._db:
            return
        state_str: str = "HOME" if state == HOME else "AWAY"
        now_str: str = datetime.now(timezone.utc).isoformat()
        try:
            self._db.execute(
                "INSERT INTO occupancy (id, state, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
                "updated_at=excluded.updated_at",
                (_OCCUPANCY_ROW_ID, state_str, now_str),
            )
            self._db.commit()
        except Exception as exc:
            logger.error("Failed to persist occupancy: %s", exc)

    def _restore_from_db(self) -> None:
        """Restore last-known occupancy from SQLite."""
        if not self._db:
            return
        try:
            cursor = self._db.execute(
                "SELECT state FROM occupancy WHERE id = ?",
                (_OCCUPANCY_ROW_ID,),
            )
            row = cursor.fetchone()
            if row:
                state_str: str = row[0]
                if state_str == "AWAY":
                    self._occupancy = AWAY
                else:
                    self._occupancy = HOME
                logger.info(
                    "Restored occupancy from DB: %s", state_str,
                )
        except Exception as exc:
            logger.debug("No occupancy state in DB: %s", exc)
