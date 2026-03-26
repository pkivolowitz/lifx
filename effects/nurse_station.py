"""Nurse station ambient patient census on LIFX Luna.

Subscribes to Retro-Med MQTT telemetry (aeye/+/telemetry, aeye/+/status)
and renders one full column per patient on the Luna's 7×5 grid.  Column
color reflects worst-case vital severity; column brightness pulses at
the patient's respiratory rate.

Layout (3-patient demo, columns 2-3-4 centered)::

    _  .  P2 P3 P4 .  _
    .  .  P2 P3 P4 .  .
    .  .  P2 P3 P4 .  .
    .  .  P2 P3 P4 .  .
    _  .  P2 P3 P4 .  _

States:
    - Pulsing green:  all vitals normal, pulse = respiratory rate
    - Pulsing yellow: any vital in warning range
    - Solid red:      any vital critical (stillness draws the eye)
    - Dim blue:       device offline / no data
    - Dark:           unassigned column
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import math
import re
import threading
import time
from typing import Any, Optional

from . import (
    DEVICE_TYPE_MATRIX,
    Effect, Param, HSBK,
    HSBK_MAX, KELVIN_DEFAULT,
    hue_to_u16,
)

logger: logging.Logger = logging.getLogger("glowup.effects.nurse_station")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Luna grid dimensions.
GRID_WIDTH: int = 7
GRID_HEIGHT: int = 5
GRID_SIZE: int = GRID_WIDTH * GRID_HEIGHT  # 35 protocol slots

# Assignable columns (full 5-row height).  Columns 0 and 6 are partial
# (dead corners at rows 0 and 4) so we skip them.
# Spread evenly across the oval for visual separation — the Luna's
# heavy diffuser bleeds adjacent columns together.
ASSIGNABLE_COLUMNS: list[int] = [1, 3, 5, 2, 4]

# Maximum simultaneous patients (one per assignable column).
MAX_PATIENTS: int = len(ASSIGNABLE_COLUMNS)

# Two pi — one full sine cycle.
TWO_PI: float = 2.0 * math.pi

# Severity levels — higher is worse.
SEVERITY_NORMAL: int = 0
SEVERITY_WARNING: int = 1
SEVERITY_CRITICAL: int = 2

# HSBK colors for each severity level.
# Green: hue 120°, full sat, moderate brightness.
COLOR_NORMAL: tuple[int, int] = (hue_to_u16(120.0), HSBK_MAX)
# Yellow: hue 50°, full sat, full brightness.
COLOR_WARNING: tuple[int, int] = (hue_to_u16(50.0), HSBK_MAX)
# Red: hue 0°, full sat, full brightness.
COLOR_CRITICAL: tuple[int, int] = (hue_to_u16(0.0), HSBK_MAX)
# Dim blue for offline: hue 220°, full sat, low brightness.
COLOR_OFFLINE: HSBK = (hue_to_u16(220.0), HSBK_MAX, int(HSBK_MAX * 0.05), KELVIN_DEFAULT)

# Black — unassigned zones and dead corners.
BLACK: HSBK = (0, 0, 0, KELVIN_DEFAULT)

# Default respiratory rate (breaths/min) when RR is unavailable.
DEFAULT_RR: float = 15.0

# Minimum brightness during the breathing pulse (fraction of max).
# Full black at the bottom of the breath — the Luna's heavy diffuser
# smears brightness, so anything above zero looks lit.
BREATHE_MIN_FRACTION: float = 0.0

# Maximum brightness for normal and warning states.
# Luna LEDs are absurdly bright — keep well below max.
BREATHE_MAX_BRIGHTNESS: int = int(HSBK_MAX * 0.25)

# Brightness for solid critical (no pulse).
CRITICAL_BRIGHTNESS: int = int(HSBK_MAX * 0.35)

# MQTT topic patterns for Retro-Med.
TOPIC_TELEMETRY: str = "aeye/+/telemetry"
TOPIC_STATUS: str = "aeye/+/status"

# Regex to extract the device_id from an MQTT topic.
# Matches "aeye/{device_id}/telemetry" or "aeye/{device_id}/status".
TOPIC_RE = re.compile(r"^aeye/([^/]+)/(telemetry|status)$")

# NODATA sentinel from Retro-Med (classifier returns this when
# confidence is too low or zone is blank).
NODATA_MARKER: str = "----"

# ---------------------------------------------------------------------------
# Vital threshold table
# ---------------------------------------------------------------------------

# Each entry: (legend, normal_low, normal_high, warn_low, warn_high).
# Values outside warn_low..warn_high are critical.
# Legends are case-insensitive for matching.
VITAL_THRESHOLDS: list[tuple[str, float, float, float, float]] = [
    # legend   normal_lo  normal_hi  warn_lo  warn_hi
    ("HR",       60.0,     100.0,     50.0,    120.0),
    ("SpO2",     95.0,     100.0,     90.0,    100.0),
    ("RR",       12.0,      20.0,      8.0,     28.0),
    ("SYS",      90.0,     140.0,     80.0,    160.0),
    ("NBPs",     90.0,     140.0,     80.0,    160.0),
]

# Build a fast lookup by uppercase legend.
_THRESHOLD_MAP: dict[str, tuple[float, float, float, float]] = {
    legend.upper(): (nlo, nhi, wlo, whi)
    for legend, nlo, nhi, wlo, whi in VITAL_THRESHOLDS
}


# ---------------------------------------------------------------------------
# Per-device state
# ---------------------------------------------------------------------------

class _PatientState:
    """Mutable state for one monitored device/patient.

    Attributes:
        device_id: Stable Retro-Med device identifier.
        description: Human-readable device name (from birth cert).
        column: Assigned Luna column index (0-6) or -1 if unassigned.
        severity: Worst-case severity across all vitals.
        rr: Current respiratory rate (breaths/min) or None.
        last_seen: Monotonic timestamp of last telemetry.
        online: True if the device has not sent a death certificate.
    """

    __slots__ = ("device_id", "description", "column", "severity",
                 "rr", "last_seen", "online")

    def __init__(self, device_id: str) -> None:
        self.device_id: str = device_id
        self.description: str = ""
        self.column: int = -1
        self.severity: int = SEVERITY_NORMAL
        self.rr: Optional[float] = None
        self.last_seen: float = time.monotonic()
        self.online: bool = True


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

def _parse_numeric(display_value: str) -> Optional[float]:
    """Extract a numeric value from a Retro-Med display string.

    Handles formats like ``"72 bpm"``, ``"95 %"``, ``"120/80 mmHg"``,
    and the NODATA sentinel ``"----"``.

    For blood pressure (contains ``/``), returns the systolic (first) value.

    Args:
        display_value: Raw display string from telemetry data entry.

    Returns:
        Extracted float, or None if unparseable or NODATA.
    """
    s: str = display_value.strip()
    if not s or NODATA_MARKER in s:
        return None
    # Strip units: take everything before the first non-numeric,
    # non-slash, non-dot, non-minus character sequence.
    # But first handle BP: "120/80 mmHg" → take "120".
    if "/" in s:
        s = s.split("/")[0]
    # Extract leading number.
    match = re.match(r"[-+]?\d*\.?\d+", s)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


def _classify_vital(legend: str, value: float) -> int:
    """Classify a single vital reading as normal, warning, or critical.

    Args:
        legend: Zone legend from telemetry (e.g., ``"HR"``).
        value: Parsed numeric value.

    Returns:
        Severity level (SEVERITY_NORMAL, SEVERITY_WARNING, or
        SEVERITY_CRITICAL).
    """
    bounds: Optional[tuple[float, float, float, float]] = (
        _THRESHOLD_MAP.get(legend.upper())
    )
    if bounds is None:
        # Unknown legend — not a monitored vital, ignore.
        return SEVERITY_NORMAL
    _nlo, _nhi, wlo, whi = bounds
    if value < wlo or value > whi:
        return SEVERITY_CRITICAL
    if value < _nlo or value > _nhi:
        return SEVERITY_WARNING
    return SEVERITY_NORMAL


# ---------------------------------------------------------------------------
# Effect
# ---------------------------------------------------------------------------

class NurseStation(Effect):
    """Patient census — one column per patient, color-coded severity.

    Subscribes to Retro-Med MQTT telemetry and renders patient status
    on the LIFX Luna matrix.  Each patient occupies a full column (5
    LEDs).  Color reflects worst-case vital severity; brightness pulses
    at the patient's respiratory rate.
    """

    name: str = "nurse_station"
    description: str = "Patient census on Luna — color-coded vitals"
    affinity: frozenset[str] = frozenset({DEVICE_TYPE_MATRIX})

    # Tunable parameters.
    broker = Param("localhost", description="MQTT broker for Retro-Med telemetry")
    mqtt_port = Param(1883, min=1, max=65535, description="MQTT broker port")
    stale_timeout = Param(10.0, min=2.0, max=60.0,
                          description="Seconds with no telemetry before marking stale")

    def __init__(self, **overrides: Any) -> None:
        super().__init__(**overrides)
        # Per-device patient state, keyed by device_id.
        self._patients: dict[str, _PatientState] = {}
        # Lock protects _patients from concurrent MQTT callback + render.
        self._lock: threading.Lock = threading.Lock()
        # Column assignment order — next column to assign.
        self._next_col_idx: int = 0
        # MQTT client and thread.
        self._mqtt_client: Any = None
        self._mqtt_thread: Optional[threading.Thread] = None
        self._stopping: bool = False

    # ---- Lifecycle --------------------------------------------------------

    def on_start(self, zone_count: int) -> None:
        """Spawn MQTT subscriber thread.

        Args:
            zone_count: Number of zones on the target device.
        """
        self._stopping = False
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error("paho-mqtt not installed — nurse_station disabled")
            return

        # paho-mqtt v2 requires CallbackAPIVersion.
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = self._on_connect
        client.on_message = self._on_message

        self._mqtt_client = client

        def _mqtt_loop() -> None:
            """Background MQTT network loop."""
            try:
                client.connect(self.broker, int(self.mqtt_port), keepalive=60)
                client.loop_forever()
            except Exception as exc:
                if not self._stopping:
                    logger.error("MQTT loop error: %s", exc)

        self._mqtt_thread = threading.Thread(
            target=_mqtt_loop, daemon=True, name="nurse-mqtt",
        )
        self._mqtt_thread.start()
        logger.info(
            "Nurse station started — MQTT %s:%s",
            self.broker, self.mqtt_port,
        )

    def on_stop(self) -> None:
        """Disconnect MQTT and join the subscriber thread."""
        self._stopping = True
        if self._mqtt_client is not None:
            try:
                self._mqtt_client.disconnect()
            except Exception:
                pass
            self._mqtt_client = None
        if self._mqtt_thread is not None:
            self._mqtt_thread.join(timeout=3.0)
            self._mqtt_thread = None
        logger.info("Nurse station stopped")

    # ---- MQTT callbacks ---------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any,
                    flags: Any, rc: int,
                    properties: Any = None) -> None:
        """Subscribe to Retro-Med topics on connect.

        Args:
            client: paho MQTT client instance.
            userdata: User data (unused).
            flags: Connection flags dict.
            rc: Connection result code (0 = success).
            properties: MQTT v5 properties (unused, required by paho v2).
        """
        if rc != 0:
            logger.warning("MQTT connect failed (rc=%d)", rc)
            return
        client.subscribe(TOPIC_TELEMETRY)
        client.subscribe(TOPIC_STATUS)
        logger.info("Subscribed to %s and %s", TOPIC_TELEMETRY, TOPIC_STATUS)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Route incoming MQTT messages to telemetry or status handlers.

        Args:
            client: paho MQTT client instance.
            userdata: User data (unused).
            msg: paho MQTTMessage with topic and payload.
        """
        m = TOPIC_RE.match(msg.topic)
        if m is None:
            return
        device_id: str = m.group(1)
        msg_type: str = m.group(2)

        try:
            data: dict[str, Any] = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("Bad JSON from %s: %s", msg.topic, exc)
            return

        if msg_type == "status":
            self._handle_status(device_id, data)
        elif msg_type == "telemetry":
            self._handle_telemetry(device_id, data)

    # ---- Message handlers -------------------------------------------------

    def _get_or_create(self, device_id: str) -> _PatientState:
        """Get or create a patient state, assigning a column if needed.

        Caller must hold ``_lock``.

        Args:
            device_id: Retro-Med device identifier.

        Returns:
            The patient state for this device.
        """
        patient: Optional[_PatientState] = self._patients.get(device_id)
        if patient is not None:
            return patient
        patient = _PatientState(device_id)
        # Assign next available column.
        if self._next_col_idx < MAX_PATIENTS:
            patient.column = ASSIGNABLE_COLUMNS[self._next_col_idx]
            self._next_col_idx += 1
            logger.info(
                "Assigned %s to column %d", device_id, patient.column,
            )
        else:
            logger.warning(
                "No columns left for %s (max %d patients)",
                device_id, MAX_PATIENTS,
            )
        self._patients[device_id] = patient
        return patient

    def _handle_status(self, device_id: str, data: dict[str, Any]) -> None:
        """Process a birth or death certificate.

        Args:
            device_id: Retro-Med device identifier.
            data: Parsed status JSON payload.
        """
        state: str = data.get("state", "")
        with self._lock:
            patient: _PatientState = self._get_or_create(device_id)
            patient.description = data.get("description", patient.description)
            if state == "offline":
                patient.online = False
                logger.info("Device offline: %s", device_id)
            else:
                patient.online = True

    def _handle_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        """Process a telemetry packet — update severity and RR.

        Args:
            device_id: Retro-Med device identifier.
            data: Parsed telemetry JSON payload.
        """
        readings: list[list[str]] = data.get("data", [])
        worst: int = SEVERITY_NORMAL
        rr_value: Optional[float] = None

        for entry in readings:
            if len(entry) < 5:
                continue
            legend: str = entry[1]
            display_val: str = entry[2]
            numeric: Optional[float] = _parse_numeric(display_val)

            # Capture respiratory rate for breathing animation.
            if legend.upper() == "RR" and numeric is not None:
                rr_value = numeric

            if numeric is not None:
                sev: int = _classify_vital(legend, numeric)
                if sev > worst:
                    worst = sev

        with self._lock:
            patient: _PatientState = self._get_or_create(device_id)
            patient.severity = worst
            patient.last_seen = time.monotonic()
            patient.online = True
            if rr_value is not None:
                patient.rr = rr_value

    # ---- Rendering --------------------------------------------------------

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame for the Luna grid.

        Args:
            t: Seconds elapsed since effect started.
            zone_count: Number of zones (35 for Luna).

        Returns:
            List of *zone_count* HSBK tuples.
        """
        # Start with all zones dark.
        frame: list[HSBK] = [BLACK] * max(zone_count, GRID_SIZE)
        now: float = time.monotonic()

        with self._lock:
            for patient in self._patients.values():
                col: int = patient.column
                if col < 0:
                    # No column assigned (overflow).
                    continue

                color: HSBK = self._patient_color(patient, t, now)

                # Paint the entire column (all 5 rows).
                for row in range(GRID_HEIGHT):
                    zone_idx: int = row * GRID_WIDTH + col
                    if zone_idx < len(frame):
                        frame[zone_idx] = color

        # Truncate or pad to requested zone_count.
        return frame[:zone_count]

    def _patient_color(self, patient: _PatientState, t: float,
                       now: float) -> HSBK:
        """Compute the HSBK color for a patient's column this frame.

        Args:
            patient: Patient state object.
            t: Seconds elapsed since effect started.
            now: Current monotonic time (for staleness check).

        Returns:
            Single HSBK tuple to apply to all zones in the column.
        """
        # Offline → dim blue.
        if not patient.online:
            return COLOR_OFFLINE

        # Stale → dim blue (same visual as offline).
        elapsed: float = now - patient.last_seen
        if elapsed > self.stale_timeout:
            return COLOR_OFFLINE

        # Critical → solid red, no pulse.
        if patient.severity == SEVERITY_CRITICAL:
            return (COLOR_CRITICAL[0], COLOR_CRITICAL[1],
                    CRITICAL_BRIGHTNESS, KELVIN_DEFAULT)

        # Normal or warning → pulse at respiratory rate.
        if patient.severity == SEVERITY_WARNING:
            hue, sat = COLOR_WARNING
        else:
            hue, sat = COLOR_NORMAL

        # Breathing pulse: sinusoidal brightness modulation.
        rr: float = patient.rr if patient.rr is not None else DEFAULT_RR
        # Clamp RR to sane range to avoid div-by-zero or insane speeds.
        rr = max(4.0, min(40.0, rr))
        # Period in seconds = 60 / RR.
        period: float = 60.0 / rr
        # Sine wave: 0→1→0 over one period (use half-sine for inhale/exhale).
        phase: float = (t % period) / period
        # Smooth breathing curve: sin maps 0→0→1→0 over one period.
        breath: float = math.sin(phase * TWO_PI)
        # Map [-1, 1] to [BREATHE_MIN_FRACTION, 1.0].
        breath_norm: float = (breath + 1.0) / 2.0
        brightness_frac: float = (
            BREATHE_MIN_FRACTION
            + breath_norm * (1.0 - BREATHE_MIN_FRACTION)
        )
        brightness: int = int(BREATHE_MAX_BRIGHTNESS * brightness_frac)

        return (hue, sat, brightness, KELVIN_DEFAULT)
