"""GlowUp Voice Satellite daemon.

Listens for the wake word on a microphone, captures the following
utterance, and publishes it to MQTT for the coordinator to process.

For development, use ``--mock-wake`` to trigger via Enter key
instead of a trained wake word model.

Usage::

    # With trained model:
    python -m voice.satellite.daemon --config /etc/glowup/voice_satellite.json

    # Development mode (laptop mic, Enter to trigger):
    python -m voice.satellite.daemon --mock-wake --room conway

    # Show available audio devices:
    python -m voice.satellite.daemon --list-devices
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Optional, Union

import numpy as np

from voice import constants as C
from voice.protocol import encode
from voice.satellite.capture import UtteranceCapture

logger: logging.Logger = logging.getLogger("glowup.voice.satellite")

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------

try:
    import pyaudio
    _HAS_PYAUDIO: bool = True
except ImportError:
    pyaudio = None  # type: ignore[assignment]
    _HAS_PYAUDIO = False

try:
    import paho.mqtt.client as mqtt
    # Detect paho v2 vs v1.
    _PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:
    mqtt = None  # type: ignore[assignment]
    _PAHO_V2 = False

# ---------------------------------------------------------------------------
# Audio device helpers
# ---------------------------------------------------------------------------

def list_audio_devices() -> None:
    """Print available audio input devices and exit."""
    if not _HAS_PYAUDIO:
        print("pyaudio not installed — pip install pyaudio")
        sys.exit(1)

    pa = pyaudio.PyAudio()
    print("\nAvailable audio input devices:\n")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            print(
                f"  [{i}] {info['name']}  "
                f"(channels={info['maxInputChannels']}, "
                f"rate={int(info['defaultSampleRate'])})"
            )
    pa.terminate()


def find_device_index(
    pa: "pyaudio.PyAudio", device_name: Optional[str],
) -> Optional[int]:
    """Find audio device index by name substring.

    Args:
        pa:          PyAudio instance.
        device_name: Substring to match against device names.
                     None means use the system default.

    Returns:
        Device index, or None for system default.
    """
    if device_name is None:
        return None

    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if (device_name.lower() in info["name"].lower()
                and info["maxInputChannels"] > 0):
            logger.info("Using audio device [%d] %s", i, info["name"])
            return i

    logger.warning(
        "Audio device '%s' not found — using system default",
        device_name,
    )
    return None


# ---------------------------------------------------------------------------
# Satellite daemon
# ---------------------------------------------------------------------------

class SatelliteDaemon:
    """Voice satellite: wake word detection + utterance capture + MQTT.

    Args:
        config: Configuration dict (from JSON file or CLI args).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize the satellite daemon."""
        self._config: dict[str, Any] = config
        self._room: str = config.get("room", "unknown")
        self._running: bool = False
        self._mock_wake: bool = config.get("mock_wake", False)

        # MQTT config.
        mqtt_cfg: dict[str, Any] = config.get("mqtt", {})
        self._mqtt_broker: str = mqtt_cfg.get("broker", "localhost")
        self._mqtt_port: int = mqtt_cfg.get("port", 1883)
        self._mqtt_client: Optional[Any] = None

        # Audio config.
        audio_cfg: dict[str, Any] = config.get("audio", {})
        self._sample_rate: int = audio_cfg.get("sample_rate", C.SAMPLE_RATE)
        self._chunk_samples: int = audio_cfg.get(
            "chunk_size", C.CHUNK_SAMPLES,
        )
        self._device_name: Optional[str] = audio_cfg.get("device_name")

        # Capture config.
        cap_cfg: dict[str, Any] = config.get("capture", {})
        self._capture = UtteranceCapture(
            sample_rate=self._sample_rate,
            chunk_samples=self._chunk_samples,
            max_seconds=cap_cfg.get("max_seconds", C.MAX_UTTERANCE_S),
            silence_timeout=cap_cfg.get(
                "silence_timeout", C.SILENCE_TIMEOUT_S,
            ),
            silence_rms=cap_cfg.get("silence_rms", C.SILENCE_RMS_THRESHOLD),
            min_seconds=cap_cfg.get("min_seconds", C.MIN_UTTERANCE_S),
            pre_wake_seconds=cap_cfg.get(
                "pre_wake_buffer_ms", C.PRE_WAKE_BUFFER_S * 1000,
            ) / 1000.0,
        )

        # Wake detector — real or mock.
        self._wake: Any = None  # Set in start().

        # PyAudio resources.
        self._pa: Optional["pyaudio.PyAudio"] = None
        self._stream: Optional["pyaudio.Stream"] = None

    def _init_mqtt(self) -> None:
        """Connect to the MQTT broker."""
        if mqtt is None:
            raise ImportError("paho-mqtt not installed")

        client_id: str = f"satellite_{self._room}_{int(time.time())}"
        if _PAHO_V2:
            self._mqtt_client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            self._mqtt_client = mqtt.Client(client_id=client_id)

        self._mqtt_client.connect(self._mqtt_broker, self._mqtt_port)
        self._mqtt_client.loop_start()
        logger.info(
            "MQTT connected: %s:%d as %s",
            self._mqtt_broker, self._mqtt_port, client_id,
        )

    def _init_audio(self) -> None:
        """Open the audio input stream."""
        if not _HAS_PYAUDIO:
            raise ImportError("pyaudio not installed")

        self._pa = pyaudio.PyAudio()
        device_index = find_device_index(self._pa, self._device_name)

        self._stream = self._pa.open(
            rate=self._sample_rate,
            channels=C.CHANNELS,
            format=pyaudio.paInt16,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=self._chunk_samples,
        )
        logger.info(
            "Audio stream opened: rate=%d chunk=%d device=%s",
            self._sample_rate, self._chunk_samples,
            self._device_name or "default",
        )

    def _init_wake(self) -> None:
        """Initialize the wake word detector."""
        if self._mock_wake:
            from voice.satellite.wake import MockWakeDetector
            self._wake = MockWakeDetector(
                cooldown=self._config.get("wake", {}).get(
                    "cooldown_seconds", C.COOLDOWN_S,
                ),
            )
            logger.info("Using MOCK wake detector (press Enter to trigger)")
            # Start keyboard listener thread.
            t = threading.Thread(
                target=self._keyboard_listener,
                daemon=True,
                name="wake-keyboard",
            )
            t.start()
        else:
            from voice.satellite.wake import WakeDetector
            wake_cfg: dict[str, Any] = self._config.get("wake", {})
            self._wake = WakeDetector(
                model_path=wake_cfg.get(
                    "model_path", "/home/pi/models/hey_glowup.onnx",
                ),
                threshold=wake_cfg.get("threshold", C.WAKE_THRESHOLD),
                vad_threshold=wake_cfg.get(
                    "vad_threshold", C.VAD_THRESHOLD,
                ),
                confidence_window=wake_cfg.get(
                    "confidence_window", C.CONFIDENCE_WINDOW,
                ),
                cooldown=wake_cfg.get("cooldown_seconds", C.COOLDOWN_S),
            )

    def _keyboard_listener(self) -> None:
        """Listen for Enter key presses to trigger mock wake word."""
        while self._running:
            try:
                input()  # Blocks until Enter.
                if self._running and self._wake is not None:
                    self._wake.trigger()
                    logger.info("[MOCK] Wake triggered via keyboard")
            except EOFError:
                break

    def _publish_utterance(
        self, pcm: bytes, wake_score: float,
    ) -> None:
        """Publish captured utterance to MQTT.

        Args:
            pcm:        Raw PCM audio bytes.
            wake_score: Confidence score from wake word detection.
        """
        if self._mqtt_client is None:
            logger.warning("MQTT not connected — dropping utterance")
            return

        header: dict[str, Any] = {
            "room": self._room,
            "sample_rate": self._sample_rate,
            "channels": C.CHANNELS,
            "bit_depth": C.BIT_DEPTH,
            "timestamp": time.time(),
            "wake_score": wake_score,
        }

        payload: bytes = encode(header, pcm)

        self._mqtt_client.publish(
            C.TOPIC_UTTERANCE, payload, qos=1,
        )

        duration: float = len(pcm) / (self._sample_rate * C.BYTES_PER_SAMPLE)
        logger.info(
            "Published %.1fs utterance (%d bytes) from %s",
            duration, len(payload), self._room,
        )

    def _publish_heartbeat(self) -> None:
        """Publish a heartbeat status message."""
        if self._mqtt_client is None:
            return

        status: dict[str, Any] = {
            "room": self._room,
            "timestamp": time.time(),
            "mock_wake": self._mock_wake,
        }
        topic: str = f"{C.TOPIC_STATUS_PREFIX}/{self._room}"
        self._mqtt_client.publish(
            topic,
            json.dumps(status).encode("utf-8"),
            qos=0,
        )

    def start(self) -> None:
        """Start the satellite daemon.

        Blocks until stopped via SIGTERM/SIGINT or ``stop()`` call.
        """
        self._running = True

        self._init_mqtt()
        self._init_audio()
        self._init_wake()

        logger.info(
            "Satellite [%s] listening%s...",
            self._room,
            " (mock wake — press Enter)" if self._mock_wake else "",
        )

        last_heartbeat: float = 0.0
        assert self._stream is not None

        try:
            while self._running:
                # Read audio chunk.
                try:
                    raw: bytes = self._stream.read(
                        self._chunk_samples,
                        exception_on_overflow=False,
                    )
                except Exception as exc:
                    logger.warning("Audio read error: %s", exc)
                    time.sleep(0.1)
                    continue

                # Feed pre-wake ring buffer.
                self._capture.feed_ring(raw)

                # Feed wake detector.
                audio_array: np.ndarray = np.frombuffer(
                    raw, dtype=np.int16,
                )
                score: Optional[float] = self._wake.feed(audio_array)

                if score is not None:
                    # Wake word detected — capture utterance.
                    logger.info("Capturing utterance...")
                    pcm: Optional[bytes] = self._capture.capture(
                        self._stream,
                    )

                    if pcm is not None:
                        self._publish_utterance(pcm, score)
                    else:
                        logger.debug("Capture rejected (too short)")

                    # Reset wake detector state.
                    self._wake.reset()

                # Periodic heartbeat.
                now: float = time.time()
                if now - last_heartbeat >= C.HEARTBEAT_INTERVAL_S:
                    self._publish_heartbeat()
                    last_heartbeat = now

        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the satellite daemon and release resources."""
        self._running = False

        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

        if self._mqtt_client is not None:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            self._mqtt_client = None

        logger.info("Satellite [%s] stopped", self._room)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args and run the satellite daemon."""
    parser = argparse.ArgumentParser(
        description="GlowUp Voice Satellite",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to voice_satellite.json config file",
    )
    parser.add_argument(
        "--room", type=str, default=None,
        help="Room name (overrides config)",
    )
    parser.add_argument(
        "--broker", type=str, default=None,
        help="MQTT broker address (overrides config)",
    )
    parser.add_argument(
        "--mock-wake", action="store_true",
        help="Use keyboard trigger instead of wake word model",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="List audio input devices and exit",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        sys.exit(0)

    # Set up logging.
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
        logger.info("Loaded config from %s", args.config)

    # CLI overrides.
    if args.room:
        config["room"] = args.room
    if args.broker:
        config.setdefault("mqtt", {})["broker"] = args.broker
    if args.mock_wake:
        config["mock_wake"] = True

    # Default room from hostname if not specified.
    if "room" not in config:
        import socket
        config["room"] = socket.gethostname().split(".")[0].lower()

    # Signal handling.
    daemon = SatelliteDaemon(config)

    def shutdown(sig: int, frame: Any) -> None:
        logger.info("Received signal %d — shutting down", sig)
        daemon.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    daemon.start()


if __name__ == "__main__":
    main()
