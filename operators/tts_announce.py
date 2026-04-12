"""TTS announcement operator — speak a phrase when a signal fires.

Publishes a TTS message to ``glowup/voice/tts_text`` when a watched
signal hits a trigger condition.  The target satellite speaks the
phrase through its local audio output (Piper TTS or Baichuan doorbell
talk path).

Debounce prevents repeated announcements — configurable as a Param
and bindable via the param-as-signal system.

Configuration example::

    {
        "type": "tts_announce",
        "name": "Welcome Doorbell",
        "sensor": {
            "type": "nvr",
            "label": "doorbell",
            "characteristic": "person"
        },
        "trigger": {"condition": "eq", "value": 1},
        "room": "Front Doorbell",
        "text": "Welcome",
        "debounce_seconds": 60.0
    }
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import time
from typing import Any, Optional

from param import Param
from operators import (
    Operator,
    SignalValue,
    TICK_BOTH,
)

logger: logging.Logger = logging.getLogger("glowup.operators.tts_announce")

# MQTT topic the voice satellites subscribe to for TTS text.
TTS_TEXT_TOPIC: str = "glowup/voice/tts_text"


class TtsAnnounceOperator(Operator):
    """Speak a phrase through a voice satellite when a signal fires.

    Watches a single signal (constructed from sensor config) and
    publishes a TTS message via MQTT when the trigger condition is met.
    Debounce prevents rapid repeated announcements.
    """

    operator_type: str = "tts_announce"
    description: str = "TTS announcement on signal trigger"
    input_signals: list[str] = []
    output_signals: list[str] = []
    tick_mode: str = TICK_BOTH
    tick_hz: float = 1.0

    # Minimum seconds between repeated announcements.
    debounce_seconds = Param(
        60.0, min=5.0, max=3600.0,
        description="Seconds between repeated announcements",
    )

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        """Initialize the TTS announcement operator.

        Args:
            name:   Instance name.
            config: Operator config from server.json.
            bus:    SignalBus instance.
        """
        super().__init__(name, config, bus)

        # Signal to watch — built from sensor config.
        sensor_cfg: dict[str, Any] = config.get("sensor", {})
        label: str = sensor_cfg.get("label", "")
        characteristic: str = sensor_cfg.get("characteristic", "")
        self._signal: str = f"{label}:{characteristic}" if label else ""
        self.input_signals = [self._signal] if self._signal else []

        # Trigger condition.
        trigger_cfg: dict[str, Any] = config.get("trigger", {})
        self._condition: str = trigger_cfg.get("condition", "eq")
        self._trigger_value: float = float(trigger_cfg.get("value", 1))

        # TTS config.
        self._room: str = config.get("room", "")
        self._text: str = config.get("text", "")

        # MQTT client — injected from server config during on_configure.
        self._mqtt_client: Any = None

        # Edge detection state.
        self._last_announce: float = 0.0

    def on_configure(self, config: dict[str, Any]) -> None:
        """Capture the MQTT client from the server config.

        Args:
            config: Full server configuration.
        """
        self._mqtt_client = config.get("_mqtt_client")

    def on_start(self) -> None:
        """Log startup."""
        logger.info(
            "TtsAnnounceOperator '%s' started — signal: %s, "
            "room: %s, text: '%s', debounce: %.0fs",
            self.name, self._signal, self._room, self._text,
            self.debounce_seconds,
        )

    def on_signal(self, name: str, value: SignalValue) -> None:
        """React to signal change — announce if trigger fires.

        Args:
            name:  Signal name.
            value: New signal value.
        """
        if not self._matches_trigger(value):
            return
        self._try_announce()

    def on_tick(self, dt: float) -> None:
        """Periodic check — no-op for now.  Debounce is handled in on_signal."""

    def _matches_trigger(self, value: SignalValue) -> bool:
        """Check if the signal value matches the trigger condition.

        Args:
            value: Signal value to test.

        Returns:
            True if the trigger condition is met.
        """
        if isinstance(value, list):
            return False
        try:
            fval: float = float(value)
        except (ValueError, TypeError):
            return False
        if self._condition == "eq":
            return fval == self._trigger_value
        elif self._condition == "gt":
            return fval > self._trigger_value
        elif self._condition == "lt":
            return fval < self._trigger_value
        return False

    def _try_announce(self) -> None:
        """Publish TTS if debounce window has passed."""
        now: float = time.monotonic()
        if now - self._last_announce < self.debounce_seconds:
            return

        if not self._mqtt_client:
            logger.warning(
                "TtsAnnounceOperator '%s' has no MQTT client — "
                "cannot publish TTS",
                self.name,
            )
            return

        if not self._room or not self._text:
            logger.warning(
                "TtsAnnounceOperator '%s' missing room or text", self.name,
            )
            return

        payload: dict[str, Any] = {
            "room": self._room,
            "text": self._text,
        }
        try:
            self._mqtt_client.publish(
                TTS_TEXT_TOPIC,
                json.dumps(payload),
                qos=0,
            )
            self._last_announce = now
            logger.info(
                "TtsAnnounceOperator '%s' announced: '%s' -> %s",
                self.name, self._text, self._room,
            )
        except Exception as exc:
            logger.warning(
                "TtsAnnounceOperator '%s' MQTT publish failed: %s",
                self.name, exc,
            )

    def get_status(self) -> dict[str, Any]:
        """Return operator status."""
        status: dict[str, Any] = super().get_status()
        status["signal"] = self._signal
        status["room"] = self._room
        status["text"] = self._text
        status["last_announce"] = self._last_announce
        return status
