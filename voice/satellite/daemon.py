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
import subprocess
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
        # _sample_rate is the TARGET rate for the pipeline (16 kHz).
        # _hw_rate is the actual hardware capture rate (may differ).
        # If they differ, PCM is resampled before publishing.
        audio_cfg: dict[str, Any] = config.get("audio", {})
        self._sample_rate: int = audio_cfg.get("sample_rate", C.SAMPLE_RATE)
        self._hw_rate: int = self._sample_rate  # Updated in _init_audio().
        self._chunk_samples: int = audio_cfg.get(
            "chunk_size", C.CHUNK_SAMPLES,
        )
        self._device_name: Optional[str] = audio_cfg.get("device_name")
        self._needs_resample: bool = False

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

        # Playback suppression — mutes wake detection while the
        # coordinator is playing TTS audio through a nearby speaker,
        # preventing the mic from re-triggering on its own output.
        self._playback_suppressed: bool = False

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

        self._mqtt_client.on_message = self._on_playback_message
        self._mqtt_client.connect(self._mqtt_broker, self._mqtt_port)
        self._mqtt_client.subscribe(C.TOPIC_PLAYBACK, qos=0)
        self._mqtt_client.loop_start()
        logger.info(
            "MQTT connected: %s:%d as %s",
            self._mqtt_broker, self._mqtt_port, client_id,
        )

    def _on_playback_message(
        self, client: Any, userdata: Any, msg: Any,
    ) -> None:
        """Handle playback state messages from the coordinator.

        Suppresses wake detection while TTS audio is playing in
        this satellite's room, preventing echo re-triggers.

        Args:
            client:   MQTT client instance.
            userdata: User data (unused).
            msg:      MQTT message with JSON payload.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)
            room: str = data.get("room", "")
            playing: bool = data.get("playing", False)

            # Only suppress if the playback is in our room.
            if room == self._room:
                self._playback_suppressed = playing
                if playing:
                    logger.info("Playback started — wake suppressed")
                else:
                    logger.info("Playback ended — wake re-enabled")
        except Exception as exc:
            logger.debug("Playback message parse error: %s", exc)

    def _init_audio(self) -> None:
        """Open the audio input stream.

        On macOS: uses PyAudio with sample rate negotiation.
        On Linux: uses ALSA arecord via subprocess (PyAudio's device
        enumeration is broken for some USB mics on Linux — the Shure
        MV88+ reports maxInputChannels=0 even though ALSA sees it).
        """
        import platform
        self._use_alsa: bool = (
            platform.system() == "Linux"
            and not _HAS_PYAUDIO
        ) or (
            platform.system() == "Linux"
            and self._config.get("audio", {}).get("use_alsa", True)
        )

        if self._use_alsa:
            self._init_audio_alsa()
        else:
            self._init_audio_pyaudio()

    def _init_audio_alsa(self) -> None:
        """Open audio via ALSA arecord subprocess.

        Finds the first USB capture device and opens a continuous
        arecord process that writes raw PCM to stdout.
        """
        import subprocess as sp

        # Find the ALSA capture device.
        alsa_device: Optional[str] = self._config.get(
            "audio", {},
        ).get("alsa_device")

        if alsa_device is None:
            # Auto-detect: parse arecord -l for USB audio.
            try:
                result = sp.run(
                    ["arecord", "-l"],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.splitlines():
                    if "card" in line and "USB" in line:
                        # Extract card number.
                        card = line.split(":")[0].split()[-1]
                        alsa_device = f"plughw:{card},0"
                        logger.info(
                            "Auto-detected ALSA capture: %s (%s)",
                            alsa_device, line.strip(),
                        )
                        break
            except Exception as exc:
                logger.error("arecord -l failed: %s", exc)

        if alsa_device is None:
            raise RuntimeError("No ALSA capture device found")

        # Capture at target rate directly — ALSA plughw handles
        # resampling in the kernel.
        self._hw_rate = self._sample_rate
        self._needs_resample = False
        self._hw_chunk = self._chunk_samples

        self._alsa_proc: Optional[sp.Popen] = sp.Popen(
            [
                "arecord",
                "-D", alsa_device,
                "-f", "S16_LE",
                "-r", str(self._sample_rate),
                "-c", "1",
                "-t", "raw",
                "-q",  # Quiet — no status output.
            ],
            stdout=sp.PIPE,
            stderr=sp.DEVNULL,
        )
        # Wrap in a stream-like object for compatibility.
        self._stream = self._alsa_proc.stdout  # type: ignore[assignment]

        logger.info(
            "ALSA audio stream opened: device=%s rate=%d",
            alsa_device, self._sample_rate,
        )

    def _init_audio_pyaudio(self) -> None:
        """Open audio via PyAudio (macOS / compatible Linux)."""
        if not _HAS_PYAUDIO:
            raise ImportError("pyaudio not installed")

        self._pa = pyaudio.PyAudio()
        device_index = find_device_index(self._pa, self._device_name)

        # If no device specified, find the first available input device.
        if device_index is None:
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if info["maxInputChannels"] > 0:
                    device_index = i
                    logger.info(
                        "Auto-selected input device [%d] %s",
                        i, info["name"],
                    )
                    break
            if device_index is None:
                raise RuntimeError("No audio input device found")

        # Try target rate first; fall back to native rate.
        self._hw_rate = self._sample_rate
        try:
            self._pa.is_format_supported(
                self._sample_rate,
                input_device=device_index,
                input_channels=C.CHANNELS,
                input_format=pyaudio.paInt16,
            )
        except ValueError:
            info = self._pa.get_device_info_by_index(device_index)
            self._hw_rate = int(info["defaultSampleRate"])
            self._needs_resample = True
            logger.info(
                "Device does not support %d Hz — capturing at %d Hz "
                "(will resample to %d Hz)",
                self._sample_rate, self._hw_rate, self._sample_rate,
            )

        hw_chunk: int = int(
            self._chunk_samples * self._hw_rate / self._sample_rate,
        )
        self._hw_chunk = hw_chunk

        self._stream = self._pa.open(
            rate=self._hw_rate,
            channels=C.CHANNELS,
            format=pyaudio.paInt16,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=hw_chunk,
        )

        logger.info(
            "PyAudio stream opened: hw_rate=%d target_rate=%d "
            "chunk=%d device=%s resample=%s",
            self._hw_rate, self._sample_rate, hw_chunk,
            self._device_name or "default",
            self._needs_resample,
        )
        self._alsa_proc = None

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
                    "model_path", "/home/pi/models/hey_mashugenah.onnx",
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

    def _resample(self, pcm: bytes) -> bytes:
        """Downsample PCM from hardware rate to target rate.

        Uses linear interpolation via numpy.  Not audiophile-grade
        but sufficient for speech recognition.

        Args:
            pcm: Raw PCM at hardware sample rate.

        Returns:
            Resampled PCM at target sample rate.
        """
        if not self._needs_resample:
            return pcm

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        ratio: float = self._sample_rate / self._hw_rate
        new_len: int = int(len(samples) * ratio)
        indices = np.linspace(0, len(samples) - 1, new_len)
        resampled = np.interp(indices, np.arange(len(samples)), samples)
        return resampled.astype(np.int16).tobytes()

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
            "wake_score": float(wake_score),
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
                    chunk_bytes: int = self._hw_chunk * C.BYTES_PER_SAMPLE
                    if self._use_alsa:
                        raw = self._stream.read(chunk_bytes)  # type: ignore[union-attr]
                        if not raw:
                            logger.warning("ALSA stream ended")
                            break
                    else:
                        raw = self._stream.read(
                            self._hw_chunk if self._needs_resample
                            else self._chunk_samples,
                            exception_on_overflow=False,
                        )
                except Exception as exc:
                    logger.warning("Audio read error: %s", exc)
                    time.sleep(0.1)
                    continue

                # Feed pre-wake ring buffer.
                self._capture.feed_ring(raw)

                # Skip wake detection while speaker is playing TTS
                # to prevent the mic from re-triggering on its own output.
                if self._playback_suppressed:
                    continue

                # Feed wake detector.
                audio_array: np.ndarray = np.frombuffer(
                    raw, dtype=np.int16,
                )
                score: Optional[float] = self._wake.feed(audio_array)

                if score is not None:
                    # Wake word detected — capture utterance.
                    logger.info("Capturing utterance...")
                    pcm: Optional[bytes] = self._capture.capture(
                        self._stream, use_alsa=self._use_alsa,
                    )

                    if pcm is not None:
                        pcm = self._resample(pcm)
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

        if hasattr(self, "_alsa_proc") and self._alsa_proc is not None:
            self._alsa_proc.terminate()
            try:
                self._alsa_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("arecord did not exit — killing")
                self._alsa_proc.kill()
                self._alsa_proc.wait(timeout=2)
            self._alsa_proc = None
            self._stream = None
        elif self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception as exc:
                logger.debug("Stream cleanup error: %s", exc)
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
