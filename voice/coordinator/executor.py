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

__version__ = "2.1"

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
    _HAS_YAML: bool = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    _HAS_YAML = False

from glowup_site import site, SiteConfigError
from voice import constants as C
from voice.coordinator.weather_sources import (
    AirQuality,
    CurrentConditions,
    ForecastPeriod,
    NWSSource,
    OpenMeteoAirQuality,
    OpenMeteoSource,
    WeatherClient,
    WeatherSourceError,
)
from zigbee_service.client import ZigbeeControlClient

logger: logging.Logger = logging.getLogger("glowup.voice.executor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mobile, AL residential electricity rate ($/kWh).
_ELECTRICITY_RATE_PER_KWH: float = 0.171

# Mobile, AL coordinates — shared by weather, forecast, and air quality.
# Matches the /home dashboard's location.
_WEATHER_LAT: float = 30.69
_WEATHER_LON: float = -88.04

# Feels-like vs. actual-temperature spread at which "feels like"
# becomes worth mentioning on the default (all-aspects) weather
# response.  A 5°F delta is where heat-index/wind-chill noticeably
# changes the outdoor experience.
_FEELS_LIKE_SPREAD_F: float = 5.0

# Pollen grains/m^3 thresholds (Open-Meteo units) used to translate
# numeric counts into spoken "low / moderate / high / very high"
# categories.  Values reflect common allergy-forecast conventions
# rather than a single canonical source.
_POLLEN_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.1, "none"),
    (20.0, "low"),
    (80.0, "moderate"),
    (200.0, "high"),
    (float("inf"), "very high"),
)

# US AQI → plain English ranges per EPA's AQI color bands.
_AQI_BANDS: tuple[tuple[float, str], ...] = (
    (50.0, "good"),
    (100.0, "moderate"),
    (150.0, "unhealthy for sensitive groups"),
    (200.0, "unhealthy"),
    (300.0, "very unhealthy"),
    (float("inf"), "hazardous"),
)

# UV index → plain English per WHO bands.
_UV_BANDS: tuple[tuple[float, str], ...] = (
    (3.0, "low"),
    (6.0, "moderate"),
    (8.0, "high"),
    (11.0, "very high"),
    (float("inf"), "extreme"),
)

# Human-facing pollen species labels — Open-Meteo keys strip the
# ``_pollen`` suffix for spoken output.
_POLLEN_LABELS: dict[str, str] = {
    "alder_pollen": "alder",
    "birch_pollen": "birch",
    "grass_pollen": "grass",
    "mugwort_pollen": "mugwort",
    "olive_pollen": "olive",
    "ragweed_pollen": "ragweed",
}

# Default URL for the glowup-zigbee-service HTTP API.  Voice plug
# commands (power on/off, is-on queries) hit it directly from the
# coordinator — not via the hub — so latency stays low.  The household-
# specific URL lives in /etc/glowup/site.json under "zigbee_service_url"
# (rendered from glowup-infra/fleet/inventory.yaml on each deploy);
# this generic source file just looks it up.  An explicit
# coordinator_config.json -> zigbee.service_url still wins over the
# site value, for one-off overrides during dev/test.
_ZIGBEE_SERVICE_URL_FROM_SITE: Optional[str] = site.get("zigbee_service_url")

# Timeout for plug HTTP calls — plugs actuate in <500 ms over Zigbee;
# anything longer means the radio is wedged, bail.
_PLUG_HTTP_TIMEOUT: float = 5.0

# Repair hints for adapters that need special onboarding beyond a restart.
_REPAIR_HINTS: dict[str, str] = {
    "vivint": "Run vivint setup in the adapters directory to re-authenticate.",
    "nvr": "Try saying repair NVR, or reboot the NVR hardware.",
}

# ---------------------------------------------------------------------------
# Chat constants
# ---------------------------------------------------------------------------

# Maximum exchanges (user + assistant pairs) kept per room.
_CHAT_HISTORY_MAX: int = 10

# History expires after 30 minutes of inactivity.
_CHAT_HISTORY_TTL_S: float = 1800.0

# Default sampling temperature for freeform chat.
_CHAT_TEMPERATURE: float = 0.7

# num_predict cap for chat-style Ollama calls — ~90 words, enough
# for 1-3 short spoken sentences.
_CHAT_NUM_PREDICT: int = 120

# /api/chat call timeout.  Local Ollama on the hub typically
# responds in 0.5-3s for gemma3:27b; 30s is generous headroom.
_OLLAMA_TIMEOUT_S: float = 30.0

# System prompt for freeform chat — concise spoken responses.
_CHAT_SYSTEM_PROMPT: str = (
    "You are GlowUp, a home assistant built on the Gemma 3 language "
    "model running locally via Ollama. Your responses "
    "are spoken aloud via text-to-speech. Be straightforward, polite, "
    "and factual. Every response MUST be 1-2 sentences maximum. "
    "You have a warm personality."
)

# System prompt for the joke action.  The category enumeration is
# load-bearing: at default chat temperature, gemma3:27b converges on
# the same handful of jokes when asked cold (the "atoms make up
# everything" attractor and a few others).  Listing many styles +
# many subjects + an explicit avoidance list of the worst offenders
# pushes the sampler off the deterministic favorites and into the
# long tail.  Per-room chat history (shared with the chat action)
# provides the within-session "don't repeat" signal naturally.
_JOKE_SYSTEM_PROMPT: str = (
    "You are GlowUp, a home assistant. The user has asked for a "
    "joke. Reply with exactly one joke and nothing else — no "
    "preamble like \"Sure!\" or \"Here's one:\", no commentary "
    "after. The reply is spoken aloud over a speaker, so keep it "
    "to 1-3 short sentences and make it land cleanly when read. "
    "Deliver it casually, as a friend would tell it.\n\n"
    "Pick uniformly at random from a wide range of comedic styles: "
    "one-liner, observational, pun, anti-joke, absurdist, dad joke, "
    "dry British, self-deprecating, paraprosdokian, hyperbole, "
    "malaphor, deadpan, shaggy-dog (kept short), Borscht-belt, "
    "callback, surreal, bar-walks-into, doctor-doctor, Tom Swifty, "
    "Spoonerism, Wellerism, news-headline twist, Bayesian-prior "
    "subversion. Subjects span science, language, food, work, "
    "animals, weather, philosophy, technology, history, sports, "
    "music, math, geography, domestic life, travel, money. "
    "Avoid knock-knock unless the user explicitly asks for one. "
    "Do not default to the most common LLM joke attractors — "
    "in particular, never use: \"why don't scientists trust "
    "atoms\", \"I'm reading a book about anti-gravity\", "
    "\"parallel lines have so much in common\", \"I told my wife "
    "she was drawing her eyebrows too high\", \"why don't "
    "skeletons fight each other\", \"what do you call a fish "
    "with no eyes\". If the conversation history shows a joke you "
    "already told, pick a different style and a different subject."
)

