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
- If the user asks about temperature/humidity IN A NAMED ROOM or AT A SPECIFIC SENSOR → use query_sensor
- If the user asks about the temperature OUTSIDE, the weather, or mentions a city name → use query_weather
- If the user asks about temperature/humidity/wind/rain WITHOUT naming a room or sensor → default to outdoor: query_weather (never invent a room name). Bare "what's the temperature?" means outside.
- If the user asks about pollen, allergens, air quality, PM2.5, ozone, or UV → use query_air_quality
- If the user asks about the FORECAST, tomorrow, tonight, "will it rain", or a future condition → use query_forecast

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
- query_weather: ask about CURRENT outdoor temperature, humidity, wind, or conditions. Use when someone asks "what's the temperature outside?", "what's the weather?", "how windy is it?", "how humid is it?", or a bare "what's the temperature?" / "what's the humidity?" without a room. Optional params.aspect narrows the answer: "temperature", "humidity", "wind", "condition", "feels_like", or "all" (default). Use "feels_like" when the user asks how hot/cold it feels, heat index, wind chill.
- query_forecast: ask about FUTURE weather — today's high, tonight, tomorrow, chance of rain, will it rain. Params: when="today"|"tonight"|"tomorrow" (default "today"); optional aspect="rain"|"high"|"low"|"all" (default "all").
- query_air_quality: ask about outdoor air quality, pollen, allergens, PM2.5, ozone, or UV index. Params: optional aspect="pollen"|"pm25"|"ozone"|"uv"|"aqi"|"all" (default "all"); optional species="grass"|"ragweed"|"birch"|"alder"|"mugwort"|"olive" when pollen aspect and a specific plant is named.
- system_status: ask about overall system health, whether the system is working, or "what is your status?" Use when someone asks "what's your status?", "how are you doing?", "are you working?", "system check", or "status report".
- set_voice: change the speaking voice. Target is the voice name. Use when someone says "switch to Samantha", "use the Daniel voice", "change voice to Karen", etc. (params: voice_name="Samantha")
- repair: restart a specific adapter or subsystem. Target is the adapter name: zigbee, vivint, nvr, printer, or mqtt. Use when someone says "repair NVR", "restart vivint", "fix the printer adapter", etc.
- tell_time: ask for the current time. Use when someone says "what time is it?", "tell me the time", "what's the time?", "got the time?", etc.
- tell_date: ask for today's date or day of the week. Use when someone says "what's today's date?", "what day is it?", "what's the date?", etc.
- query_locks: ask about lock status. Use when someone says "are the doors locked?", "is the front door locked?", "lock status", "check the locks", etc.
- query_doors: ask about door open/closed status. Use when someone says "are any doors open?", "is the back door open?", "check the doors", etc.
- query_alarm: ask about the alarm/security system. Use when someone says "is the alarm on?", "alarm status", "is the house armed?", etc.
- query_batteries: ask about low batteries, chirping, or beeping sensors. Use when someone says "any low batteries?", "battery status", "what needs batteries?", "are any sensors chirping?", "which sensor is beeping?", "what's that chirping?", "which smoke detector is dying?", etc. This also covers tampered/bypassed sensors because those audibly beep too.
- query_printer: ask about the printer. Use when someone says "how's the printer?", "printer status", "does the printer need toner?", "any printer alerts?", etc.
- query_schedule: ask about the lighting schedule. Use when someone says "what's scheduled?", "what's on the schedule?", "any schedules tonight?", etc.
- query_uptime: ask about server uptime or how long the system has been running. Use when someone says "how long have you been running?", "uptime", "when did you start?", etc.
- identify_room: identify which room or satellite is responding. Use when someone says "what room am I in?", "which satellite is this?", "where am I?", "what room is this?", etc.
- shopping_add: add an item to the shopping list. Use when someone says "add milk to the shopping list", "put bread on the list", "we need eggs", etc. (params: item="milk")
- shopping_remove: remove an item from the shopping list. Use when someone says "remove milk from the list", "take bread off the list", "never mind the eggs", etc. (params: item="milk")
- shopping_query: check if something is on the shopping list or ask what's on it. Use when someone says "is milk on the list?", "what's on the shopping list?", "read the list", etc. (params: item="milk" or empty for full list)
- shopping_clear: clear the entire shopping list. Use when someone says "clear the shopping list", "empty the list", "start a new list", etc.
- flush: cancel all in-flight requests and reset. Use when someone says "flush it", "flush", "cancel", "never mind", "stop listening", "forget it", etc.
- commands: list what the system can do at a high level. Use when someone says "commands", "list commands", "what can you do?", "what do you do?", etc.
- help_lights: explain available lighting commands. Use when someone says "help lights", "commands lights", "what light commands are there?", etc.
- help_shopping: explain shopping list commands. Use when someone says "help shopping", "commands shopping", etc.
- help_security: explain home security commands. Use when someone says "help security", "commands security", etc.
- help_system: explain system commands. Use when someone says "help system", "commands system", etc.
- help_sensors: explain sensor commands. Use when someone says "help sensors", "commands sensors", etc.
- list_sensors: enumerate available sensors. Use when someone says "list sensors", "what sensors do I have?", "what sensors are there?", etc.
- list_groups: enumerate light groups. Use when someone says "list groups", "what groups do I have?", "what are my groups?", etc.
- list_doors: enumerate door sensors. Use when someone says "list doors", "what doors do you know about?", etc.
- list_locks: enumerate locks. Use when someone says "list locks", "what locks do you know about?", etc.
- enable_voice_gate: temporarily enable listening on a gated satellite (the porch/doorbell). Target is the gate slug ("doorbell" for the front porch). Params MUST include a duration in seconds. If the user does not say a duration, still emit this action with params.duration_seconds = 0 so the system can ask. Use when someone says "enable the porch for two hours", "open the doorbell for thirty minutes", "turn on the porch mic for ten minutes", etc. Convert spoken durations to integer seconds: "two hours" -> 7200, "thirty minutes" -> 1800, "one hour" -> 3600, "ninety seconds" -> 90.
- disable_voice_gate: immediately disable listening on a gated satellite. Target is the gate slug ("doorbell"). Use when someone says "disable the porch", "close the doorbell", "turn off the porch mic", "shut the porch", etc.
- scene: activate a named scene or preset
- joke: tell the user a joke. Use when the user asks for a joke, says "make me laugh", "got a joke?", "tell me something funny", "do you know any jokes", or asks for a specific style/topic of joke ("tell me a dad joke", "tell me a science joke", "knock knock", "another joke"). Optional params: style (a short label like "knock knock", "dad", "one-liner", "anti-joke", "pun", "limerick") and topic (a short noun like "science", "food", "music"). Use empty params {{}} when no style or topic is named. DO NOT generate or include the joke itself in params — generating the joke is the assistant's job, never the parser's. NEVER put a punchline, setup, or any joke text into a params field.
- play_asset: play a pre-recorded audio file (an "easter egg"). Use ONLY when the user asks to sing or play a SPECIFIC named song that is in the small easter-egg catalogue: "Daisy" / "Daisy Bell" / "the song from 2001". Always pass the user's full spoken text in params.message — the executor will match it against the trigger map. Do NOT use this for general singing requests, music playback, or songs that aren't in the catalogue; for those, use chat (which will politely decline).
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

