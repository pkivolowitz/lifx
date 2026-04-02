"""Config-driven voice intent executor.

Dispatches voice intents to GlowUp API calls using action
definitions from ``actions.yml``.  Simple commands and queries
are handled generically by the config.  Complex queries use
named handler functions — the config equivalent of function
pointers.  Adding a new voice skill is a YAML entry, not code.

Chat (freeform Ollama conversation with history) is the one
special case that lives in code rather than config.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

logger: logging.Logger = logging.getLogger("glowup.voice.executor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mobile, AL residential electricity rate ($/kWh).
_ELECTRICITY_RATE_PER_KWH: float = 0.171

# ---------------------------------------------------------------------------
# Chat constants
# ---------------------------------------------------------------------------

# Maximum exchanges (user + assistant pairs) kept per room.
_CHAT_HISTORY_MAX: int = 10

# History expires after 30 minutes of inactivity.
_CHAT_HISTORY_TTL_S: float = 1800.0

# System prompt for freeform chat — concise spoken responses.
_CHAT_SYSTEM_PROMPT: str = (
    "You are GlowUp, a home assistant built on the Gemma 2 language "
    "model running locally via Ollama on a Mac Studio. Your responses "
    "are spoken aloud via text-to-speech. Be straightforward, polite, "
    "and factual. No humor, no sarcasm, no personality. Just answer "
    "the question. Every response MUST be 1-2 sentences maximum."
)

# ---------------------------------------------------------------------------
# Actions config path — adjacent to this module.
# ---------------------------------------------------------------------------

_ACTIONS_FILE: Path = Path(__file__).parent / "actions.yml"


class GlowUpExecutor:
    """Config-driven voice intent executor.

    Loads action definitions from ``actions.yml`` and dispatches
    intents through a generic command/query pipeline.  Complex
    queries delegate to named handler functions registered at init.

    Args:
        api_base:     GlowUp server URL.
        auth_token:   Bearer token for API auth.
        chat_model:   Ollama model for freeform chat.
        ollama_host:  Ollama API base URL.
    """

    def __init__(
        self,
        api_base: str = "http://localhost:8420",
        auth_token: str = "",
        chat_model: str = "gemma2:27b",
        ollama_host: str = "http://localhost:11434",
    ) -> None:
        """Initialize the executor."""
        self._api_base: str = api_base.rstrip("/")
        self._auth_token: str = auth_token
        self._chat_model: str = chat_model
        self._ollama_host: str = ollama_host

        # Per-room conversation history.
        self._chat_history: dict[str, list[dict[str, str]]] = {}
        self._chat_timestamps: dict[str, float] = {}

        # Load action definitions.
        self._actions: dict[str, dict[str, Any]] = self._load_actions()

        # Named query handlers — the config's "function pointers."
        # Each takes (api_data, action_config, target_raw, params)
        # and returns a result dict.
        self._handlers: dict[str, Any] = {
            "power_state": self._handle_power_state,
            "sensor_reading": self._handle_sensor_reading,
            "power_summary": self._handle_power_summary,
            "soil_moisture": self._handle_soil_moisture,
        }

        logger.info(
            "Loaded %d action definitions from %s",
            len(self._actions), _ACTIONS_FILE.name,
        )

    def get_action_label(self, action: str) -> str:
        """Get the human-readable label for an action.

        Used by the pipeline to speak "Waiting on the {label}"
        before execution begins.

        Args:
            action: Action name from intent.

        Returns:
            Label string, or empty string if not found.
        """
        cfg: dict[str, Any] | None = self._actions.get(action)
        if cfg is None:
            return ""
        return cfg.get("label", "")

    def get_action_type(self, action: str) -> str:
        """Get the action type (command, query, chat).

        Args:
            action: Action name from intent.

        Returns:
            Type string, or empty string if not found.
        """
        cfg: dict[str, Any] | None = self._actions.get(action)
        if cfg is None:
            return ""
        return cfg.get("type", "")

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_actions(self) -> dict[str, dict[str, Any]]:
        """Load action definitions from YAML.

        Returns:
            Dict mapping action name → config dict.
        """
        if not _ACTIONS_FILE.exists():
            logger.error("Actions file not found: %s", _ACTIONS_FILE)
            return {}

        with open(_ACTIONS_FILE, "r") as f:
            return yaml.safe_load(f) or {}

    # ------------------------------------------------------------------
    # HTTP client
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
    ) -> dict[str, Any]:
        """Make an authenticated request to the GlowUp API.

        Args:
            method: HTTP method.
            path:   URL path.
            body:   Request body (JSON-encoded).

        Returns:
            Parsed JSON response dict.
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

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # Log the response body for debugging.
            error_body: str = ""
            try:
                error_body = exc.read().decode()[:200]
            except Exception:
                pass
            logger.error(
                "API %s %s → %d: %s (body: %s)",
                method, path, exc.code, exc.reason, error_body,
            )
            raise

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def _resolve_target(self, target: str) -> str:
        """Resolve a fuzzy target name to an actual group or device.

        Args:
            target: Raw target string from the LLM.

        Returns:
            Resolved name with ``group:`` prefix for groups.
        """
        if target.lower() == "all":
            return "all"

        try:
            data = self._request("GET", "/api/groups")
            groups: dict = data.get("groups", {})
            target_lower: str = target.lower().strip()

            # Exact match.
            for name in groups:
                if name.lower() == target_lower:
                    logger.info(
                        "Target '%s' resolved to group 'group:%s'",
                        target, name,
                    )
                    return f"group:{name}"

            # Substring match.
            for name in groups:
                if target_lower in name.lower():
                    logger.info(
                        "Fuzzy target '%s' resolved to group 'group:%s'",
                        target, name,
                    )
                    return f"group:{name}"

            # Device list.
            dev_data = self._request("GET", "/api/devices")
            devices: list = dev_data.get("devices", [])
            for dev in devices:
                label: str = dev.get("label", "")
                if target_lower in label.lower():
                    logger.info(
                        "Fuzzy target '%s' resolved to device '%s'",
                        target, label,
                    )
                    return label

        except Exception as exc:
            logger.debug("Target resolution failed: %s", exc)

        return target

    # ------------------------------------------------------------------
    # Generic dispatch
    # ------------------------------------------------------------------

    def execute(
        self, intent: dict[str, Any], room: str,
    ) -> dict[str, Any]:
        """Execute a voice intent via config-driven dispatch.

        Args:
            intent: Parsed intent with ``action``, ``target``, ``params``.
            room:   Originating room name.

        Returns:
            Dict with ``status``, ``confirmation``, ``speak``.
        """
        action: str = intent.get("action", "unknown")
        target_raw: str = intent.get("target", "all")
        target_raw = self._resolve_target(target_raw)
        target_url: str = urllib.parse.quote(target_raw, safe="")
        params: dict[str, Any] = intent.get("params", {})

        # Human-readable target (strip group: prefix).
        display_target: str = (
            target_raw[6:] if target_raw.startswith("group:") else target_raw
        )

        # Look up action config.
        action_cfg: dict[str, Any] | None = self._actions.get(action)

        if action_cfg is None:
            # Route unknown actions to chat as fallback.
            action_cfg = self._actions.get("chat")
            if action_cfg and action_cfg.get("type") == "chat":
                params["message"] = params.get("message", intent.get("target", ""))

        if action_cfg is None:
            return {
                "status": "error",
                "confirmation": f"I don't know how to {action}.",
                "speak": True,
            }

        try:
            action_type: str = action_cfg.get("type", "command")

            if action_type == "command":
                return self._dispatch_command(
                    action_cfg, target_url, display_target, params,
                )
            elif action_type == "query":
                return self._dispatch_query(
                    action_cfg, target_url, target_raw,
                    display_target, params,
                )
            elif action_type == "chat":
                message: str = params.get("message", "")
                return self._exec_chat(message, room)
            else:
                return {
                    "status": "error",
                    "confirmation": f"Unknown action type: {action_type}.",
                    "speak": True,
                }
        except Exception as exc:
            logger.error("Execution failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Something went wrong. Please try again.",
                "speak": True,
            }

    # ------------------------------------------------------------------
    # Command dispatch — POST to API, format confirmation
    # ------------------------------------------------------------------

    def _dispatch_command(
        self,
        cfg: dict[str, Any],
        target_url: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a command action from config.

        Args:
            cfg:            Action config from YAML.
            target_url:     URL-encoded target for API path.
            display_target: Human-readable target for confirmation.
            params:         Intent parameters.

        Returns:
            Result dict.
        """
        # Build API path.
        path: str = cfg["path"].format(target=target_url)

        # Build request body — resolve "param:key" references.
        body: Any = self._resolve_body(cfg.get("body", {}), params)

        logger.debug("Command: %s %s body=%s", cfg.get("method", "POST"), path, body)

        # Make the API call.
        self._request(cfg.get("method", "POST"), path, body)

        # Build confirmation text.
        confirmation: str = self._format_confirm(
            cfg.get("confirm", "Done."),
            display_target,
            params,
            cfg.get("param_map", {}),
        )

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": cfg.get("speak", False),
        }

    def _resolve_body(
        self, body_template: Any, params: dict[str, Any],
    ) -> Any:
        """Resolve ``param:key`` references in a body template.

        Recursively walks the body dict/list and replaces string
        values starting with ``param:`` with the corresponding
        intent parameter value (preserving type).

        Args:
            body_template: Body structure from YAML.
            params:        Intent parameters.

        Returns:
            Resolved body with actual parameter values.
        """
        if isinstance(body_template, dict):
            return {
                k: self._resolve_body(v, params)
                for k, v in body_template.items()
            }
        elif isinstance(body_template, list):
            return [self._resolve_body(v, params) for v in body_template]
        elif isinstance(body_template, str) and body_template.startswith("param:"):
            key: str = body_template[6:]
            return params.get(key, body_template)
        return body_template

    def _format_confirm(
        self,
        template: str,
        display_target: str,
        params: dict[str, Any],
        param_map: dict[str, Any] | None = None,
    ) -> str:
        """Format a confirmation template with target and params.

        Args:
            template:       Format string from YAML.
            display_target: Human-readable target name.
            params:         Intent parameters.
            param_map:      Optional value mappings (e.g., bool → text).

        Returns:
            Formatted confirmation string.
        """
        # Build substitution dict.
        subs: dict[str, Any] = {"target": display_target}
        subs.update(params)

        # Apply param_map — e.g., on=true → on_off="on".
        if param_map:
            for param_name, mapping in param_map.items():
                value = params.get(param_name)
                output_key: str = mapping.get("key", param_name)
                # Convert to string for lookup in the map.
                str_value: str = str(value).lower()
                subs[output_key] = mapping.get(str_value, str(value))

        try:
            return template.format_map(subs)
        except KeyError as exc:
            logger.warning("Confirm template key missing: %s", exc)
            return template

    # ------------------------------------------------------------------
    # Query dispatch — delegates to named handler functions
    # ------------------------------------------------------------------

    def _dispatch_query(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a query action via a named handler.

        Args:
            cfg:            Action config from YAML.
            target_url:     URL-encoded target.
            target_raw:     Raw resolved target (with group: prefix).
            display_target: Human-readable target.
            params:         Intent parameters.

        Returns:
            Result dict from the handler.
        """
        handler_name: str = cfg.get("handler", "")
        handler = self._handlers.get(handler_name)

        if handler is None:
            logger.error("No handler registered for '%s'", handler_name)
            return {
                "status": "error",
                "confirmation": f"Query handler '{handler_name}' not found.",
                "speak": True,
            }

        return handler(cfg, target_url, target_raw, display_target, params)

    # ------------------------------------------------------------------
    # Query handlers — the "function pointers" referenced by config
    # ------------------------------------------------------------------

    def _handle_power_state(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Check if devices/group are on or off."""
        try:
            all_devices: dict[str, Any] = self._request("GET", "/api/devices")
            devices: list[dict[str, Any]] = all_devices.get("devices", [])

            is_group: bool = target_raw.startswith("group:")
            powered: list[bool] = []

            for dev in devices:
                label: str = dev.get("label", "")
                if is_group:
                    if (dev.get("group", "") == display_target
                            or label == display_target):
                        powered.append(dev.get("power", False))
                else:
                    if label == display_target:
                        powered.append(dev.get("power", False))

            if not powered:
                # Fall back to effect status.
                return self._handle_effect_status(
                    cfg, target_url, target_raw, display_target, params,
                )

            if all(powered):
                state = "on"
            elif not any(powered):
                state = "off"
            else:
                on_count: int = sum(powered)
                state = f"partially on, {on_count} of {len(powered)}"

            return {
                "status": "ok",
                "confirmation": f"{display_target} is {state}.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "ok",
                "confirmation": f"I can't check the status of {display_target}.",
                "speak": True,
            }

    def _handle_effect_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Check what effect is playing on a device."""
        try:
            data = self._request(
                "GET", f"/api/devices/{target_url}/status",
            )
            effect: str = data.get("effect", "nothing")
            if effect and effect != "none":
                elapsed: float = data.get("elapsed", 0)
                return {
                    "status": "ok",
                    "confirmation": (
                        f"{display_target} is playing {effect}, "
                        f"running for {elapsed:.0f} seconds."
                    ),
                    "speak": True,
                }
            return {
                "status": "ok",
                "confirmation": f"Nothing is playing on {display_target}.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "ok",
                "confirmation": f"I can't check the status of {display_target}.",
                "speak": True,
            }

    def _handle_sensor_reading(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query BLE sensor readings (temperature, humidity, motion)."""
        try:
            data = self._request("GET", "/api/ble/sensors")
        except Exception as exc:
            logger.error("Sensor API request failed: %s", exc)
            return {
                "status": "ok",
                "confirmation": (
                    f"I can't reach the sensors for {display_target}."
                ),
                "speak": True,
            }
        sensor_type: str = params.get("sensor_type", "temperature")

        for label, readings in data.items():
            if display_target.lower() in label.lower():
                value = readings.get(sensor_type)
                if value is not None:
                    if sensor_type == "temperature":
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
            "confirmation": (
                f"I don't have {sensor_type} data for {display_target}."
            ),
            "speak": True,
        }

    def _handle_power_summary(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query power consumption from Zigbee plugs."""
        try:
            data = self._request(
                "GET", f"/api/power/summary?device={target_url}",
            )
        except Exception:
            data = self._request("GET", "/api/power/summary")

        avg: float = data.get("avg_watts", 0)
        peak: float = data.get("peak_watts", 0)
        kwh: float = data.get("total_kwh", 0)
        cost: float = kwh * _ELECTRICITY_RATE_PER_KWH

        return {
            "status": "ok",
            "confirmation": (
                f"{display_target} averages {avg:.0f} watts, "
                f"peaked at {peak:.0f} watts. "
                f"Total energy {kwh:.1f} kilowatt hours, "
                f"costing about ${cost:.2f}."
            ),
            "speak": True,
        }

    def _handle_soil_moisture(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query soil moisture for a yard zone.

        Zone mapping comes from the action config (actions.yml),
        not hardcoded — add new sensors by editing the YAML.
        """
        zones: dict[str, Any] = cfg.get("zones", {})
        threshold: float = cfg.get("threshold", 40.0)

        # Find the zone — fuzzy match against config keys.
        zone_lower: str = display_target.lower().strip()
        zone_cfg: dict[str, Any] | None = zones.get(zone_lower)

        if zone_cfg is None:
            for key in zones:
                if zone_lower in key or key in zone_lower:
                    zone_cfg = zones[key]
                    zone_lower = key
                    break

        if zone_cfg is None:
            return {
                "status": "error",
                "confirmation": (
                    f"I don't have soil sensors for {display_target}."
                ),
                "speak": True,
            }

        try:
            data: dict[str, Any] = self._request("GET", "/api/home/soil")
            sensors: list[dict[str, Any]] = data.get("sensors", [])
            readings: dict[str, dict[str, Any]] = {
                s.get("name", ""): s for s in sensors if s.get("name")
            }

            parts: list[str] = []
            moisture_values: list[float] = []

            for sensor_def in zone_cfg.get("sensors", []):
                sensor_name: str = sensor_def["name"]
                direction: str = sensor_def["direction"]
                reading = readings.get(sensor_name)
                if reading and "soil_moisture" in reading:
                    pct: float = reading["soil_moisture"]
                    moisture_values.append(pct)
                    parts.append(f"{pct:.0f}% to the {direction}")
                else:
                    parts.append(f"no reading to the {direction}")

            if not moisture_values:
                return {
                    "status": "ok",
                    "confirmation": (
                        f"No soil readings available for {zone_lower}."
                    ),
                    "speak": True,
                }

            moisture_text: str = " and ".join(parts)
            avg_moisture: float = (
                sum(moisture_values) / len(moisture_values)
            )
            suggestion: str = (
                "Watering is suggested."
                if avg_moisture < threshold
                else "Watering is not suggested."
            )

            return {
                "status": "ok",
                "confirmation": (
                    f"The present soil moisture in the {zone_lower} is "
                    f"{moisture_text}. {suggestion}"
                ),
                "speak": True,
            }
        except Exception as exc:
            logger.error("Soil query failed: %s", exc)
            return {
                "status": "error",
                "confirmation": (
                    f"I couldn't check the soil in {display_target}."
                ),
                "speak": True,
            }

    # ------------------------------------------------------------------
    # Chat — freeform Ollama conversation (the one special case)
    # ------------------------------------------------------------------

    def _get_chat_history(self, room: str) -> list[dict[str, str]]:
        """Get conversation history for a room, expiring stale sessions.

        Args:
            room: Room name for history isolation.

        Returns:
            List of message dicts.
        """
        now: float = time.time()
        last_active: float = self._chat_timestamps.get(room, 0.0)

        if now - last_active > _CHAT_HISTORY_TTL_S:
            self._chat_history.pop(room, None)

        self._chat_timestamps[room] = now

        if room not in self._chat_history:
            self._chat_history[room] = []

        return self._chat_history[room]

    def _exec_chat(
        self, message: str, room: str,
    ) -> dict[str, Any]:
        """Handle freeform chat via Ollama with per-room history.

        Args:
            message: The user's spoken message.
            room:    Room name for history isolation.

        Returns:
            Dict with spoken confirmation.
        """
        if not message.strip():
            return {
                "status": "error",
                "confirmation": "I didn't catch that.",
                "speak": True,
            }

        history: list[dict[str, str]] = self._get_chat_history(room)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": _CHAT_SYSTEM_PROMPT},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        payload: dict[str, Any] = {
            "model": self._chat_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 80,
            },
        }

        body: bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._ollama_host}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            t0: float = time.time()
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                reply: str = (
                    result.get("message", {}).get("content", "").strip()
                )
            elapsed: float = time.time() - t0
            logger.info(
                "Chat response in %.2fs (%d chars): '%s'",
                elapsed, len(reply), reply[:80],
            )

            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": reply})

            while len(history) > _CHAT_HISTORY_MAX * 2:
                history.pop(0)
                history.pop(0)

            return {
                "status": "ok",
                "confirmation": reply,
                "speak": True,
            }
        except Exception as exc:
            logger.error("Chat failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Sorry, I couldn't think of a response.",
                "speak": True,
            }
