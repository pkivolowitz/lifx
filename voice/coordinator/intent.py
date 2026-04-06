"""Intent parsing using Ollama (local LLM).

Sends transcribed text to a local Ollama instance and extracts a
structured JSON intent for the GlowUp voice command handler.

The system prompt is dynamically constructed with the current list
of available effects, groups, and devices — refreshed periodically
from the GlowUp API.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import time
import urllib.request
from typing import Any, Optional

from voice import constants as C

logger: logging.Logger = logging.getLogger("glowup.voice.intent")

# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE: str = """You are the voice command parser for GlowUp, a home automation and sensor-fusion platform installed in a home in Mobile, Alabama.

Parse the user's spoken command into a JSON action. Think carefully about what they want.

IMPORTANT: This system has physical sensors (temperature, humidity, motion) inside the home.
- If the user asks about temperature/humidity IN A ROOM or AT A SPECIFIC SENSOR → use query_sensor
- If the user asks about the temperature OUTSIDE, the weather, or mentions a city name → use query_weather

Available actions:
- power: turn devices on or off (params: on=true/false)
- brightness: set brightness level (params: brightness=0-100)
- color: set light color by name (params: color="blue", "red", etc.)
- temperature: set color temperature in Kelvin (params: temperature=2500-9000)
- play_effect: start a lighting effect (params: effect="cylon", "breathe", etc.)
- stop: stop current effect on a device or group
- query_sensor: ask about PHYSICAL sensor readings like temperature, humidity, or motion sensors (params: sensor_type="temperature", "humidity", "motion"). NOT for asking if lights are on.
- query_power: ask about power consumption or electricity cost
- query_status: ask if lights/devices are on or off, or what effect is playing. Use this when someone asks "are the lights on?" or "is the bedroom on?"
- query_soil: ask about soil moisture or whether to water. Use when someone asks "do I need to water?" or "how wet is the yard?"
- query_weather: ask about outdoor temperature, weather, forecast, or conditions outside. Use when someone asks "what's the temperature outside?" or "what's the weather?" or mentions a city name with temperature.
- system_status: ask about overall system health, whether the system is working, or "what is your status?" Use when someone asks "what's your status?", "how are you doing?", "are you working?", "system check", or "status report".
- set_voice: change the speaking voice. Target is the voice name. Use when someone says "switch to Samantha", "use the Daniel voice", "change voice to Karen", etc. (params: voice_name="Samantha")
- repair: restart a specific adapter or subsystem. Target is the adapter name: zigbee, vivint, nvr, printer, or mqtt. Use when someone says "repair NVR", "restart vivint", "fix the printer adapter", etc.
- tell_time: ask for the current time. Use when someone says "what time is it?", "tell me the time", "what's the time?", "got the time?", etc.
- tell_date: ask for today's date or day of the week. Use when someone says "what's today's date?", "what day is it?", "what's the date?", etc.
- query_locks: ask about lock status. Use when someone says "are the doors locked?", "is the front door locked?", "lock status", "check the locks", etc.
- query_doors: ask about door open/closed status. Use when someone says "are any doors open?", "is the back door open?", "check the doors", etc.
- query_alarm: ask about the alarm/security system. Use when someone says "is the alarm on?", "alarm status", "is the house armed?", etc.
- query_batteries: ask about low batteries. Use when someone says "any low batteries?", "battery status", "what needs batteries?", etc.
- query_printer: ask about the printer. Use when someone says "how's the printer?", "printer status", "does the printer need toner?", "any printer alerts?", etc.
- query_schedule: ask about the lighting schedule. Use when someone says "what's scheduled?", "what's on the schedule?", "any schedules tonight?", etc.
- query_uptime: ask about server uptime or how long the system has been running. Use when someone says "how long have you been running?", "uptime", "when did you start?", etc.
- identify_room: identify which room or satellite is responding. Use when someone says "what room am I in?", "which satellite is this?", "where am I?", "what room is this?", etc.
- shopping_add: add an item to the shopping list. Use when someone says "add milk to the shopping list", "put bread on the list", "we need eggs", etc. (params: item="milk")
- shopping_remove: remove an item from the shopping list. Use when someone says "remove milk from the list", "take bread off the list", "never mind the eggs", etc. (params: item="milk")
- shopping_query: check if something is on the shopping list or ask what's on it. Use when someone says "is milk on the list?", "what's on the shopping list?", "read the list", etc. (params: item="milk" or empty for full list)
- shopping_clear: clear the entire shopping list. Use when someone says "clear the shopping list", "empty the list", "start a new list", etc.
- scene: activate a named scene or preset
- chat: general conversation, questions, or anything NOT related to controlling devices or querying sensors (params: message=<the user's full message>)

{capabilities}

Respond with ONLY a JSON object. No explanation, no preamble.

Schema:
{{
  "action": "<action>",
  "target": "<device label, group name, or 'all'>",
  "params": {{ ... action-specific parameters ... }}
}}

If the user is asking a general knowledge question, making conversation, or requesting anything that is NOT about controlling lights/devices/sensors, use action "chat":
{{"action": "chat", "target": "all", "params": {{"message": "<the user's full spoken text>"}}}}

If you cannot determine the intent, use "chat" — do NOT return "unknown".

Examples:
- "turn off the bedroom lights" -> {{"action": "power", "target": "bedroom", "params": {{"on": false}}}}
- "play cylon on the living room" -> {{"action": "play_effect", "target": "living", "params": {{"effect": "cylon"}}}}
- "what's the temperature in the bedroom?" -> {{"action": "query_sensor", "target": "bedroom", "params": {{"sensor_type": "temperature"}}}}
- "how much power is the TV using?" -> {{"action": "query_power", "target": "LRTV", "params": {{}}}}
- "set brightness to 50 percent" -> {{"action": "brightness", "target": "all", "params": {{"brightness": 50}}}}
- "are the bedroom lights on?" -> {{"action": "query_status", "target": "bedroom", "params": {{}}}}
- "is the living room on?" -> {{"action": "query_status", "target": "living", "params": {{}}}}
- "what was my last question?" -> {{"action": "chat", "target": "all", "params": {{"message": "what was my last question?"}}}}
- "do I need to water the backyard?" -> {{"action": "query_soil", "target": "backyard", "params": {{}}}}
- "how wet is the listening room?" -> {{"action": "query_soil", "target": "listening room", "params": {{}}}}
- "what's the temperature outside?" -> {{"action": "query_weather", "target": "all", "params": {{}}}}
- "what's the weather like today?" -> {{"action": "query_weather", "target": "all", "params": {{}}}}
- "what is the temperature in Mobile Alabama?" -> {{"action": "query_weather", "target": "all", "params": {{}}}}
- "what is your status?" -> {{"action": "system_status", "target": "all", "params": {{}}}}
- "are you working?" -> {{"action": "system_status", "target": "all", "params": {{}}}}
- "system check" -> {{"action": "system_status", "target": "all", "params": {{}}}}
- "repair NVR" -> {{"action": "repair", "target": "nvr", "params": {{}}}}
- "restart vivint" -> {{"action": "repair", "target": "vivint", "params": {{}}}}
- "fix the printer" -> {{"action": "repair", "target": "printer", "params": {{}}}}
- "switch to Samantha" -> {{"action": "set_voice", "target": "all", "params": {{"voice_name": "Samantha"}}}}
- "use the Daniel voice" -> {{"action": "set_voice", "target": "all", "params": {{"voice_name": "Daniel"}}}}
- "what time is it?" -> {{"action": "tell_time", "target": "all", "params": {{}}}}
- "tell me the time" -> {{"action": "tell_time", "target": "all", "params": {{}}}}
- "what's today's date?" -> {{"action": "tell_date", "target": "all", "params": {{}}}}
- "what day is it?" -> {{"action": "tell_date", "target": "all", "params": {{}}}}
- "are the doors locked?" -> {{"action": "query_locks", "target": "all", "params": {{}}}}
- "is the front door locked?" -> {{"action": "query_locks", "target": "front door", "params": {{}}}}
- "are any doors open?" -> {{"action": "query_doors", "target": "all", "params": {{}}}}
- "is the back door open?" -> {{"action": "query_doors", "target": "back door", "params": {{}}}}
- "is the alarm on?" -> {{"action": "query_alarm", "target": "all", "params": {{}}}}
- "any low batteries?" -> {{"action": "query_batteries", "target": "all", "params": {{}}}}
- "how's the printer?" -> {{"action": "query_printer", "target": "all", "params": {{}}}}
- "what's on the schedule?" -> {{"action": "query_schedule", "target": "all", "params": {{}}}}
- "how long have you been running?" -> {{"action": "query_uptime", "target": "all", "params": {{}}}}
- "what room am I in?" -> {{"action": "identify_room", "target": "all", "params": {{}}}}
- "which satellite is this?" -> {{"action": "identify_room", "target": "all", "params": {{}}}}
- "add milk to the shopping list" -> {{"action": "shopping_add", "target": "all", "params": {{"item": "milk"}}}}
- "we need paper towels" -> {{"action": "shopping_add", "target": "all", "params": {{"item": "paper towels"}}}}
- "put bread on the list" -> {{"action": "shopping_add", "target": "all", "params": {{"item": "bread"}}}}
- "remove milk from the list" -> {{"action": "shopping_remove", "target": "all", "params": {{"item": "milk"}}}}
- "is milk on the list?" -> {{"action": "shopping_query", "target": "all", "params": {{"item": "milk"}}}}
- "what's on the shopping list?" -> {{"action": "shopping_query", "target": "all", "params": {{}}}}
- "clear the shopping list" -> {{"action": "shopping_clear", "target": "all", "params": {{}}}}
- "tell me about the battle of Gettysburg" -> {{"action": "chat", "target": "all", "params": {{"message": "tell me about the battle of Gettysburg"}}}}
"""


class IntentParser:
    """Parse voice transcriptions into structured intents via Ollama.

    Args:
        model:        Ollama model name (e.g., ``llama3.2:3b``).
        ollama_host:  Ollama API base URL.
        timeout:      Request timeout in seconds.
        max_retries:  Retries on invalid JSON response.
    """

    def __init__(
        self,
        model: str = "llama3.2:3b",
        ollama_host: str = "http://localhost:11434",
        timeout: float = C.INTENT_TIMEOUT_S,
        max_retries: int = C.INTENT_MAX_RETRIES,
    ) -> None:
        """Initialize the intent parser."""
        self._model: str = model
        self._ollama_host: str = ollama_host
        self._timeout: float = timeout
        self._max_retries: int = max_retries
        self._capabilities_text: str = ""
        self._capabilities_last_refresh: float = 0.0

    def refresh_capabilities(
        self, api_base: str, auth_token: str,
    ) -> None:
        """Fetch available effects, groups, devices from GlowUp API.

        Updates the system prompt with current capabilities so the
        LLM knows what targets and effects are available.

        Args:
            api_base:   GlowUp server URL (e.g., ``http://localhost:8420``).
            auth_token: Bearer token for GlowUp API.
        """
        parts: list[str] = []
        headers: dict[str, str] = {
            "Authorization": f"Bearer {auth_token}",
        }

        for endpoint, label in [
            ("/api/effects", "Available effects"),
            ("/api/groups", "Available groups"),
            ("/api/devices", "Available devices"),
        ]:
            try:
                req = urllib.request.Request(
                    f"{api_base}{endpoint}", headers=headers,
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())

                if endpoint == "/api/effects":
                    names = [e.get("name", "") for e in data if isinstance(e, dict)]
                    if names:
                        parts.append(f"{label}: {', '.join(names)}")
                elif endpoint == "/api/groups":
                    if isinstance(data, dict):
                        parts.append(f"{label}: {', '.join(data.keys())}")
                elif endpoint == "/api/devices":
                    labels = [
                        d.get("label", d.get("ip", ""))
                        for d in data if isinstance(d, dict)
                    ]
                    if labels:
                        parts.append(f"{label}: {', '.join(labels)}")

            except Exception as exc:
                logger.debug("Failed to fetch %s: %s", endpoint, exc)

        self._capabilities_text = "\n".join(parts)
        self._capabilities_last_refresh = time.time()
        logger.info(
            "Refreshed capabilities: %d chars", len(self._capabilities_text),
        )

    def _build_system_prompt(self) -> str:
        """Build the full system prompt with current capabilities."""
        return _SYSTEM_PROMPT_TEMPLATE.format(
            capabilities=self._capabilities_text or "No capability data available yet.",
        )

    def _call_ollama(self, text: str) -> Optional[dict[str, Any]]:
        """Send text to Ollama and parse the JSON response.

        Args:
            text: Transcribed voice command.

        Returns:
            Parsed intent dict, or None on failure.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._build_system_prompt()},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "options": {
                "temperature": 0.1,  # Low temperature for deterministic output.
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
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read())
                content: str = result.get("message", {}).get("content", "").strip()

                # Strip markdown code fences if present.
                if content.startswith("```"):
                    lines = content.split("\n")
                    # Remove first and last line (``` markers).
                    content = "\n".join(lines[1:-1]).strip()

                return json.loads(content)

        except json.JSONDecodeError as exc:
            logger.warning("Ollama returned invalid JSON: %s", exc)
            return None
        except Exception as exc:
            logger.error("Ollama request failed: %s", exc)
            return None

    def parse(self, text: str) -> dict[str, Any]:
        """Parse transcribed text into a structured intent.

        Calls Ollama with the system prompt and user text.
        Retries once on invalid JSON.

        Args:
            text: Transcribed voice command.

        Returns:
            Intent dict with ``action``, ``target``, ``params``.
            Returns ``{"action": "unknown"}`` on failure.
        """
        if not text.strip():
            return {"action": "unknown"}

        t0: float = time.monotonic()

        for attempt in range(1 + self._max_retries):
            result = self._call_ollama(text)
            if result is not None and isinstance(result, dict):
                elapsed: float = time.monotonic() - t0
                logger.info(
                    "Intent parsed in %.2fs (attempt %d): %s",
                    elapsed, attempt + 1, result,
                )
                return result

            if attempt < self._max_retries:
                logger.info("Retrying intent parse (attempt %d)...", attempt + 2)

        logger.warning("Intent parsing failed after %d attempts", 1 + self._max_retries)
        return {"action": "unknown"}

    def should_refresh(self) -> bool:
        """Check if capabilities should be refreshed.

        Returns:
            True if the refresh interval has elapsed.
        """
        return (
            time.time() - self._capabilities_last_refresh
            > C.CAPABILITIES_REFRESH_S
        )


class MockIntentParser:
    """Mock intent parser for testing without Ollama.

    Returns a fixed intent or prompts stdin.

    Args:
        intent: Fixed intent dict to return.  If None, prompts
                stdin for JSON input.
    """

    def __init__(self, intent: Optional[dict[str, Any]] = None) -> None:
        """Initialize the mock parser."""
        self._intent: Optional[dict[str, Any]] = intent

    def parse(self, text: str) -> dict[str, Any]:
        """Return mock intent.

        Args:
            text: Ignored unless prompting.

        Returns:
            The pre-set intent or user input from stdin.
        """
        if self._intent is not None:
            return self._intent

        print(f"[MOCK INTENT] Heard: '{text}'")
        try:
            raw = input("[MOCK INTENT] Enter JSON intent: ").strip()
            return json.loads(raw)
        except (EOFError, json.JSONDecodeError):
            return {"action": "unknown"}

    def refresh_capabilities(self, api_base: str, auth_token: str) -> None:
        """No-op for mock."""

    def should_refresh(self) -> bool:
        """Never refresh for mock."""
        return False