Target disambiguation rules:
- Each plug listed above shows its "type words" in parentheses, e.g.
  "Main Bedroom TV Switch (tv, television)" means that plug controls
  a tv.  When the user's noun matches a plug's type words, target
  that plug — even if the user says "lights" or "lamp", a plug whose
  type words include "light"/"lamp" IS a light.
- "lights" with no matching plug type words and a matching room group
  refers to the bulb group for that room.
- An unqualified room name ("main bedroom", "the living room") with
  no plug type-word match refers to the group for that room.
- A phrase containing "switch", "plug", "outlet", or a literal plug
  label always targets that plug.

Examples:
- "turn off the bedroom lights" -> {{"action": "power", "target": "bedroom", "params": {{"on": false}}}}
- "play cylon on the living room" -> {{"action": "play_effect", "target": "living", "params": {{"effect": "cylon"}}}}
- "what's the temperature in the bedroom?" -> {{"action": "query_sensor", "target": "bedroom", "params": {{"sensor_type": "temperature"}}}}
- "how much power is the TV using?" -> {{"action": "query_power", "target": "LRTV", "params": {{}}}}
- "set brightness to 50 percent" -> {{"action": "brightness", "target": "all", "params": {{"brightness": 50}}}}
- "are the bedroom lights on?" -> {{"action": "query_status", "target": "bedroom", "params": {{}}}}
- "is the living room on?" -> {{"action": "query_status", "target": "living", "params": {{}}}}
- "turn on the ML switch" -> {{"action": "power", "target": "ML Switch", "params": {{"on": true}}}}
- "turn off the main bedroom TV switch" -> {{"action": "power", "target": "Main Bedroom TV Switch", "params": {{"on": false}}}}
- "turn off the lights in the main bedroom" -> {{"action": "power", "target": "Main Bedroom", "params": {{"on": false}}}}
- "turn on the lights in the main bedroom" -> {{"action": "power", "target": "Main Bedroom", "params": {{"on": true}}}}
- "turn off the lights in the blue bedroom" -> {{"action": "power", "target": "Blue Bedroom", "params": {{"on": false}}}}
- "turn off everything in the main bedroom" -> {{"action": "power", "target": "Main Bedroom", "params": {{"on": false}}}}
- "is the backyard IR switch on?" -> {{"action": "query_status", "target": "Backyard IR Switch", "params": {{}}}}
- "is the living room TV switch on?" -> {{"action": "query_status", "target": "Living Room TV Switch", "params": {{}}}}
- "what was my last question?" -> {{"action": "chat", "target": "all", "params": {{"message": "what was my last question?"}}}}
- "do I need to water the backyard?" -> {{"action": "query_soil", "target": "backyard", "params": {{}}}}
- "how wet is the listening room?" -> {{"action": "query_soil", "target": "listening room", "params": {{}}}}
- "what's the temperature outside?" -> {{"action": "query_weather", "target": "all", "params": {{"aspect": "temperature"}}}}
- "what is the temperature?" -> {{"action": "query_weather", "target": "all", "params": {{"aspect": "temperature"}}}}
- "what's the humidity?" -> {{"action": "query_weather", "target": "all", "params": {{"aspect": "humidity"}}}}
- "how windy is it?" -> {{"action": "query_weather", "target": "all", "params": {{"aspect": "wind"}}}}
- "what's the weather like today?" -> {{"action": "query_weather", "target": "all", "params": {{}}}}
- "what does it feel like outside?" -> {{"action": "query_weather", "target": "all", "params": {{"aspect": "feels_like"}}}}
- "what is the temperature in Mobile Alabama?" -> {{"action": "query_weather", "target": "all", "params": {{"aspect": "temperature"}}}}
- "what's the forecast?" -> {{"action": "query_forecast", "target": "all", "params": {{"when": "today"}}}}
- "what's the forecast this morning?" -> {{"action": "query_forecast", "target": "all", "params": {{"when": "today"}}}}
- "will it rain today?" -> {{"action": "query_forecast", "target": "all", "params": {{"when": "today", "aspect": "rain"}}}}
- "what's tonight's forecast?" -> {{"action": "query_forecast", "target": "all", "params": {{"when": "tonight"}}}}
- "will it rain tonight?" -> {{"action": "query_forecast", "target": "all", "params": {{"when": "tonight", "aspect": "rain"}}}}
- "what's tomorrow's high?" -> {{"action": "query_forecast", "target": "all", "params": {{"when": "tomorrow", "aspect": "high"}}}}
- "what's the forecast for tomorrow?" -> {{"action": "query_forecast", "target": "all", "params": {{"when": "tomorrow"}}}}
- "what's the pollen?" -> {{"action": "query_air_quality", "target": "all", "params": {{"aspect": "pollen"}}}}
- "is ragweed bad today?" -> {{"action": "query_air_quality", "target": "all", "params": {{"aspect": "pollen", "species": "ragweed"}}}}
- "how's the grass pollen?" -> {{"action": "query_air_quality", "target": "all", "params": {{"aspect": "pollen", "species": "grass"}}}}
- "what's the air quality?" -> {{"action": "query_air_quality", "target": "all", "params": {{"aspect": "aqi"}}}}
- "how's the air outside?" -> {{"action": "query_air_quality", "target": "all", "params": {{}}}}
- "what's the UV index?" -> {{"action": "query_air_quality", "target": "all", "params": {{"aspect": "uv"}}}}
- "any allergens today?" -> {{"action": "query_air_quality", "target": "all", "params": {{"aspect": "pollen"}}}}
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
- "are any sensors chirping?" -> {{"action": "query_batteries", "target": "all", "params": {{}}}}
- "which smoke detector is chirping?" -> {{"action": "query_batteries", "target": "all", "params": {{}}}}
- "what's that beeping?" -> {{"action": "query_batteries", "target": "all", "params": {{}}}}
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
- "flush it" -> {{"action": "flush", "target": "all", "params": {{}}}}
- "flush" -> {{"action": "flush", "target": "all", "params": {{}}}}
- "never mind" -> {{"action": "flush", "target": "all", "params": {{}}}}
- "cancel" -> {{"action": "flush", "target": "all", "params": {{}}}}
- "forget it" -> {{"action": "flush", "target": "all", "params": {{}}}}
- "commands" -> {{"action": "commands", "target": "all", "params": {{}}}}
- "list commands" -> {{"action": "commands", "target": "all", "params": {{}}}}
- "what can you do?" -> {{"action": "commands", "target": "all", "params": {{}}}}
- "help lights" -> {{"action": "help_lights", "target": "all", "params": {{}}}}
- "commands lights" -> {{"action": "help_lights", "target": "all", "params": {{}}}}
- "help shopping" -> {{"action": "help_shopping", "target": "all", "params": {{}}}}
- "help security" -> {{"action": "help_security", "target": "all", "params": {{}}}}
- "help system" -> {{"action": "help_system", "target": "all", "params": {{}}}}
- "help sensors" -> {{"action": "help_sensors", "target": "all", "params": {{}}}}
- "list sensors" -> {{"action": "list_sensors", "target": "all", "params": {{}}}}
- "what sensors do I have?" -> {{"action": "list_sensors", "target": "all", "params": {{}}}}
- "list groups" -> {{"action": "list_groups", "target": "all", "params": {{}}}}
- "what groups do I have?" -> {{"action": "list_groups", "target": "all", "params": {{}}}}
- "list doors" -> {{"action": "list_doors", "target": "all", "params": {{}}}}
- "list locks" -> {{"action": "list_locks", "target": "all", "params": {{}}}}
- "enable the porch for two hours" -> {{"action": "enable_voice_gate", "target": "doorbell", "params": {{"duration_seconds": 7200}}}}
- "open the doorbell for thirty minutes" -> {{"action": "enable_voice_gate", "target": "doorbell", "params": {{"duration_seconds": 1800}}}}
- "turn on the porch mic for ten minutes" -> {{"action": "enable_voice_gate", "target": "doorbell", "params": {{"duration_seconds": 600}}}}
- "enable the porch" -> {{"action": "enable_voice_gate", "target": "doorbell", "params": {{"duration_seconds": 0}}}}
- "disable the porch" -> {{"action": "disable_voice_gate", "target": "doorbell", "params": {{}}}}
- "close the doorbell" -> {{"action": "disable_voice_gate", "target": "doorbell", "params": {{}}}}
- "tell me about the battle of Gettysburg" -> {{"action": "chat", "target": "all", "params": {{"message": "tell me about the battle of Gettysburg"}}}}
- "tell me a joke" -> {{"action": "joke", "target": "all", "params": {{}}}}
- "another joke" -> {{"action": "joke", "target": "all", "params": {{}}}}
- "got a joke?" -> {{"action": "joke", "target": "all", "params": {{}}}}
- "make me laugh" -> {{"action": "joke", "target": "all", "params": {{}}}}
- "tell me something funny" -> {{"action": "joke", "target": "all", "params": {{}}}}
- "do you know any jokes?" -> {{"action": "joke", "target": "all", "params": {{}}}}
- "tell me a dad joke" -> {{"action": "joke", "target": "all", "params": {{"style": "dad"}}}}
- "tell me a knock knock joke" -> {{"action": "joke", "target": "all", "params": {{"style": "knock knock"}}}}
- "tell me a one-liner" -> {{"action": "joke", "target": "all", "params": {{"style": "one-liner"}}}}
- "tell me an anti-joke" -> {{"action": "joke", "target": "all", "params": {{"style": "anti-joke"}}}}
- "tell me a science joke" -> {{"action": "joke", "target": "all", "params": {{"topic": "science"}}}}
- "tell me a joke about food" -> {{"action": "joke", "target": "all", "params": {{"topic": "food"}}}}
- "tell me a dad joke about cars" -> {{"action": "joke", "target": "all", "params": {{"style": "dad", "topic": "cars"}}}}
- "sing daisy" -> {{"action": "play_asset", "target": "all", "params": {{"message": "sing daisy"}}}}
- "sing daisy bell" -> {{"action": "play_asset", "target": "all", "params": {{"message": "sing daisy bell"}}}}
- "sing me daisy" -> {{"action": "play_asset", "target": "all", "params": {{"message": "sing me daisy"}}}}
- "sing daisy from 2001" -> {{"action": "play_asset", "target": "all", "params": {{"message": "sing daisy from 2001"}}}}
- "sing the song from 2001" -> {{"action": "play_asset", "target": "all", "params": {{"message": "sing the song from 2001"}}}}
- "sing the song hal sang" -> {{"action": "play_asset", "target": "all", "params": {{"message": "sing the song hal sang"}}}}
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
        # Plug friendly names — load from actions.yml so the intent
        # parser knows "Main Bedroom TV Switch" et al. are valid targets.
        # Reading the YAML here keeps the coordinator from having to
        # thread data in from the executor.
        try:
            import yaml as _yaml  # type: ignore[import-not-found]
            from pathlib import Path as _Path
            actions_file = (
                _Path(__file__).parent / "actions.yml"
            )
            if actions_file.exists():
                with open(actions_file) as f:
                    _actions = _yaml.safe_load(f) or {}
                plugs_dict = _actions.get("plugs") or {}
                plug_strs: list[str] = []
                for friendly, val in plugs_dict.items():
                    # Each plug entry is either a dict
                    # {zigbee, type_words} or (legacy) a bare string
                    # treated as zigbee with no type_words.  type_words
                    # render in parens after the friendly name so the
                    # LLM can match user nouns ("tv", "lamp") to the
                    # right plug.  Bare-string plugs render alone and
                    # are reachable only by their friendly name.
                    if isinstance(val, dict):
                        words = [
                            str(w) for w in (val.get("type_words") or [])
                        ]
                    else:
                        words = []
                    if words:
                        plug_strs.append(
                            f"{friendly} ({', '.join(words)})"
                        )
                    else:
                        plug_strs.append(friendly)
                if plug_strs:
                    parts.append(
                        "Available switches (Zigbee plugs): "
                        + ", ".join(plug_strs)
                    )
        except Exception as exc:
            logger.debug("Plug capability load failed: %s", exc)

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
                    # API shape: {"effects": {name: {...}, ...}}
                    effects = data.get("effects", {}) if isinstance(data, dict) else {}
                    names = list(effects.keys()) if isinstance(effects, dict) else []
                    if names:
                        parts.append(f"{label}: {', '.join(names)}")
                elif endpoint == "/api/groups":
                    # API shape: {"groups": {name: [...], ...}}
                    groups = data.get("groups", {}) if isinstance(data, dict) else {}
                    if isinstance(groups, dict) and groups:
                        parts.append(f"{label}: {', '.join(groups.keys())}")
                elif endpoint == "/api/devices":
                    # API shape: {"devices": [{"label": ..., "ip": ...}, ...]}
                    devs = data.get("devices", []) if isinstance(data, dict) else []
                    labels = [
                        d.get("label", d.get("ip", ""))
                        for d in devs if isinstance(d, dict)
                    ]
                    labels = [s for s in labels if s]
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
