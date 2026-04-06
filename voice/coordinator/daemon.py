"""GlowUp Voice Coordinator daemon.

Subscribes to MQTT for utterance messages from satellites, dispatches
each to a worker thread for processing through the full pipeline:
STT → intent → execute → TTS → response.

Usage::

    # With real STT (requires faster-whisper + ffmpeg):
    python -m voice.coordinator.daemon --broker 10.0.0.48

    # Mock mode (type transcription manually):
    python -m voice.coordinator.daemon --mock-stt --mock-intent
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import concurrent.futures
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Optional

from voice import constants as C
from voice.protocol import ProtocolError, decode
from voice.coordinator.pipeline import process_utterance

logger: logging.Logger = logging.getLogger("glowup.voice.coordinator")

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------

try:
    import paho.mqtt.client as mqtt
    _PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:
    mqtt = None  # type: ignore[assignment]
    _PAHO_V2 = False


class CoordinatorDaemon:
    """Voice coordinator: MQTT subscriber + worker pool + pipeline.

    Args:
        config: Configuration dict.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the coordinator."""
        self._config: dict[str, Any] = config
        self._running: bool = False

        # MQTT.
        mqtt_cfg: dict[str, Any] = config.get("mqtt", {})
        self._mqtt_broker: str = mqtt_cfg.get("broker", "localhost")
        self._mqtt_port: int = mqtt_cfg.get("port", 1883)
        self._mqtt_client: Optional[Any] = None

        # Worker pool.
        workers_cfg: dict[str, Any] = config.get("workers", {})
        self._max_workers: int = workers_cfg.get(
            "max_threads", C.MAX_WORKERS,
        )
        self._pool: Optional[concurrent.futures.ThreadPoolExecutor] = None

        # Pipeline components — initialized in start().
        self._stt: Any = None
        self._intent: Any = None
        self._executor: Any = None
        self._tts: Any = None
        self._player: Any = None

        # Per-room speaker routing.
        # "local": coordinator speaks via persistent piper + sounddevice.
        # "satellite": send TTS text back to satellite to speak.
        # "mqtt": publish to a custom MQTT topic for a remote speaker.
        self._room_speakers: dict[str, dict[str, Any]] = config.get(
            "room_speakers", {},
        )
        self._local_speaker: Optional[Any] = None

        # GlowUp API config.
        glowup_cfg: dict[str, Any] = config.get("glowup", {})
        self._api_base: str = glowup_cfg.get(
            "api_base", "http://localhost:8420",
        )
        self._auth_token: str = glowup_cfg.get("auth_token", "")

    def _init_stt(self) -> None:
        """Initialize speech-to-text engine."""
        if self._config.get("mock_stt"):
            from voice.coordinator.stt import MockSpeechToText
            self._stt = MockSpeechToText()
            logger.info("Using MOCK STT (type transcription manually)")
        else:
            stt_cfg: dict[str, Any] = self._config.get("stt", {})
            from voice.coordinator.stt import SpeechToText
            self._stt = SpeechToText(
                model_size=stt_cfg.get("model_size", "base.en"),
                device=stt_cfg.get("device", "cpu"),
                compute_type=stt_cfg.get("compute_type", "int8"),
            )

    def _init_intent(self) -> None:
        """Initialize intent parser."""
        if self._config.get("mock_intent"):
            from voice.coordinator.intent import MockIntentParser
            self._intent = MockIntentParser()
            logger.info("Using MOCK intent parser")
        else:
            intent_cfg: dict[str, Any] = self._config.get("intent", {})
            from voice.coordinator.intent import IntentParser
            self._intent = IntentParser(
                model=intent_cfg.get("ollama_model", "llama3.2:3b"),
                ollama_host=intent_cfg.get(
                    "ollama_host", "http://localhost:11434",
                ),
                timeout=intent_cfg.get("timeout_seconds", C.INTENT_TIMEOUT_S),
                max_retries=intent_cfg.get(
                    "max_retries", C.INTENT_MAX_RETRIES,
                ),
            )
            # Initial capabilities refresh.
            if self._auth_token:
                self._intent.refresh_capabilities(
                    self._api_base, self._auth_token,
                )

    def _init_executor(self) -> None:
        """Initialize GlowUp API executor."""
        from voice.coordinator.executor import GlowUpExecutor
        chat_cfg: dict[str, Any] = self._config.get("chat", {})
        intent_cfg: dict[str, Any] = self._config.get("intent", {})
        self._executor = GlowUpExecutor(
            api_base=self._api_base,
            auth_token=self._auth_token,
            chat_model=chat_cfg.get("model", "llama3.1:8b"),
            ollama_host=intent_cfg.get(
                "ollama_host", "http://localhost:11434",
            ),
        )

    def _init_tts(self) -> None:
        """Initialize text-to-speech engine."""
        tts_cfg: dict[str, Any] = self._config.get("tts", {})
        from voice.coordinator.tts import TextToSpeech
        self._tts = TextToSpeech(
            voice_model=tts_cfg.get("voice_model"),
            voice_name=tts_cfg.get("voice_name"),
        )

    def _init_local_speaker(self) -> None:
        """Initialize persistent piper for rooms with speaker='local'.

        Only starts if at least one room is configured for local speech.
        The piper process stays warm for sub-second inference.
        """
        has_local: bool = any(
            cfg.get("speaker") == "local"
            for cfg in self._room_speakers.values()
        )
        if not has_local:
            return

        try:
            from voice.speaker.daemon import SpeakerDaemon
            model: str = self._config.get(
                "piper_model",
                os.path.expanduser("~/models/en_US-ryan-low.onnx"),
            )
            piper_bin: str = self._config.get(
                "piper_bin",
                os.path.expanduser("~/venv/bin/piper"),
            )
            speaker: SpeakerDaemon = SpeakerDaemon(
                piper_model=model,
                piper_bin=piper_bin,
            )
            if speaker._init_piper():
                self._local_speaker = speaker
                logger.info("Local speaker initialized (piper + sounddevice)")
            else:
                logger.warning("Local speaker piper init failed")
        except Exception as exc:
            logger.error("Local speaker init failed: %s", exc)

    def _speak_for_room(self, room: str, text: str) -> None:
        """Route TTS to the correct speaker for a room.

        Checks room_speakers config to determine output method.
        Falls back to satellite if room is not configured.

        Args:
            room: Room name (from satellite).
            text: Text to speak.
        """
        cfg: dict[str, Any] = self._room_speakers.get(room, {})
        speaker_type: str = cfg.get("speaker", "satellite")

        if speaker_type == "local" and self._local_speaker is not None:
            try:
                self._local_speaker._speak(text)
                logger.info("[%s] Spoke locally: '%s'", room, text[:40])
            except Exception as exc:
                logger.error("[%s] Local speak failed: %s", room, exc)
                self._publish_tts_text(room, text)

        elif speaker_type == "mqtt":
            topic: str = cfg.get("topic", "glowup/tts/speak")
            payload: str = json.dumps({"text": text})
            if self._mqtt_client is not None:
                self._mqtt_client.publish(topic, payload, qos=1)
                logger.info("[%s] TTS → %s: '%s'", room, topic, text[:40])

        else:
            self._publish_tts_text(room, text)

    def _init_player(self) -> None:
        """Initialize AirPlay audio player for TTS responses."""
        airplay_cfg: dict[str, Any] = self._config.get("airplay", {})
        if not airplay_cfg.get("enabled", False):
            logger.info("AirPlay player disabled (no config)")
            return

        try:
            from voice.coordinator.airplay import AirPlayPlayer
            self._player = AirPlayPlayer(
                room_map=airplay_cfg.get("room_map", {}),
                default_device=airplay_cfg.get("default_device"),
            )
        except ImportError:
            logger.warning("pyatv not installed — AirPlay disabled")
        except Exception as exc:
            logger.error("AirPlay init failed: %s", exc)

    def _notify_playback(self, room: str, playing: bool) -> None:
        """Publish playback state so satellites suppress wake detection.

        Args:
            room:    Room name (satellites filter by their own room).
            playing: True when TTS audio is about to play, False when done.
        """
        if self._mqtt_client is None:
            return

        payload: str = json.dumps({
            "room": room,
            "playing": playing,
            "timestamp": time.time(),
        })
        self._mqtt_client.publish(
            C.TOPIC_PLAYBACK, payload, qos=0,
        )
        logger.debug(
            "[%s] Playback %s", room, "started" if playing else "stopped",
        )

    def _publish_tts_text(self, room: str, text: str) -> None:
        """Publish TTS text so satellites can speak it locally.

        Args:
            room: Target room (satellites filter by their own room).
            text: Text to synthesize and speak.
        """
        if self._mqtt_client is None:
            return

        payload: str = json.dumps({
            "room": room,
            "text": text,
            "timestamp": time.time(),
        })
        self._mqtt_client.publish(
            C.TOPIC_TTS_TEXT, payload, qos=0,
        )
        logger.info("[%s] Published TTS text: '%s'", room, text[:60])

    def _init_mqtt(self) -> None:
        """Connect to MQTT broker and subscribe to utterance topic."""
        if mqtt is None:
            raise ImportError("paho-mqtt not installed")

        client_id: str = f"coordinator_{int(time.time())}"
        if _PAHO_V2:
            self._mqtt_client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            self._mqtt_client = mqtt.Client(client_id=client_id)

        self._mqtt_client.on_message = self._on_message
        self._mqtt_client.on_connect = self._on_connect
        self._mqtt_client.connect(self._mqtt_broker, self._mqtt_port)
        self._mqtt_client.loop_start()
        logger.info(
            "MQTT connected: %s:%d",
            self._mqtt_broker, self._mqtt_port,
        )

    def _on_connect(self, client: Any, userdata: Any, *args: Any) -> None:
        """Subscribe to utterance topic on connect/reconnect."""
        client.subscribe(C.TOPIC_UTTERANCE, qos=1)
        client.subscribe(f"{C.TOPIC_STATUS_PREFIX}/#", qos=0)
        logger.info("Subscribed to %s", C.TOPIC_UTTERANCE)

    def _on_message(
        self, client: Any, userdata: Any, message: Any,
    ) -> None:
        """Handle incoming MQTT messages.

        Utterance messages are dispatched to the worker pool.
        Status messages are logged.
        """
        topic: str = message.topic

        if topic == C.TOPIC_UTTERANCE:
            # Dispatch to worker — don't block the MQTT thread.
            if self._pool is not None:
                self._pool.submit(self._process_message, message.payload)
            else:
                logger.warning("Worker pool not ready — dropping utterance")

        elif topic.startswith(C.TOPIC_STATUS_PREFIX):
            try:
                status = json.loads(message.payload)
                logger.debug(
                    "Heartbeat from %s", status.get("room", "?"),
                )
            except Exception:
                pass

    def _process_message(self, payload: bytes) -> None:
        """Decode and process a single utterance message.

        Runs in a worker thread from the pool.

        Args:
            payload: Raw MQTT message payload.
        """
        try:
            header, pcm = decode(payload)
        except ProtocolError as exc:
            logger.warning("Protocol decode error: %s", exc)
            return

        room: str = header.get("room", "unknown")
        logger.info(
            "[%s] Received utterance: %.1fs audio (wake=%.2f)",
            room,
            len(pcm) / (header.get("sample_rate", 16000) * 2),
            header.get("wake_score", 0),
        )

        # Suppress wake detection for the entire pipeline duration,
        # not just during TTS playback.  Prevents the satellite from
        # re-triggering on its own HomePod output during slow queries.
        self._notify_playback(room, True)

        # Refresh capabilities if stale.
        if (hasattr(self._intent, "should_refresh")
                and self._intent.should_refresh()
                and self._auth_token):
            self._intent.refresh_capabilities(
                self._api_base, self._auth_token,
            )

        try:
            result = process_utterance(
                room=room,
                pcm=pcm,
                meta=header,
                stt=self._stt,
                intent_parser=self._intent,
                executor=self._executor,
                tts=self._tts,
                player=self._player,
                playback_notifier=self._notify_playback,
                tts_text_publisher=self._speak_for_room,
            )
        finally:
            # Always re-enable wake detection after pipeline completes,
            # even if the pipeline crashed.
            self._notify_playback(room, False)

        logger.info(
            "[%s] Pipeline: '%s' → %s (%.0fms)",
            room,
            result.get("text", ""),
            result.get("intent", {}).get("action", "?"),
            result.get("latency_ms", 0),
        )

    def start(self) -> None:
        """Start the coordinator daemon.

        Blocks until stopped via signal or ``stop()``.
        """
        self._running = True

        # Initialize components.
        logger.info("Initializing pipeline components...")
        self._init_stt()
        self._init_intent()
        self._init_executor()
        self._init_tts()
        self._init_player()
        self._init_local_speaker()

        # Give executor access to TTS for voice-change commands.
        self._executor.set_tts(self._tts)

        # Worker pool.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="voice-worker",
        )

        # MQTT.
        self._init_mqtt()

        logger.info(
            "Coordinator running (workers=%d, broker=%s:%d)",
            self._max_workers, self._mqtt_broker, self._mqtt_port,
        )

        # Block main thread until stopped.
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the coordinator and release resources."""
        self._running = False

        if self._mqtt_client is not None:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None

        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

        # Close AirPlay connections cleanly so HomePods don't hold
        # stale sessions.
        if hasattr(self._player, "close"):
            try:
                self._player.close()
            except Exception as exc:
                logger.debug("Player close failed: %s", exc)

        logger.info("Coordinator stopped")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args and run the coordinator daemon."""
    parser = argparse.ArgumentParser(
        description="GlowUp Voice Coordinator",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to voice_coordinator.json config file",
    )
    parser.add_argument(
        "--broker", type=str, default=None,
        help="MQTT broker address",
    )
    parser.add_argument(
        "--api-base", type=str, default=None,
        help="GlowUp server URL (e.g., http://localhost:8420)",
    )
    parser.add_argument(
        "--token", type=str, default=None,
        help="GlowUp auth token",
    )
    parser.add_argument(
        "--airplay-device", type=str, default=None,
        help="Default AirPlay device name for TTS playback",
    )
    parser.add_argument(
        "--mock-stt", action="store_true",
        help="Use mock STT (type transcription manually)",
    )
    parser.add_argument(
        "--mock-intent", action="store_true",
        help="Use mock intent parser",
    )
    args = parser.parse_args()

    # Logging.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load config.
    config: dict[str, Any] = {}
    if args.config and os.path.exists(args.config):
        with open(args.config, "r") as f:
            config = json.load(f)

    # CLI overrides.
    if args.broker:
        config.setdefault("mqtt", {})["broker"] = args.broker
    if args.api_base:
        config.setdefault("glowup", {})["api_base"] = args.api_base
    if args.token:
        config.setdefault("glowup", {})["auth_token"] = args.token
    if args.mock_stt:
        config["mock_stt"] = True
    if args.mock_intent:
        config["mock_intent"] = True
    if args.airplay_device:
        config["airplay"] = {
            "enabled": True,
            "default_device": args.airplay_device,
            "room_map": {},
        }

    # Signal handling.
    daemon = CoordinatorDaemon(config)

    def shutdown(sig: int, frame: Any) -> None:
        logger.info("Received signal %d", sig)
        daemon.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    daemon.start()


if __name__ == "__main__":
    main()
