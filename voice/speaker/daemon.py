"""MQTT-driven TTS speaker daemon.

Subscribes to ``glowup/tts/speak`` and speaks received text through
local audio using a persistent piper process.  Piper's model is loaded
once at startup (~10 seconds) and stays warm — subsequent requests
produce audio in under a second.

Any GlowUp satellite (or anything) publishes text to the topic and
this daemon plays it through the host's speakers.  Designed to run
on Daedalus (Mac Studio with headphones/speakers connected).

MQTT message format::

    {"text": "It is 8:35 PM."}

Optional fields::

    {"text": "...", "priority": 1}

Usage::

    python -m voice.speaker.daemon --broker <hub-broker> --model ~/models/en_US-ryan-low.onnx
    python -m voice.speaker.daemon --config ~/speaker_config.json

Config file::

    {
        "broker": "<hub-broker>",
        "port": 1883,
        "piper_model": "~/models/en_US-ryan-low.onnx",
        "topic": "glowup/tts/speak"
    }
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import json
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.voice.speaker")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT topic for TTS requests.
DEFAULT_TOPIC: str = "glowup/tts/speak"

# Default MQTT broker.  Resolved at import time from the same site
# config every other GlowUp tool reads (no household IP in source);
# empty string when neither site.json nor env supplies one — the
# operator must then pass --broker explicitly.
from glowup_site import site as _site
DEFAULT_BROKER: str = _site.get("hub_broker") or ""

# Default MQTT port.
DEFAULT_PORT: int = 1883

# Default piper model path.
DEFAULT_MODEL: str = os.path.expanduser("~/models/en_US-ryan-low.onnx")

# Default piper binary — look in venv first.
DEFAULT_PIPER_BIN: str = os.path.expanduser("~/venv/bin/piper")

# Read timeout for piper stdout (seconds).
# After this silence, we assume the utterance is complete.
PIPER_READ_TIMEOUT: float = 0.3

# Maximum raw PCM chunk size per read.
PIPER_CHUNK_SIZE: int = 65536

# Playback timeout (seconds).
PLAYBACK_TIMEOUT: float = 30.0

# Log format.
LOG_FORMAT: str = "%(asctime)s %(name)s %(levelname)s %(message)s"


# ---------------------------------------------------------------------------
# SpeakerDaemon
# ---------------------------------------------------------------------------

class SpeakerDaemon:
    """MQTT-driven TTS speaker using persistent piper process.

    Args:
        broker:      MQTT broker address.
        port:        MQTT broker port.
        topic:       MQTT topic to subscribe to.
        piper_model: Path to piper ONNX model.
        piper_bin:   Path to piper binary.
    """

    def __init__(
        self,
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_PORT,
        topic: str = DEFAULT_TOPIC,
        piper_model: str = DEFAULT_MODEL,
        piper_bin: str = DEFAULT_PIPER_BIN,
    ) -> None:
        """Initialize the speaker daemon."""
        self._broker: str = broker
        self._port: int = port
        self._topic: str = topic
        self._piper_model: str = os.path.expanduser(piper_model)
        self._piper_bin: str = os.path.expanduser(piper_bin)
        self._piper_proc: Optional[subprocess.Popen] = None
        self._piper_rate: int = 22050
        self._piper_lock: threading.Lock = threading.Lock()
        self._running: bool = False
        self._client: Any = None

    def start(self) -> None:
        """Start piper, connect to MQTT, and block until stopped."""
        self._running = True

        # Signal handling for graceful shutdown.
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Start piper process.
        if not self._init_piper():
            logger.error("Cannot start without piper — exiting")
            sys.exit(1)

        # Connect to MQTT.
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error("paho-mqtt not installed")
            sys.exit(1)

        paho_v2: bool = hasattr(mqtt, "CallbackAPIVersion")
        client_id: str = f"glowup-speaker-{int(time.time())}"

        if paho_v2:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            self._client = mqtt.Client(client_id=client_id)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        self._client.connect(self._broker, self._port)
        logger.info(
            "Speaker daemon started — broker=%s:%d topic=%s",
            self._broker, self._port, self._topic,
        )

        # Block on MQTT loop.
        self._client.loop_forever()

    def _signal_handler(self, sig: int, frame: Any) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info("Received signal %d — shutting down", sig)
        self._running = False
        if self._client is not None:
            self._client.disconnect()
        self._cleanup_piper()

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, *args: Any) -> None:
        """Subscribe to TTS topic on connect."""
        client.subscribe(self._topic, qos=1)
        logger.info("Subscribed to %s", self._topic)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Handle incoming TTS request."""
        try:
            data: dict[str, Any] = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError):
            # Plain text fallback — just the text to speak.
            text: str = msg.payload.decode("utf-8", errors="replace").strip()
            if text:
                self._speak(text)
            return

        text = data.get("text", "").strip()
        if not text:
            return

        logger.info("TTS request: '%s'", text[:60])
        self._speak(text)

    # ------------------------------------------------------------------
    # Piper TTS
    # ------------------------------------------------------------------

    def _init_piper(self) -> bool:
        """Start the persistent piper process.

        Returns:
            True if piper started successfully.
        """
        if not os.path.exists(self._piper_bin):
            logger.error("Piper binary not found: %s", self._piper_bin)
            return False
        if not os.path.exists(self._piper_model):
            logger.error("Piper model not found: %s", self._piper_model)
            return False

        # Read sample rate from model config.
        model_json: str = self._piper_model + ".json"
        if os.path.exists(model_json):
            with open(model_json, "r") as f:
                mcfg: dict[str, Any] = json.load(f)
            self._piper_rate = mcfg.get("audio", {}).get(
                "sample_rate", 22050,
            )

        try:
            self._piper_proc = subprocess.Popen(
                [self._piper_bin, "--model", self._piper_model,
                 "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "Piper started: model=%s rate=%d",
                os.path.basename(self._piper_model), self._piper_rate,
            )
            return True
        except Exception as exc:
            logger.error("Failed to start piper: %s", exc)
            return False

    def _cleanup_piper(self) -> None:
        """Terminate the piper process."""
        if self._piper_proc is not None:
            try:
                self._piper_proc.terminate()
                self._piper_proc.wait(timeout=5)
            except Exception as exc:
                logger.debug("Error terminating piper process: %s", exc)
            self._piper_proc = None

    def _speak(self, text: str) -> None:
        """Synthesize text via piper and play through sounddevice.

        Args:
            text: Text to speak.
        """
        with self._piper_lock:
            if self._piper_proc is None or self._piper_proc.poll() is not None:
                logger.warning("Piper died — restarting")
                self._cleanup_piper()
                if not self._init_piper():
                    logger.error("Piper restart failed")
                    return

            try:
                # Write text line to piper stdin.
                line: bytes = (text.strip() + "\n").encode("utf-8")
                self._piper_proc.stdin.write(line)
                self._piper_proc.stdin.flush()

                # Read raw PCM from piper stdout until silence.
                chunks: list[bytes] = []
                fd: int = self._piper_proc.stdout.fileno()
                while True:
                    ready, _, _ = select.select(
                        [fd], [], [], PIPER_READ_TIMEOUT,
                    )
                    if ready:
                        data: bytes = os.read(fd, PIPER_CHUNK_SIZE)
                        if not data:
                            break
                        chunks.append(data)
                    else:
                        if chunks:
                            break

                if not chunks:
                    logger.warning("Piper produced no audio for: '%s'",
                                   text[:40])
                    return

                raw_audio: bytes = b"".join(chunks)
                self._play_sounddevice(raw_audio)
                logger.info("Spoke: '%s'", text[:60])

            except Exception as exc:
                logger.error("Speak failed: %s", exc)
                self._cleanup_piper()
                self._init_piper()

    def _play_sounddevice(self, raw_pcm: bytes) -> None:
        """Play raw 16-bit signed LE PCM via sounddevice.

        Args:
            raw_pcm: Raw PCM bytes from piper.
        """
        import numpy as np
        import sounddevice as sd

        # Piper outputs 16-bit signed LE mono.
        samples: np.ndarray = np.frombuffer(raw_pcm, dtype=np.int16)
        # Normalize to float32 [-1, 1] for sounddevice.
        audio: np.ndarray = samples.astype(np.float32) / 32768.0

        sd.play(audio, samplerate=self._piper_rate, blocking=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args and start the speaker daemon."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp TTS Speaker Daemon",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to speaker config JSON",
    )
    parser.add_argument(
        "--broker", default=None,
        help="MQTT broker address",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="MQTT broker port",
    )
    parser.add_argument(
        "--model", default=None,
        help="Path to piper ONNX model",
    )
    parser.add_argument(
        "--topic", default=None,
        help="MQTT topic to subscribe to",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args: argparse.Namespace = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=LOG_FORMAT,
    )

    # Load config file if provided.
    config: dict[str, Any] = {}
    if args.config:
        try:
            with open(args.config) as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load config: %s", exc)
            sys.exit(1)

    # CLI args override config.
    broker: str = args.broker or config.get("broker", DEFAULT_BROKER)
    port: int = args.port or config.get("port", DEFAULT_PORT)
    model: str = args.model or config.get("piper_model", DEFAULT_MODEL)
    topic: str = args.topic or config.get("topic", DEFAULT_TOPIC)

    daemon: SpeakerDaemon = SpeakerDaemon(
        broker=broker, port=port, topic=topic,
        piper_model=model,
    )
    daemon.start()


if __name__ == "__main__":
    main()
