"""GlowUp API executor — dispatches voice intents to the server.

Translates structured intent dicts from the LLM into GlowUp REST
API calls and returns a confirmation response for TTS.

When the coordinator and GlowUp server run on the same machine
(Daedalus), this uses HTTP to localhost.  A future optimization
could call the handler in-process.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import urllib.parse
import urllib.request
from typing import Any

logger: logging.Logger = logging.getLogger("glowup.voice.executor")


class GlowUpExecutor:
    """Execute voice intents against the GlowUp REST API.

    Args:
        api_base:   GlowUp server URL (e.g., ``http://localhost:8420``).
        auth_token: Bearer token for GlowUp API authentication.
    """

    def __init__(
        self,
        api_base: str = "http://localhost:8420",
        auth_token: str = "",
    ) -> None:
        """Initialize the executor."""
        self._api_base: str = api_base.rstrip("/")
        self._auth_token: str = auth_token

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the GlowUp API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            path:   URL path (e.g., ``/api/devices/bedroom/power``).
            body:   Request body (will be JSON-encoded).

        Returns:
            Parsed JSON response dict.

        Raises:
            Exception: On HTTP or connection errors.
        """
        url: str = f"{self._api_base}{path}"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        }

        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, headers=headers, method=method,
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def execute(
        self, intent: dict[str, Any], room: str,
    ) -> dict[str, Any]:
        """Execute a voice intent.

        Maps the intent action to GlowUp API calls.  Returns a
        response with confirmation text and a ``speak`` flag.

        Args:
            intent: Parsed intent dict from the LLM with ``action``,
                    ``target``, and ``params``.
            room:   Originating room name (for context).

        Returns:
            Dict with ``status``, ``confirmation``, ``speak``.
        """
        action: str = intent.get("action", "unknown")
        target_raw: str = intent.get("target", "all")
        # URL-encode for API paths (spaces, special chars).
        target: str = urllib.parse.quote(target_raw, safe="")
        params: dict[str, Any] = intent.get("params", {})
        # Store raw target for confirmation text (not URL-encoded).
        self._target_raw: str = target_raw

        try:
            if action == "power":
                return self._exec_power(target, params)
            elif action == "brightness":
                return self._exec_brightness(target, params)
            elif action == "color":
                return self._exec_color(target, params)
            elif action == "temperature":
                return self._exec_temperature(target, params)
            elif action == "play_effect":
                return self._exec_play(target, params)
            elif action == "stop":
                return self._exec_stop(target)
            elif action == "query_sensor":
                return self._exec_query_sensor(target, params)
            elif action == "query_power":
                return self._exec_query_power(target)
            elif action == "query_status":
                return self._exec_query_status(target)
            elif action == "unknown":
                return {
                    "status": "error",
                    "confirmation": "I didn't understand that command.",
                    "speak": True,
                }
            else:
                return {
                    "status": "error",
                    "confirmation": f"I don't know how to {action}.",
                    "speak": True,
                }
        except Exception as exc:
            logger.error("Execution failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Something went wrong. Please try again.",
                "speak": True,
            }

    # --- Action handlers ---------------------------------------------------

    def _exec_power(
        self, target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn device/group on or off."""
        on: bool = params.get("on", True)
        self._request("POST", f"/api/devices/{target}/power", {"on": on})
        state: str = "on" if on else "off"
        return {
            "status": "ok",
            "confirmation": f"{self._target_raw} is {state}.",
            "speak": False,  # Physical change is the feedback.
        }

    def _exec_brightness(
        self, target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Set brightness level."""
        brightness: int = params.get("brightness", 100)
        self._request(
            "POST", f"/api/devices/{target}/brightness",
            {"brightness": brightness},
        )
        return {
            "status": "ok",
            "confirmation": f"{self._target_raw} brightness set to {brightness}%.",
            "speak": False,
        }

    def _exec_color(
        self, target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Set light color."""
        color: str = params.get("color", "white")
        # Color resolution happens server-side.
        self._request(
            "POST", f"/api/devices/{target}/play",
            {"effect": "solid", "params": {"color": color}},
        )
        return {
            "status": "ok",
            "confirmation": f"{self._target_raw} set to {color}.",
            "speak": False,
        }

    def _exec_temperature(
        self, target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Set color temperature."""
        temp: int = params.get("temperature", 4000)
        self._request(
            "POST", f"/api/devices/{target}/play",
            {"effect": "solid", "params": {"kelvin": temp}},
        )
        return {
            "status": "ok",
            "confirmation": f"{self._target_raw} set to {temp}K.",
            "speak": False,
        }

    def _exec_play(
        self, target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Start a lighting effect."""
        effect: str = params.get("effect", "breathe")
        self._request(
            "POST", f"/api/devices/{target}/play",
            {"effect": effect},
        )
        return {
            "status": "ok",
            "confirmation": f"Playing {effect} on {target}.",
            "speak": False,
        }

    def _exec_stop(self, target: str) -> dict[str, Any]:
        """Stop current effect."""
        self._request("POST", f"/api/devices/{target}/stop", {})
        return {
            "status": "ok",
            "confirmation": f"{self._target_raw} stopped.",
            "speak": False,
        }

    def _exec_query_sensor(
        self, target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query sensor readings."""
        data = self._request("GET", "/api/ble/sensors")
        sensor_type: str = params.get("sensor_type", "temperature")

        # Find matching sensor.
        for label, readings in data.items():
            if target.lower() in label.lower():
                value = readings.get(sensor_type)
                if value is not None:
                    if sensor_type == "temperature":
                        # Convert Celsius to Fahrenheit.
                        f_val: float = value * 9 / 5 + 32
                        return {
                            "status": "ok",
                            "confirmation": (
                                f"The {label} temperature is "
                                f"{f_val:.0f} degrees."
                            ),
                            "speak": True,
                        }
                    elif sensor_type == "humidity":
                        return {
                            "status": "ok",
                            "confirmation": (
                                f"The {label} humidity is "
                                f"{value:.0f} percent."
                            ),
                            "speak": True,
                        }
                    elif sensor_type == "motion":
                        state: str = "detected" if value else "clear"
                        return {
                            "status": "ok",
                            "confirmation": f"Motion in {label} is {state}.",
                            "speak": True,
                        }

        return {
            "status": "ok",
            "confirmation": f"I don't have {sensor_type} data for {target}.",
            "speak": True,
        }

    def _exec_query_power(self, target: str) -> dict[str, Any]:
        """Query power consumption."""
        # Try device-specific summary first.
        try:
            data = self._request(
                "GET", f"/api/power/summary?device={target}",
            )
        except Exception:
            data = self._request("GET", "/api/power/summary")

        avg: float = data.get("avg_watts", 0)
        peak: float = data.get("peak_watts", 0)
        kwh: float = data.get("total_kwh", 0)
        cost: float = kwh * 0.171  # Rate per kWh.

        return {
            "status": "ok",
            "confirmation": (
                f"{self._target_raw} averages {avg:.0f} watts, "
                f"peaked at {peak:.0f} watts. "
                f"Total energy {kwh:.1f} kilowatt hours, "
                f"costing about ${cost:.2f}."
            ),
            "speak": True,
        }

    def _exec_query_status(self, target: str) -> dict[str, Any]:
        """Query what effect is currently playing."""
        try:
            data = self._request(
                "GET", f"/api/devices/{target}/status",
            )
            effect: str = data.get("effect", "nothing")
            if effect and effect != "none":
                elapsed: float = data.get("elapsed", 0)
                return {
                    "status": "ok",
                    "confirmation": (
                        f"{self._target_raw} is playing {effect}, "
                        f"running for {elapsed:.0f} seconds."
                    ),
                    "speak": True,
                }
            else:
                return {
                    "status": "ok",
                    "confirmation": f"Nothing is playing on {target}.",
                    "speak": True,
                }
        except Exception:
            return {
                "status": "ok",
                "confirmation": f"I can't check the status of {target}.",
                "speak": True,
            }