# Higher sampling temperature on joke calls.  At chat-default 0.7,
# the model collapses to its favorites; at 1.1 with the category
# list above, it samples broadly across the long tail without
# losing coherence.
_JOKE_TEMPERATURE: float = 1.1

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
        chat_model: str = "gemma3:27b",
        ollama_host: str = "http://localhost:11434",
        zigbee_service_url: Optional[str] = None,
    ) -> None:
        """Initialize the executor.

        ``zigbee_service_url`` resolution order: explicit argument →
        ``site.json`` ``zigbee_service_url`` key → fail.  The voice
        coordinator's plug commands cannot work without this URL, so
        a missing value is fatal at construction time rather than at
        first plug call.
        """
        self._api_base: str = api_base.rstrip("/")
        self._auth_token: str = auth_token
        self._chat_model: str = chat_model
        self._ollama_host: str = ollama_host
        resolved_zigbee_url: Optional[str] = (
            zigbee_service_url or _ZIGBEE_SERVICE_URL_FROM_SITE
        )
        if not resolved_zigbee_url:
            raise SiteConfigError(
                "voice coordinator requires zigbee_service_url; set "
                "'zigbee_service_url' in /etc/glowup/site.json (rendered "
                "from glowup-infra/fleet/inventory.yaml on deploy) or "
                "pass coordinator_config zigbee.service_url"
            )
        self._zigbee_url: str = resolved_zigbee_url.rstrip("/")
        # Single canonical publisher to glowup-zigbee-service.  Replaces
        # the local _plug_http helper so voice and the hub scheduler
        # (phase 3) will share one client with identical error-handling
        # and positive-handoff semantics.
        self._zigbee: ZigbeeControlClient = ZigbeeControlClient(
            self._zigbee_url, timeout_s=_PLUG_HTTP_TIMEOUT,
        )

        # Per-room conversation history.
        self._chat_history: dict[str, list[dict[str, str]]] = {}
        self._chat_timestamps: dict[str, float] = {}

        # Weather client with NWS primary + Open-Meteo fallback.
        # The fallback notice hook is rebound per-utterance by the
        # daemon (via :meth:`set_interim_speaker`) so "retrying" is
        # routed to the correct satellite.
        self._weather_client: WeatherClient = WeatherClient(
            primary=NWSSource(_WEATHER_LAT, _WEATHER_LON),
            fallback=OpenMeteoSource(_WEATHER_LAT, _WEATHER_LON),
            on_fallback=self._speak_interim,
        )
        # Air quality has no viable US fallback provider — single-source.
        self._air_quality: OpenMeteoAirQuality = OpenMeteoAirQuality(
            _WEATHER_LAT, _WEATHER_LON,
        )

        # Interim-speech callback — set by the daemon before each
        # pipeline via :meth:`set_interim_speaker`.  Used by long-
        # running handlers (weather fallback) to tell the satellite
        # "retrying" without waiting for the final confirmation.
        self._interim_speaker: Optional[Any] = None

        # Load action definitions.
        self._actions: dict[str, dict[str, Any]] = self._load_actions()

        # Plug synonyms — "friendly name" (what the user says) mapped
        # to the Z2M device name (what broker-2 addresses). Loaded from
        # the `plugs:` section of actions.yml. Keys are stored in
        # *normalized* form so "ML Switch", "ml", "the ML smart plug"
        # all resolve to the same plug. _plug_display preserves the
        # original friendly name for spoken confirmation.
        #
        # Schema: each plug value is either a dict
        # {zigbee: <name>, type_words: [<word>, ...]} or a bare string
        # (legacy) interpreted as the Z2M name with empty type_words.
        # type_words are surfaced to the LLM via intent.py's capabilities
        # prompt; the executor itself does not match user phrases against
        # them — the LLM is expected to pick the friendly name as the
        # target after seeing the parenthesized type words.
        plugs_cfg: dict[str, Any] = self._actions.get("plugs", {}) or {}
        self._plug_synonyms: dict[str, str] = {}
        self._plug_display: dict[str, str] = {}
        self._plug_type_words: dict[str, list[str]] = {}
        for _friendly, _val in plugs_cfg.items():
            if isinstance(_val, str):
                _zigbee_id: str = _val
                _type_words: list[str] = []
            elif isinstance(_val, dict):
                _zigbee_id = str(_val.get("zigbee", ""))
                _type_words = [
                    str(w) for w in (_val.get("type_words") or [])
                ]
            else:
                # Malformed entry — log and skip rather than crash the
                # whole executor on one bad plug.
                logger.warning(
                    "Skipping malformed plug entry %r: %r",
                    _friendly, _val,
                )
                continue
            if not _zigbee_id:
                logger.warning(
                    "Skipping plug %r with empty zigbee name", _friendly,
                )
                continue
            self._plug_synonyms[
                self._normalize_plug_phrase(_friendly)
            ] = _zigbee_id
            self._plug_display[_zigbee_id] = _friendly
            self._plug_type_words[_zigbee_id] = _type_words

        # Named query handlers — the config's "function pointers."
        # Each takes (api_data, action_config, target_raw, params)
        # and returns a result dict.
        self._handlers: dict[str, Any] = {
            "power_state": self._handle_power_state,
            "sensor_reading": self._handle_sensor_reading,
            "power_summary": self._handle_power_summary,
            "soil_moisture": self._handle_soil_moisture,
            "weather": self._handle_weather,
            "air_quality": self._handle_air_quality,
            "forecast": self._handle_forecast,
            "system_status": self._handle_system_status,
            "set_voice": self._handle_set_voice,
            "tell_time": self._handle_tell_time,
            "tell_date": self._handle_tell_date,
            "lock_status": self._handle_lock_status,
            "door_status": self._handle_door_status,
            "alarm_status": self._handle_alarm_status,
            "battery_status": self._handle_battery_status,
            "printer_status": self._handle_printer_status,
            "schedule_status": self._handle_schedule_status,
            "uptime_status": self._handle_uptime_status,
            "shopping_add": self._handle_shopping_add,
            "shopping_remove": self._handle_shopping_remove,
            "shopping_query": self._handle_shopping_query,
            "shopping_clear": self._handle_shopping_clear,
            "identify_room": self._handle_identify_room,
            "commands": self._handle_commands,
            "help_lights": self._handle_help_lights,
            "help_shopping": self._handle_help_shopping,
            "help_security": self._handle_help_security,
            "help_system": self._handle_help_system,
            "help_sensors": self._handle_help_sensors,
            "list_sensors": self._handle_list_sensors,
            "list_groups": self._handle_list_groups,
            "list_doors": self._handle_list_doors,
            "list_locks": self._handle_list_locks,
            "enable_voice_gate": self._handle_enable_voice_gate,
            "disable_voice_gate": self._handle_disable_voice_gate,
            "joke": self._handle_joke,
        }

        # TTS reference — set after init by the coordinator daemon.
        self._tts: Any = None

        # MQTT client reference — set after init by the coordinator
        # daemon via :meth:`set_mqtt_client`.  Used by gate handlers
        # to publish retained gate state.  None when running under
        # unit tests that don't wire MQTT.
        self._mqtt_client: Any = None

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
            except Exception as exc2:
                logger.debug("Failed to read HTTP error body: %s", exc2)
            logger.error(
                "API %s %s → %d: %s (body: %s)",
                method, path, exc.code, exc.reason, error_body,
            )
            raise

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    # Filler + category words stripped when matching plug names. Keeps
    # "Switch" optional and forgives "the", "my", "smart plug" etc.
    _PLUG_DROP_WORDS: set[str] = {
        "the", "my", "a", "an", "switch", "plug", "smart",
    }

    @classmethod
    def _normalize_plug_phrase(cls, s: str) -> str:
        """Lowercase, trim, drop filler/category words.

        'Main Bedroom TV Switch' -> 'main bedroom tv'
        'the ML smart plug'      -> 'ml'
        'Backyard IR'            -> 'backyard ir'
        """
        tokens: list[str] = [
            t for t in s.lower().split()
            if t not in cls._PLUG_DROP_WORDS
        ]
        return " ".join(tokens).strip()

    def _resolve_target(self, target: str) -> str:
        """Resolve a fuzzy target name to an actual group or device.

        Uses the source room (``self._current_room``) as a tiebreaker
        when multiple groups substring-match the target.  "bedroom"
        spoken from "Main Bedroom" resolves to "Main Bedroom", not
        whichever group happens to iterate first.

        Args:
            target: Raw target string from the LLM.

        Returns:
            Resolved name with ``group:`` prefix for groups.
        """
        if target.lower() == "all":
            return "all"

        # Plug synonyms win over groups — friendly plug names like
        # "Main Bedroom TV Switch" could overlap with group names
        # (e.g. "Main Bedroom"). Normalization drops "switch", "plug",
        # and common filler words so the match is forgiving: "ML",
        # "ML switch", and "the ML smart plug" all resolve the same.
        normalized: str = self._normalize_plug_phrase(target)
        if normalized and normalized in self._plug_synonyms:
            zigbee_name: str = self._plug_synonyms[normalized]
            logger.info(
                "Target '%s' (normalized '%s') resolved to plug 'plug:%s'",
                target, normalized, zigbee_name,
            )
            return f"plug:{zigbee_name}"

        try:
            data = self._request("GET", "/api/groups")
            groups: dict = data.get("groups", {})
            target_lower: str = target.lower().strip()
            room_lower: str = getattr(
                self, "_current_room", ""
            ).lower().strip()

            # Exact match (highest priority).
            for name in groups:
                if name.lower() == target_lower:
                    logger.info(
                        "Target '%s' resolved to group 'group:%s'",
                        target, name,
                    )
                    return f"group:{name}"

            # Substring match — collect all candidates, prefer the
            # source room when there are multiple hits.
            candidates: list[str] = [
                name for name in groups
                if target_lower in name.lower()
            ]
            if candidates:
                # If the source room is among the candidates, prefer it.
                room_match: Optional[str] = next(
                    (n for n in candidates if n.lower() == room_lower),
                    None,
                )
                chosen: str = room_match if room_match else candidates[0]
                if room_match:
                    logger.info(
                        "Fuzzy target '%s' resolved to source room "
                        "'group:%s' (preferred over %d other match(es))",
                        target, chosen, len(candidates) - 1,
                    )
                else:
                    logger.info(
                        "Fuzzy target '%s' resolved to group 'group:%s'",
                        target, chosen,
                    )
                return f"group:{chosen}"

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

    def _prepare_for_brightness(
        self, target_url: str, display_target: str,
    ) -> None:
        """Stop any running effect and power on before setting brightness.

        Two prerequisites for a brightness command to visibly work:

        1. The effect engine must be stopped — otherwise it repaints
           bulb state every frame, overwriting the new brightness.
        2. The bulb must be powered on — LIFX stores HSBK while
           powered off but emits nothing.  (blank-on-poweroff means
           set_power(on) alone emits zero light, but set_brightness
           writes a visible warm-white immediately after.)

        Errors are logged but do not block the brightness call.
        """
        # Stop running effect.
        stop_cfg = self._actions.get("stop")
        if stop_cfg is not None:
            try:
                self._dispatch_command(
                    stop_cfg, target_url, display_target, {},
                )
                logger.info(
                    "Stopped running effect on %s before brightness",
                    display_target,
                )
            except Exception as exc:
                logger.debug("Stop before brightness (non-fatal): %s", exc)

        # Power on.
        power_cfg = self._actions.get("power")
        if power_cfg is not None:
            try:
                self._dispatch_command(
                    power_cfg, target_url, display_target, {"on": True},
                )
                logger.info("Powered on %s before brightness", display_target)
            except Exception as exc:
                logger.debug("Power-on before brightness (non-fatal): %s", exc)

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
        # Stash room for handlers that need it (e.g., identify_room).
        self._current_room: str = room

        action: str = intent.get("action", "unknown")
        target_raw: str = intent.get("target", "all")
        target_raw = self._resolve_target(target_raw)
        target_url: str = urllib.parse.quote(target_raw, safe="")
        params: dict[str, Any] = intent.get("params", {})

        # Human-readable target (strip group: / plug: prefix).
        if target_raw.startswith("group:"):
            display_target: str = target_raw[6:]
        elif target_raw.startswith("plug:"):
            zigbee_name: str = target_raw[5:]
            display_target = self._plug_display.get(zigbee_name, zigbee_name)
        else:
            display_target = target_raw

        # Plug intercept — friendly name resolved to plug:<zigbee_name>.
        # Route power on/off and status queries directly to broker-2's
        # glowup-zigbee-service; skip the LIFX-shaped API dispatch.
        if target_raw.startswith("plug:"):
            if action == "power":
                return self._plug_power(
                    target_raw[5:], display_target, params,
                )
            if action == "query_status":
                return self._plug_query(
                    target_raw[5:], display_target,
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
                # Brightness and power+brightness both need any running
                # effect stopped first — otherwise the effect engine
                # repaints over the new brightness on every frame.
                if action in ("brightness",):
                    self._prepare_for_brightness(target_url, display_target)

                result = self._dispatch_command(
                    action_cfg, target_url, display_target, params,
                )
                # When Ollama bundles brightness into a power intent
                # (e.g. "turn on to 10%"), chain a brightness call
                # after the power-on succeeds.
                if (
                    action == "power"
                    and result.get("status") == "ok"
                    and params.get("on") is True
                    and "brightness" in params
                ):
                    self._prepare_for_brightness(target_url, display_target)
                    bri_cfg = self._actions.get("brightness")
                    if bri_cfg:
                        self._dispatch_command(
                            bri_cfg, target_url, display_target, params,
                        )
                        result["confirmation"] = (
                            f"{display_target} on at "
                            f"{params['brightness']}%."
                        )
                        logger.info(
                            "Chained brightness %d%% after power-on for %s",
                            params["brightness"], display_target,
                        )
                return result
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

    # ------------------------------------------------------------------
    # Plug handlers — route through the shared ZigbeeControlClient.
    # No hub hop, no effect-engine machinery, no brightness chaining.
    # ------------------------------------------------------------------

    def _plug_power(
        self, zigbee_name: str, display_target: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Turn a Zigbee plug on or off via broker-2.

        Uses the shared client's positive-handoff semantics — if the
        service accepted the command but the device never echoed, the
        spoken confirmation reflects that ambiguity rather than
        claiming success.
        """
        on: bool = bool(params.get("on", False))
        state: str = "ON" if on else "OFF"
        result = self._zigbee.set_state(zigbee_name, state)
        if not result.ok:
            logger.warning(
                "Plug %s set %s failed: %s",
                zigbee_name, state, result.error,
            )
            return {
                "status": "error",
                "confirmation": (
                    f"I couldn't reach {display_target}. {result.error}."
                ),
                "speak": True,
            }
        # Service accepted but device did not echo — do not claim
        # success to the user.  This is Perry's "military device"
        # positive-handoff: every stage confirms receipt.
        if not result.echoed:
            logger.warning(
                "Plug %s sent %s but no echo: %s",
                zigbee_name, state, result.error,
            )
            return {
                "status": "error",
                "confirmation": (
                    f"I sent the command but {display_target} didn't "
                    f"confirm. It may still be switching."
                ),
                "speak": True,
            }
        logger.info("Plug %s set to %s", zigbee_name, state)
        return {
            "status": "ok",
            "confirmation": f"{display_target} is {state.lower()}.",
            "speak": True,
        }

    def _plug_query(
        self, zigbee_name: str, display_target: str,
    ) -> dict[str, Any]:
        """Report a plug's current ON/OFF state."""
        ok, result = self._zigbee.get_device(zigbee_name)
        if not ok:
            # 404 from the service arrives here — treat "unknown
            # device" distinctly from "service unreachable" so the
            # user hears an accurate spoken report.
            err: str = str(result)
            if "unknown device" in err.lower():
                return {
                    "status": "ok",
                    "confirmation": f"I don't know about {display_target}.",
                    "speak": True,
                }
            return {
                "status": "error",
                "confirmation": (
                    f"I can't check {display_target} right now."
                ),
                "speak": True,
            }
        if not isinstance(result, dict):
            return {
                "status": "error",
                "confirmation": (
                    f"I can't check {display_target} right now."
                ),
                "speak": True,
            }
        if not result.get("online", False):
            return {
                "status": "ok",
                "confirmation": f"{display_target} is offline.",
                "speak": True,
            }
        dev_state: str = (result.get("state") or "unknown").lower()
        return {
            "status": "ok",
            "confirmation": f"{display_target} is {dev_state}.",
            "speak": True,
        }

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
                                f"{label} is {f_val:.0f} degrees."
                            ),
                            "speak": True,
                        }
                    elif sensor_type == "humidity":
                        return {
                            "status": "ok",
                            "confirmation": (
                                f"{label} humidity is {value:.0f} percent."
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
                f"I don't have a {sensor_type} sensor for {display_target}."
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
    # Weather — NWS primary, Open-Meteo fallback (see weather_sources.py)
    # ------------------------------------------------------------------

    def _handle_weather(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query current outdoor conditions, optionally restricted to one aspect.

        ``params.aspect`` selects the subset of current conditions to
        speak.  Valid values: ``temperature``, ``humidity``, ``wind``,
        ``condition``, ``feels_like``, ``all`` (default).  Any other
        value is treated as ``all``.
        """
        aspect: str = str(params.get("aspect", "all")).lower()
        try:
            conditions: CurrentConditions = self._weather_client.current()
        except WeatherSourceError as exc:
            logger.error(
                "Both weather sources failed: %s", exc, exc_info=True,
            )
            return {
                "status": "error",
                "confirmation": "I couldn't reach any weather service.",
                "speak": True,
            }
        return {
            "status": "ok",
            "confirmation": self._format_weather(conditions, aspect),
            "speak": True,
        }

    def _format_weather(
        self, c: CurrentConditions, aspect: str,
    ) -> str:
        """Render a ``CurrentConditions`` into a spoken sentence.

        Per-aspect paths return a single short sentence.  The default
        ``all`` path composes temperature + feels-like (only when the
        spread exceeds :data:`_FEELS_LIKE_SPREAD_F`) + condition +
        humidity + wind.
        """
        if aspect == "temperature":
            if c.temp_f is None:
                return "I don't have an outdoor temperature right now."
            return f"It is {c.temp_f:.0f} degrees outside."

        if aspect == "humidity":
            if c.humidity_pct is None:
                return "I don't have outdoor humidity data."
            return f"Outdoor humidity is {c.humidity_pct:.0f} percent."

        if aspect == "wind":
            if c.wind_mph is None:
                return "I don't have wind data right now."
            return f"Wind is {c.wind_mph:.0f} miles per hour."

        if aspect == "condition":
            return f"It is currently {c.condition}."

        if aspect == "feels_like":
            if c.apparent_f is None:
                return "Feels-like temperature is not available."
            return f"It feels like {c.apparent_f:.0f} degrees."

        # Default: all aspects.  Build incrementally so missing fields
        # simply drop out rather than producing "0 degrees" artifacts.
        sentences: list[str] = []
        if c.temp_f is not None:
            base: str = f"It is {c.temp_f:.0f} degrees"
            if (
                c.apparent_f is not None
                and abs(c.apparent_f - c.temp_f) >= _FEELS_LIKE_SPREAD_F
            ):
                base += f" but feels like {c.apparent_f:.0f}"
            sentences.append(f"{base} with {c.condition}.")
        else:
            sentences.append(f"It is currently {c.condition}.")
        if c.humidity_pct is not None:
            sentences.append(
                f"Humidity is {c.humidity_pct:.0f} percent."
            )
        if c.wind_mph is not None:
            sentences.append(
                f"Wind is {c.wind_mph:.0f} miles per hour."
            )
        return " ".join(sentences)

    # ------------------------------------------------------------------
    # Forecast — NWS periods (primary), Open-Meteo daily (fallback)
    # ------------------------------------------------------------------

    def _handle_forecast(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query the forecast for a named period.

        ``params.when`` selects the period: ``today`` (default),
        ``tonight``, ``tomorrow``.  ``params.aspect`` narrows the
        response: ``rain`` (precipitation only), ``high`` / ``low``
        (temperature only), ``all`` (default, reads condition +
        temperature + precipitation probability).
        """
        when: str = str(params.get("when", "today")).lower()
        aspect: str = str(params.get("aspect", "all")).lower()

        try:
            periods: list[ForecastPeriod] = self._weather_client.forecast()
        except WeatherSourceError as exc:
            logger.error(
                "Both forecast sources failed: %s", exc, exc_info=True,
            )
            return {
                "status": "error",
                "confirmation": "I couldn't reach any forecast service.",
                "speak": True,
            }

        period: Optional[ForecastPeriod] = self._select_period(periods, when)
        if period is None:
            return {
                "status": "error",
                "confirmation": f"I don't have a forecast for {when}.",
                "speak": True,
            }
        return {
            "status": "ok",
            "confirmation": self._format_forecast(period, aspect),
            "speak": True,
        }

    def _select_period(
        self, periods: list[ForecastPeriod], when: str,
    ) -> Optional[ForecastPeriod]:
        """Pick the period matching ``when`` from NWS or Open-Meteo output.

        NWS names periods "Today", "This Afternoon", "Tonight", a
        weekday name, etc.  Open-Meteo is reshaped in
        :class:`OpenMeteoSource` to use "Today", "Today night",
        "Tomorrow", "Tomorrow night".  The matching here accepts either
        shape.
        """
        if not periods:
            return None

        def _name(p: ForecastPeriod) -> str:
            return p.name.lower()

        if when == "tonight":
            for p in periods:
                if _name(p) == "tonight" or _name(p) == "today night":
                    return p
            # Next nighttime period is a reasonable fallback.
            for p in periods:
                if not p.is_daytime:
                    return p
            return None

        if when == "tomorrow":
            for p in periods:
                if _name(p) == "tomorrow":
                    return p
            # NWS uses the weekday name; the first future daytime period
            # after "today"/"this afternoon" is tomorrow.
            seen_today: bool = False
            for p in periods:
                low: str = _name(p)
                if low.startswith("today") or low == "this afternoon":
                    seen_today = True
                    continue
                if seen_today and p.is_daytime:
                    return p
            return None

        # Default: today.
        for p in periods:
            low = _name(p)
            if low in ("today", "this afternoon"):
                return p
        # First daytime period is "today" if NWS skipped that label.
        for p in periods:
            if p.is_daytime:
                return p
        return periods[0]

    def _format_forecast(
        self, p: ForecastPeriod, aspect: str,
    ) -> str:
        """Render a ``ForecastPeriod`` into a spoken forecast sentence."""
        label: str = p.name or ("today" if p.is_daytime else "tonight")

        if aspect in ("rain", "precipitation"):
            if p.precip_probability_pct is None:
                return f"{label}: no precipitation data."
            return (
                f"{label}: {p.precip_probability_pct:.0f} percent "
                f"chance of precipitation."
            )

        if aspect == "high":
            if p.temperature_f is None or not p.is_daytime:
                return f"{label}: no daytime high available."
            return f"{label}'s high is {p.temperature_f:.0f}."

        if aspect == "low":
            if p.temperature_f is None or p.is_daytime:
                return f"{label}: no overnight low available."
            return f"{label}'s low is {p.temperature_f:.0f}."

        # Default: all — condition + temperature + precipitation chance.
        bits: list[str] = [f"{label}: {p.condition}"]
        if p.temperature_f is not None:
            temp_kind: str = "high" if p.is_daytime else "low"
            bits.append(f"{temp_kind} of {p.temperature_f:.0f}")
        if (
            p.precip_probability_pct is not None
            and p.precip_probability_pct >= 10.0
        ):
            bits.append(
                f"{p.precip_probability_pct:.0f} percent chance of rain"
            )
        return ", ".join(bits) + "."

    # ------------------------------------------------------------------
    # Air quality — Open-Meteo AQ (no NWS equivalent)
    # ------------------------------------------------------------------

    def _handle_air_quality(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query outdoor air quality and pollen.

        ``params.aspect`` selects the subset: ``pollen`` (all species
        or one via ``params.species``), ``pm25``, ``ozone``, ``uv``,
        ``aqi``, ``all`` (default).  When aspect is ``pollen`` and a
        specific species is named, only that species is reported.
        """
        aspect: str = str(params.get("aspect", "all")).lower()
        species: str = str(params.get("species", "")).lower().strip()

        try:
            aq: AirQuality = self._air_quality.current()
        except WeatherSourceError as exc:
            logger.error(
                "Air quality fetch failed: %s", exc, exc_info=True,
            )
            return {
                "status": "error",
                "confirmation": "I couldn't reach the air-quality service.",
                "speak": True,
            }
        return {
            "status": "ok",
            "confirmation": self._format_air_quality(aq, aspect, species),
            "speak": True,
        }

    @staticmethod
    def _band(value: float, bands: tuple[tuple[float, str], ...]) -> str:
        """Translate a numeric value to a plain-English category."""
        for threshold, label in bands:
            if value <= threshold:
                return label
        return bands[-1][1]

    def _format_air_quality(
        self, aq: AirQuality, aspect: str, species: str,
    ) -> str:
        """Render an ``AirQuality`` snapshot into a spoken sentence."""
        if aspect == "pollen":
            return self._format_pollen(aq, species)

        if aspect == "pm25":
            if aq.pm2_5 is None:
                return "PM2.5 data is not available."
            return f"PM2.5 is {aq.pm2_5:.0f} micrograms per cubic meter."

        if aspect == "ozone":
            if aq.ozone is None:
                return "Ozone data is not available."
            return f"Ozone is {aq.ozone:.0f} micrograms per cubic meter."

        if aspect == "uv":
            if aq.uv_index is None:
                return "UV index is not available."
            band: str = self._band(aq.uv_index, _UV_BANDS)
            return f"UV index is {aq.uv_index:.0f}, {band}."

        if aspect == "aqi":
            if aq.us_aqi is None:
                return "Air quality index is not available."
            band = self._band(aq.us_aqi, _AQI_BANDS)
            return f"Air quality index is {aq.us_aqi:.0f}, {band}."

        # Default: all aspects.
        parts: list[str] = []
        if aq.us_aqi is not None:
            band = self._band(aq.us_aqi, _AQI_BANDS)
            parts.append(f"Air quality is {band} at {aq.us_aqi:.0f}")
        elif aq.pm2_5 is not None:
            parts.append(f"PM2.5 is {aq.pm2_5:.0f}")
        if aq.uv_index is not None:
            uv_band: str = self._band(aq.uv_index, _UV_BANDS)
            parts.append(f"UV index {aq.uv_index:.0f} ({uv_band})")
        pollen_summary: str = self._pollen_summary(aq)
        if pollen_summary:
            parts.append(pollen_summary)
        if not parts:
            return "I don't have air-quality data right now."
        return ". ".join(parts) + "."

    def _format_pollen(self, aq: AirQuality, species: str) -> str:
        """Render a pollen-only spoken response."""
        if not aq.pollen:
            # Open-Meteo's pollen dataset is CAMS Europe; North America
            # returns all species as null. Surface the regional reality
            # instead of implying transience.
            return "Pollen data isn't published for our area."

        if species:
            # Accept either the bare species ("ragweed") or the full
            # Open-Meteo key ("ragweed_pollen").
            key: str = (
                species if species.endswith("_pollen")
                else f"{species}_pollen"
            )
            value: Optional[float] = aq.pollen.get(key)
            if value is None:
                return f"I don't have {species} pollen data."
            label: str = _POLLEN_LABELS.get(key, species.replace("_", " "))
            band: str = self._band(value, _POLLEN_THRESHOLDS)
            return f"{label.capitalize()} pollen is {band} at {value:.0f}."

        # All species — report the highest-category one.
        summary: str = self._pollen_summary(aq)
        if not summary:
            return "No pollen detected right now."
        return summary + "."

    def _pollen_summary(self, aq: AirQuality) -> str:
        """Build a short pollen summary naming dominant species.

        Returns an empty string if no species report any pollen.  When
        multiple species are in the same top category, they are listed;
        "low"-only readings are summarized as "all low" rather than
        enumerated, since that is the common no-news case.
        """
        if not aq.pollen:
            return ""

        categorized: dict[str, list[tuple[str, float]]] = {}
        for key, val in aq.pollen.items():
            band: str = self._band(val, _POLLEN_THRESHOLDS)
            label: str = _POLLEN_LABELS.get(key, key.replace("_pollen", ""))
            categorized.setdefault(band, []).append((label, val))

        # Order from worst to best; surface the worst category with
        # names, or just report "all low" when nothing is elevated.
        for band in ("very high", "high", "moderate"):
            if band in categorized:
                names: list[str] = [n for n, _ in categorized[band]]
                return f"Pollen: {', '.join(names)} {band}"
        if "low" in categorized:
            return "Pollen is low"
        return ""

    # ------------------------------------------------------------------
    # System status — comprehensive health check
    # ------------------------------------------------------------------

    def _handle_system_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Check overall system health and verbalize the result.

        Queries server status, device count, adapter states, and
        schedule.  If everything is healthy, responds with
        "All is well."  Otherwise enumerates problems.
        """
        problems: list[str] = []

        # 1. Server status + adapter health (single API call).
        adapters: dict[str, dict[str, Any]] = {}
        try:
            status_data: dict[str, Any] = self._request(
                "GET", "/api/status",
            )
            if not status_data.get("ready", False):
                problems.append("server is still loading")
            adapters = status_data.get("adapters", {})
        except Exception:
            return {
                "status": "error",
                "confirmation": "The server is not responding.",
                "speak": True,
            }

        # 2. Check each adapter/daemon.
        adapter_ok: list[str] = []
        adapter_bad: list[str] = []
        for name, info in adapters.items():
            # Adapters report health differently:
            #   - threads: {"running": bool}
            #   - network adapters: {"connected": bool}
            #   - printer: {"status": "ok"/"error"}
            # An adapter is healthy if ANY positive indicator is present.
            healthy: bool = (
                info.get("running", False)
                or info.get("connected", False)
                or info.get("status") == "ok"
            )
            if healthy:
                adapter_ok.append(name)
            else:
                adapter_bad.append(name)
                hint: str = _REPAIR_HINTS.get(name, "")
                if hint:
                    problems.append(f"{name} is down. {hint}")
                else:
                    problems.append(f"{name} is down")

        # 3. Device count.
        total: int = 0
        powered_on: int = 0
        try:
            dev_data: dict[str, Any] = self._request(
                "GET", "/api/devices",
            )
            devices: list[dict[str, Any]] = dev_data.get("devices", [])
            total = len(devices)
            powered_on = sum(
                1 for d in devices if d.get("power", False)
            )
            if total == 0:
                problems.append("no devices discovered")
        except Exception:
            problems.append("cannot read device list")

        # 4. Schedule.
        schedule_count: int = 0
        try:
            sched_data: dict[str, Any] = self._request(
                "GET", "/api/schedule",
            )
            entries: list = sched_data.get("entries", [])
            schedule_count = sum(
                1 for e in entries if e.get("enabled", True)
            )
        except Exception:
            pass  # Non-critical.

        # 5. Build spoken response — keep it short to avoid
        # audio breakup on long TTS playback.
        if not problems:
            confirmation: str = (
                f"All is well. {total} devices, {powered_on} on, "
                f"{len(adapter_ok)} adapters healthy."
            )
        else:
            confirmation = ". ".join(
                p.capitalize() for p in problems
            ) + "."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    # ------------------------------------------------------------------
    # TTS voice management
    # ------------------------------------------------------------------

    def set_tts(self, tts: Any) -> None:
        """Set the TTS engine reference for voice-change commands.

        Args:
            tts: TextToSpeech instance from the coordinator.
        """
        self._tts = tts

    def set_interim_speaker(
        self, cb: Optional[Any],
    ) -> None:
        """Register a ``(room, text)`` callback for mid-handler updates.

        The daemon binds this per utterance to the scoped satellite TTS
        publisher so that long-running handlers (weather failover) can
        emit a "retrying" notice while still running.  Clearing the
        callback (``None``) between utterances is not required — the
        next utterance will simply overwrite it — but the daemon does
        so to keep the state accurate when idle.
        """
        self._interim_speaker = cb

    def _speak_interim(self, text: str) -> None:
        """Best-effort emit ``text`` to the active pipeline's satellite.

        Silently no-ops when no callback is registered or no room is
        currently bound.  Handler code should never depend on the
        interim reaching the user — it is purely a "still working" hint.
        """
        cb = self._interim_speaker
        room: str = getattr(self, "_current_room", "") or ""
        if cb is None or not room:
            return
        try:
            cb(room, text)
        except Exception as exc:
            logger.debug("Interim speaker raised: %s", exc)

    def set_mqtt_client(self, client: Any) -> None:
        """Set the MQTT client reference for gate publish handlers.

        Called by the coordinator daemon after MQTT is connected.
        Gate handlers use this client to publish retained state on
        ``glowup/voice/gate/<room_slug>``.  Left as None in unit
        tests — handlers detect the missing client and return an
        error result instead of crashing.

        Args:
            client: paho-mqtt Client instance.
        """
        self._mqtt_client = client

    def _handle_set_voice(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Change the macOS say voice at runtime."""
        voice: str = params.get("voice_name", display_target)
        if self._tts is None:
            return {
                "status": "error",
                "confirmation": "Voice engine is not available.",
                "speak": True,
            }
        self._tts.voice_name = voice
        return {
            "status": "ok",
            "confirmation": f"Voice changed to {voice}.",
            "speak": True,
        }

    # ------------------------------------------------------------------
    # Tell time — local clock, no API call
    # ------------------------------------------------------------------

    def _handle_tell_time(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Speak the current local time.

        Formats the time in 12-hour spoken form (e.g., "It is 3:45 PM").
        No API call required — reads the system clock directly.
        """
        now: datetime = datetime.now()
        hour: int = now.hour
        minute: int = now.minute
        period: str = "AM" if hour < 12 else "PM"

        # Convert to 12-hour format.
        display_hour: int = hour % 12
        if display_hour == 0:
            display_hour = 12

        if minute == 0:
            time_str: str = f"{display_hour} {period}"
        else:
            time_str = f"{display_hour}:{minute:02d} {period}"

        return {
            "status": "ok",
            "confirmation": f"It is {time_str}.",
            "speak": True,
        }

    def _handle_tell_date(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Speak today's date and day of the week.

        No API call required — reads the system clock directly.
        """
        now: datetime = datetime.now()
        # "Saturday, April 5th, 2026"
        day_name: str = now.strftime("%A")
        month_name: str = now.strftime("%B")
        day_num: int = now.day
        year: int = now.year

        # Ordinal suffix.
        if 11 <= day_num <= 13:
            suffix: str = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(day_num % 10, "th")

        return {
            "status": "ok",
            "confirmation": f"Today is {day_name}, {month_name} {day_num}{suffix}, {year}.",
            "speak": True,
        }

    def _handle_lock_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report lock status from Vivint adapter.

        One API call to /api/home/locks.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/locks")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the lock system.",
                "speak": True,
            }

        locks: list[dict[str, Any]] = data.get("locks", [])
        if not locks:
            return {
                "status": "ok",
                "confirmation": "No locks are configured.",
                "speak": True,
            }

        # Filter to specific lock if target is not "all".
        if target_raw and target_raw != "all":
            target_lower: str = target_raw.lower()
            locks = [
                lk for lk in locks
                if target_lower in lk.get("name", "").lower()
            ]
            if not locks:
                return {
                    "status": "ok",
                    "confirmation": f"I don't see a lock called {display_target}.",
                    "speak": True,
                }

        locked: list[str] = []
        unlocked: list[str] = []
        for lk in locks:
            name: str = lk.get("name", "unknown")
            # API returns "locked": true/false.
            if lk.get("locked", False):
                locked.append(name)
            else:
                unlocked.append(name)

        if not unlocked:
            confirmation: str = "All locks are locked."
        elif not locked:
            confirmation = "All locks are unlocked."
        else:
            unlocked_names: str = ", ".join(unlocked)
            confirmation = f"{unlocked_names} {'is' if len(unlocked) == 1 else 'are'} unlocked."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_door_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report door contact sensor status from Vivint adapter.

        One API call to /api/home/security.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/security")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the security system.",
                "speak": True,
            }

        doors: list[dict[str, Any]] = data.get("doors", [])
        if not doors:
            return {
                "status": "ok",
                "confirmation": "No door sensors are configured.",
                "speak": True,
            }

        open_doors: list[str] = [
            d.get("name", "unknown") for d in doors if d.get("open", False)
        ]

        if not open_doors:
            confirmation: str = "All doors are closed."
        else:
            names: str = ", ".join(open_doors)
            confirmation = f"{names} {'is' if len(open_doors) == 1 else 'are'} open."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_alarm_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report alarm/security panel state from Vivint adapter.

        One API call to /api/home/security.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/security")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the security system.",
                "speak": True,
            }

        state: str = data.get("alarm_state", "unknown")
        # Map internal state names to spoken forms.
        spoken_map: dict[str, str] = {
            "disarmed": "disarmed",
            "armed_stay": "armed in stay mode",
            "armed_away": "armed in away mode",
            "unknown": "in an unknown state",
        }
        spoken: str = spoken_map.get(state, state)

        return {
            "status": "ok",
            "confirmation": f"The alarm is {spoken}.",
            "speak": True,
        }

    def _handle_battery_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report Vivint devices that are audibly complaining.

        Answers "any low batteries?" and "are any sensors chirping?".
        A Vivint sensor chirps when its low_battery flag is set, when
        it is reporting tampered, or (on smoke/CO units) when it is
        bypassed.  This handler walks the Vivint adapter's sensor and
        lock state and reports every "attention needed" reason with
        the human-readable sensor name so Perry can locate the one
        that is actually beeping without hunting by ear.

        Checked signals (per sensor, via /api/status):
          - ``low_battery`` boolean (vivintpy authoritative chirp reason)
          - ``is_tampered`` boolean (audible on most Vivint gear)
          - ``is_bypassed`` boolean (panel announces bypassed zones)
          - ``battery`` percent <30%   (legacy fallback for sensors that
                                         report a numeric level)

        First Alert detectors are NOT on the network and cannot be
        queried — they are excluded from this response.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/status")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the server.",
                "speak": True,
            }

        # Legacy percent threshold for sensors/locks that report a
        # numeric battery level.  Smoke detectors report boolean low
        # only, so the bool path below is the authoritative source.
        LOW_THRESHOLD: int = 30

        attention: list[str] = []  # Human-readable reasons, one per issue.
        vivint: dict[str, Any] = data.get("adapters", {}).get("vivint", {})

        # Check locks — percent battery only, no tamper/bypass fields.
        for name, info in vivint.get("locks", {}).items():
            batt: float = info.get("battery", 1.0)
            pct: int = int(batt * 100) if batt <= 1.0 else int(batt)
            if pct < LOW_THRESHOLD:
                label: str = name.replace("_", " ").title()
                attention.append(f"{label} at {pct} percent")

        # Check sensors — percent AND the new boolean fault fields
        # (low_battery / is_tampered / is_bypassed) published by the
        # vivint adapter as of 2026-04-12.  The booleans catch smoke
        # detectors which only report "low yes/no," not a percentage.
        for name, info in vivint.get("sensors", {}).items():
            label = info.get("name", name)

            # Boolean low-battery → authoritative chirp reason.
            if info.get("low_battery"):
                attention.append(f"{label} low battery")

            # Tamper → audible beep until cleared.
            if info.get("is_tampered"):
                attention.append(f"{label} tampered")

            # Bypassed → panel chirps to remind you a zone is off.
            if info.get("is_bypassed"):
                attention.append(f"{label} bypassed")

            # Numeric battery percent, for sensors that report one.
            batt_val: Any = info.get("battery")
            if batt_val is not None:
                pct = int(batt_val)
                # Only flag on percent if we did NOT already flag on
                # the boolean — avoid saying the same thing twice.
                if pct < LOW_THRESHOLD and not info.get("low_battery"):
                    attention.append(f"{label} at {pct} percent")

        if not attention:
            confirmation: str = (
                "All Vivint sensors are reporting clean. "
                "If you still hear chirping, check a First Alert unit."
            )
        else:
            # Speak the first few by name so Perry can walk straight
            # to the offender; summarize the tail.
            head: list[str] = attention[:5]
            confirmation = (
                f"{len(attention)} need attention: " + ", ".join(head)
            )
            if len(attention) > 5:
                confirmation += f", and {len(attention) - 5} more"
            confirmation += "."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_printer_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report printer health from the printer adapter.

        One API call to /api/status.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/status")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the server.",
                "speak": True,
            }

        printer: dict[str, Any] = data.get("adapters", {}).get("printer", {})
        if not printer:
            return {
                "status": "ok",
                "confirmation": "No printer is configured.",
                "speak": True,
            }

        status: str = printer.get("status", "unknown")
        details: dict[str, Any] = printer.get("details", {})
        alerts: list[str] = details.get("alerts", [])
        drum_pct: float = details.get("drum_life_pct", 0)
        page_count: int = details.get("page_count", 0)

        if status == "ok" and not alerts:
            confirmation: str = (
                f"Printer is fine. Drum at {drum_pct:.0f}%, "
                f"{page_count} pages printed."
            )
        else:
            alert_str: str = ", ".join(alerts) if alerts else "unknown issue"
            confirmation = f"Printer needs attention: {alert_str}."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_schedule_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report active schedule entries.

        One API call to /api/schedule.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/schedule")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the server.",
                "speak": True,
            }

        entries: list[dict[str, Any]] = data.get("entries", [])
        enabled: list[dict[str, Any]] = [
            e for e in entries if e.get("enabled", True)
        ]

        if not enabled:
            return {
                "status": "ok",
                "confirmation": "No active schedules.",
                "speak": True,
            }

        # Summarize: count + first few targets.
        targets: list[str] = []
        for e in enabled[:5]:
            t: str = e.get("target", e.get("label", "unknown"))
            if t not in targets:
                targets.append(t)

        target_str: str = ", ".join(targets)
        confirmation: str = (
            f"{len(enabled)} active schedules covering {target_str}."
        )

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_uptime_status(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Report server uptime.

        One API call to /api/status (checks response time as proxy).
        Uses the process start time from /api/status if available,
        otherwise just confirms the server is reachable.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/status")
        except Exception:
            return {
                "status": "error",
                "confirmation": "The server is not responding.",
                "speak": True,
            }

        # Server doesn't expose uptime directly. Report adapter count
        # and ready state as a proxy.
        adapters: dict[str, Any] = data.get("adapters", {})
        healthy: int = sum(
            1 for info in adapters.values()
            if info.get("running") or info.get("connected") or info.get("status") == "ok"
        )

        return {
            "status": "ok",
            "confirmation": (
                f"Server is up and ready with {healthy} adapters online."
            ),
            "speak": True,
        }

    def _handle_shopping_add(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Add an item to the shopping list.

        One API call to POST /api/shopping.
        """
        item_text: str = params.get("item", "").strip()
        if not item_text:
            return {
                "status": "error",
                "confirmation": "I didn't catch what to add.",
                "speak": True,
            }
        try:
            self._request("POST", "/api/shopping", {"text": item_text})
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to add to the shopping list.",
                "speak": True,
            }
        return {
            "status": "ok",
            "confirmation": f"Added {item_text} to the shopping list.",
            "speak": True,
        }

    def _handle_shopping_remove(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Remove an item from the shopping list.

        Fetches the list, finds the item, deletes by ID.
        """
        item_text: str = params.get("item", "").strip()
        if not item_text:
            return {
                "status": "error",
                "confirmation": "I didn't catch what to remove.",
                "speak": True,
            }
        try:
            data: dict[str, Any] = self._request("GET", "/api/shopping")
            items: list[dict[str, Any]] = data.get("items", [])
            target_lower: str = item_text.lower()
            for item in items:
                if (item["text"].lower() == target_lower
                        and not item.get("checked")):
                    self._request("DELETE", f"/api/shopping/{item['id']}")
                    return {
                        "status": "ok",
                        "confirmation": f"Removed {item_text} from the list.",
                        "speak": True,
                    }
            return {
                "status": "ok",
                "confirmation": f"{item_text} is not on the list.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to check the shopping list.",
                "speak": True,
            }

    def _handle_shopping_query(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Query the shopping list — specific item or full list.

        One API call to GET /api/shopping.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/shopping")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to check the shopping list.",
                "speak": True,
            }

        items: list[dict[str, Any]] = [
            i for i in data.get("items", []) if not i.get("checked")
        ]

        # Specific item query.
        item_text: str = params.get("item", "").strip()
        if item_text:
            found: bool = any(
                i["text"].lower() == item_text.lower() for i in items
            )
            if found:
                return {
                    "status": "ok",
                    "confirmation": f"Yes, {item_text} is on the list.",
                    "speak": True,
                }
            return {
                "status": "ok",
                "confirmation": f"No, {item_text} is not on the list.",
                "speak": True,
            }

        # Full list.
        if not items:
            return {
                "status": "ok",
                "confirmation": "The shopping list is empty.",
                "speak": True,
            }

        # Read up to 8 items to keep TTS reasonable.
        names: list[str] = [i["text"] for i in items[:8]]
        count: int = len(items)
        listing: str = ", ".join(names)
        if count > 8:
            listing += f", and {count - 8} more"
        return {
            "status": "ok",
            "confirmation": f"{count} items: {listing}.",
            "speak": True,
        }

    def _handle_shopping_clear(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Clear the entire shopping list.

        Fetches and deletes all items.
        """
        try:
            data: dict[str, Any] = self._request("GET", "/api/shopping")
            items: list[dict[str, Any]] = data.get("items", [])
            for item in items:
                self._request("DELETE", f"/api/shopping/{item['id']}")
            return {
                "status": "ok",
                "confirmation": f"Shopping list cleared. {len(items)} items removed.",
                "speak": True,
            }
        except Exception:
            return {
                "status": "error",
                "confirmation": "Failed to clear the shopping list.",
                "speak": True,
            }

    def _handle_identify_room(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Identify which satellite/room is processing this request.

        No API call — the room name comes from the satellite's MQTT
        header, passed through the pipeline as display_target.
        """
        # display_target is "all" for untargeted queries, but the
        # room is passed through the pipeline's room parameter.
        # The executor receives room via the intent's target, but
        # the actual room comes from the pipeline caller.
        # We store it on self during execute() dispatch.
        room: str = getattr(self, "_current_room", "unknown")
        return {
            "status": "ok",
            "confirmation": f"You are in the {room}.",
            "speak": True,
        }

    # ------------------------------------------------------------------
    # Voice gate handlers — enable/disable the doorbell (and any other
    # gated satellite) from an interior trusted room, time-bounded.
    # ------------------------------------------------------------------

    # Words that spell durations — allow "two hours", "2 hours", etc.
    # Kept as a class constant so the handler is pure and testable.
    _NUMBER_WORDS: dict[str, int] = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
        "twenty": 20, "thirty": 30, "forty": 40, "fortyfive": 45,
        "forty-five": 45, "sixty": 60, "ninety": 90,
    }

    @classmethod
    def _parse_duration_seconds(
        cls, params: dict[str, Any],
    ) -> int:
        """Extract a duration in seconds from intent params.

        Accepts any of the following shapes (first match wins):
        - ``duration_seconds`` (int or numeric string)
        - ``duration_minutes``
        - ``duration_hours``
        - ``duration`` as a free-form string like ``"two hours"``,
          ``"30 minutes"``, ``"10 min"``, ``"1h"``, ``"90s"``

        Returns 0 when no recognizable duration is present so the
        caller can reject the request.  Zero is deliberately returned
        for missing OR malformed input — the handler must not invent
        a default, per the approved design.
        """
        # Primary path: explicit seconds.
        if "duration_seconds" in params:
            try:
                return max(0, int(params["duration_seconds"]))
            except (TypeError, ValueError):
                return 0

        if "duration_minutes" in params:
            try:
                return max(0, int(params["duration_minutes"]) * 60)
            except (TypeError, ValueError):
                return 0

        if "duration_hours" in params:
            try:
                return max(0, int(params["duration_hours"]) * 3600)
            except (TypeError, ValueError):
                return 0

        raw: Any = params.get("duration")
        if not isinstance(raw, str) or not raw.strip():
            return 0

        text: str = raw.strip().lower()
        # Tokenize on whitespace; handle "1h", "30m", "90s" as units.
        tokens: list[str] = text.replace(",", " ").split()
        value: int = 0
        unit_multiplier: int = 0

        for tok in tokens:
            # Compact forms: "2h", "30m", "90s".
            if tok.endswith("h") and tok[:-1].isdigit():
                return int(tok[:-1]) * 3600
            if tok.endswith("m") and tok[:-1].isdigit():
                return int(tok[:-1]) * 60
            if tok.endswith("s") and tok[:-1].isdigit():
                return int(tok[:-1])

            if tok.isdigit():
                value = int(tok)
                continue
            if tok in cls._NUMBER_WORDS:
                value = cls._NUMBER_WORDS[tok]
                continue
            if tok.startswith("hour"):
                unit_multiplier = 3600
            elif tok.startswith("min"):
                unit_multiplier = 60
            elif tok.startswith("sec"):
                unit_multiplier = 1

        if value > 0 and unit_multiplier > 0:
            return value * unit_multiplier
        return 0

    def _gate_slug_for_target(self, target_raw: str) -> str:
        """Map an intent target onto a gate room slug.

        The intent parser returns targets like ``"doorbell"``,
        ``"porch"``, ``"front porch"``, etc.  The satellite's gate
        topic uses the slug of its room name — Doorbell → ``doorbell``.
        We treat "porch" and "doorbell" as aliases for the Front
        Doorbell gate.  Any other target becomes its own slug so
        future gated rooms work without code changes.
        """
        t: str = (target_raw or "").strip().lower()
        if t in ("doorbell", "porch", "front porch", "front door",
                 "front doorbell"):
            return "doorbell"
        return t.replace(" ", "_")

    def _handle_enable_voice_gate(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enable a gated satellite's mic for a bounded duration.

        Enforces three rules from the approved design:

        - The originating room must be in
          :data:`voice.constants.VOICE_GATE_ALLOWED_ROOMS`.  This
          stops a gated exterior satellite from enabling itself.
        - A duration is REQUIRED.  Missing/zero duration is a parse
          error, not a silent default — the satellite speaks back
          "how long?" and the caller re-issues with a duration.
        - The duration is clamped to
          :data:`voice.constants.VOICE_GATE_MAX_SECONDS`.  Any
          longer request is trimmed and the spoken reply says so.
        """
        room: str = getattr(self, "_current_room", "unknown")
        if room not in C.VOICE_GATE_ALLOWED_ROOMS:
            logger.warning(
                "Voice gate enable rejected: room %r not in allowlist",
                room,
            )
            return {
                "status": "error",
                "confirmation": (
                    "I can't enable the porch from here."
                ),
                "speak": True,
            }

        requested: int = self._parse_duration_seconds(params)
        if requested <= 0:
            return {
                "status": "error",
                "confirmation": (
                    "How long should I open the porch for?"
                ),
                "speak": True,
            }

        clamped: int = min(requested, C.VOICE_GATE_MAX_SECONDS)
        was_clamped: bool = clamped < requested

        slug: str = self._gate_slug_for_target(target_raw)
        topic: str = f"{C.TOPIC_VOICE_GATE_PREFIX}/{slug}"
        expires_at: float = time.time() + float(clamped)

        if self._mqtt_client is None:
            logger.error(
                "Gate enable requested but no MQTT client wired",
            )
            return {
                "status": "error",
                "confirmation": (
                    "I can't reach the satellites right now."
                ),
                "speak": True,
            }

        payload: bytes = json.dumps({
            "enabled": True,
            "expires_at": expires_at,
        }).encode("utf-8")

        try:
            self._mqtt_client.publish(topic, payload, qos=1, retain=True)
        except Exception as exc:
            logger.error("Gate enable publish failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Publishing to the gate failed.",
                "speak": True,
            }

        logger.info(
            "Gate enabled on %s for %ds from room %r (requested=%ds, clamped=%s)",
            topic, clamped, room, requested, was_clamped,
        )

        if was_clamped:
            confirmation: str = (
                "OK, opening the porch for two hours — "
                "I can't do longer."
            )
        else:
            confirmation = self._format_gate_duration(clamped)

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_disable_voice_gate(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Close a gated satellite's gate immediately.

        No allowlist check — closing is always safe.  Publishes a
        retained ``{"enabled": false, "expires_at": 0}`` message so
        a reconnecting satellite picks up the closed state.
        """
        slug: str = self._gate_slug_for_target(target_raw)
        topic: str = f"{C.TOPIC_VOICE_GATE_PREFIX}/{slug}"

        if self._mqtt_client is None:
            logger.error(
                "Gate disable requested but no MQTT client wired",
            )
            return {
                "status": "error",
                "confirmation": (
                    "I can't reach the satellites right now."
                ),
                "speak": True,
            }

        payload: bytes = json.dumps({
            "enabled": False,
            "expires_at": 0,
        }).encode("utf-8")

        try:
            self._mqtt_client.publish(topic, payload, qos=1, retain=True)
        except Exception as exc:
            logger.error("Gate disable publish failed: %s", exc)
            return {
                "status": "error",
                "confirmation": "Publishing to the gate failed.",
                "speak": True,
            }

        logger.info("Gate closed on %s", topic)
        return {
            "status": "ok",
            "confirmation": "Porch closed.",
            "speak": True,
        }

    @staticmethod
    def _format_gate_duration(seconds: int) -> str:
        """Render a gate duration as a spoken confirmation."""
        if seconds % 3600 == 0 and seconds >= 3600:
            h: int = seconds // 3600
            noun: str = "hour" if h == 1 else "hours"
            return f"Opening the porch for {h} {noun}."
        if seconds % 60 == 0 and seconds >= 60:
            m: int = seconds // 60
            noun = "minute" if m == 1 else "minutes"
            return f"Opening the porch for {m} {noun}."
        return f"Opening the porch for {seconds} seconds."

    # ------------------------------------------------------------------
    # Commands / help / list handlers
    # ------------------------------------------------------------------

    def _handle_commands(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Top-level command summary — speaks categories."""
        return {
            "status": "ok",
            "confirmation": (
                "I can control lights, answer questions about sensors "
                "and weather, manage your shopping list, check home "
                "security, and have a conversation. "
                "Say 'help lights', 'help sensors', 'help shopping', "
                "'help security', or 'help system' for details."
            ),
            "speak": True,
        }

    def _handle_help_lights(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for lighting commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can turn lights on or off, set brightness, "
                "change color or color temperature, play effects, "
                "and stop them. You can target a single light, "
                "a group, or say 'all'. "
                "Say 'list groups' to hear your groups."
            ),
            "speak": True,
        }

    def _handle_help_shopping(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for shopping list commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can add items to the shopping list, remove items, "
                "read the list, or clear it. "
                "For example, say 'add milk to the shopping list' "
                "or 'what's on the shopping list'."
            ),
            "speak": True,
        }

    def _handle_help_security(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for home security commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can check if doors are locked, if any doors are open, "
                "the alarm status, and battery levels. "
                "Say 'list locks' or 'list doors' to hear the names."
            ),
            "speak": True,
        }

    def _handle_help_system(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for system commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can check system status, report uptime, "
                "restart an adapter, change my voice, "
                "tell time and date, check the schedule, "
                "check the printer, and flush pending requests."
            ),
            "speak": True,
        }

    def _handle_help_sensors(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Help for sensor commands."""
        return {
            "status": "ok",
            "confirmation": (
                "I can read temperature and humidity from indoor sensors, "
                "check soil moisture, report power usage from smart plugs, "
                "and get the weather. "
                "Say 'list sensors' to hear what sensors are available."
            ),
            "speak": True,
        }

    def _handle_list_sensors(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate available sensors from live API data."""
        parts: list[str] = []

        # BLE sensors (temperature, humidity).
        try:
            ble_data: dict[str, Any] = self._request("GET", "/api/ble/sensors")
            if ble_data:
                names: list[str] = list(ble_data.keys())
                parts.append(
                    f"Indoor sensors: {', '.join(names)}"
                )
        except Exception:
            parts.append("Indoor sensors are not responding")

        # Soil moisture sensors (from actions.yml zones).
        soil_cfg: dict[str, Any] | None = self._actions.get("query_soil")
        if soil_cfg:
            zones: dict[str, Any] = soil_cfg.get("zones", {})
            if zones:
                zone_names: list[str] = list(zones.keys())
                parts.append(
                    f"Soil sensors: {', '.join(zone_names)}"
                )

        # Power monitors (Zigbee smart plugs).
        try:
            power_data: dict[str, Any] = self._request(
                "GET", "/api/power/devices",
            )
            raw_devices: Any = power_data.get("devices", [])
            # API returns a list of device name strings.
            if isinstance(raw_devices, list) and raw_devices:
                parts.append(
                    f"Power monitors: {', '.join(str(d) for d in raw_devices)}"
                )
        except Exception as exc:
            logger.debug("Failed to query power devices: %s", exc)

        if not parts:
            confirmation: str = "No sensors are available right now."
        else:
            confirmation = ". ".join(parts) + "."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_list_groups(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate light groups from live API data."""
        try:
            data: dict[str, Any] = self._request("GET", "/api/groups")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the lighting system.",
                "speak": True,
            }

        groups_dict: dict[str, Any] = data.get("groups", {})
        group_names: list[str] = [
            name for name in groups_dict.keys() if name != "all"
        ]
        if not group_names:
            confirmation: str = "No groups are configured."
        else:
            confirmation = f"Your groups are: {', '.join(group_names)}."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_list_doors(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate door sensors from live API data."""
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/security")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the security system.",
                "speak": True,
            }

        doors: list[dict[str, Any]] = data.get("doors", [])
        if not doors:
            confirmation: str = "No door sensors are configured."
        else:
            names: list[str] = [d.get("name", "unknown") for d in doors]
            confirmation = f"Your door sensors are: {', '.join(names)}."

        return {
            "status": "ok",
            "confirmation": confirmation,
            "speak": True,
        }

    def _handle_list_locks(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Enumerate locks from live API data."""
        try:
            data: dict[str, Any] = self._request("GET", "/api/home/locks")
        except Exception:
            return {
                "status": "error",
                "confirmation": "Cannot reach the lock system.",
                "speak": True,
            }

        locks: list[dict[str, Any]] = data.get("locks", [])
        if not locks:
            confirmation: str = "No locks are configured."
        else:
            names: list[str] = [lk.get("name", "unknown") for lk in locks]
            confirmation = f"Your locks are: {', '.join(names)}."

        return {
            "status": "ok",
            "confirmation": confirmation,
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

    def _call_ollama_chat(
        self,
        system_prompt: str,
        message: str,
        room: str,
        temperature: float,
        fail_message: str,
    ) -> dict[str, Any]:
        """Call Ollama /api/chat with a custom system prompt and
        per-room history.

        Shared by freeform chat and category-specific handlers
        (jokes, etc.).  The system prompt and temperature vary per
        caller; the conversation history, model, timeout, and reply
        bookkeeping are common — extracting this helper keeps the
        per-category handlers to a few lines and prevents the
        Ollama-call logic from drifting between copies.

        Args:
            system_prompt: System role content for this call.
            message:       User message.
            room:          Room name for history isolation.
            temperature:   Sampling temperature.
            fail_message:  Spoken fallback if the call raises.

        Returns:
            Result dict with status and confirmation.
        """
        if not message.strip():
            return {
                "status": "error",
                "confirmation": "I didn't catch that.",
                "speak": True,
            }

        history: list[dict[str, str]] = self._get_chat_history(room)

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        payload: dict[str, Any] = {
            "model": self._chat_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": _CHAT_NUM_PREDICT,
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
            with urllib.request.urlopen(
                req, timeout=_OLLAMA_TIMEOUT_S,
            ) as resp:
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
                "confirmation": fail_message,
                "speak": True,
            }

    def _exec_chat(
        self, message: str, room: str,
    ) -> dict[str, Any]:
        """Handle freeform chat via Ollama with per-room history.

        Thin wrapper over :meth:`_call_ollama_chat` with the
        general-purpose chat system prompt and default temperature.
        Kept as a named method because the chat action type in
        :meth:`execute_intent` dispatches to this name explicitly.
        """
        return self._call_ollama_chat(
            system_prompt=_CHAT_SYSTEM_PROMPT,
            message=message,
            room=room,
            temperature=_CHAT_TEMPERATURE,
            fail_message="Sorry, I couldn't think of a response.",
        )

    def _handle_joke(
        self,
        cfg: dict[str, Any],
        target_url: str,
        target_raw: str,
        display_target: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Tell a joke via Ollama with broad-category sampling.

        The intent parser routes any joke-shaped request here and
        forwards the user's exact spoken text in ``params.message``.
        Forwarding the raw phrasing lets style-specific requests
        ("knock knock", "dad joke", "tell me a science joke") reach
        the model verbatim and override the system prompt's
        knock-knock-avoidance default.  Falls back to a generic
        "tell me a joke" if the intent didn't supply a message.

        Per-room chat history is shared with the chat action so
        consecutive "another joke" requests within the 30-minute
        history window naturally avoid what was just said.
        """
        room: str = getattr(self, "_current_room", "unknown")
        message: str = params.get("message") or "Tell me a joke."
        return self._call_ollama_chat(
            system_prompt=_JOKE_SYSTEM_PROMPT,
            message=message,
            room=room,
            temperature=_JOKE_TEMPERATURE,
            fail_message="Sorry, I can't think of a joke right now.",
        )
