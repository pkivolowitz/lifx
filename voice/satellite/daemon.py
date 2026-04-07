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

        # TTS output routing — "local" (default) speaks through piper/espeak
        # on this device, "mqtt" publishes text to a topic for a remote
        # speaker daemon (e.g. Daedalus) to play through its speakers.
        self._tts_output: str = config.get("tts_output", "local")
        self._tts_topic: str = config.get(
            "tts_topic", "glowup/tts/speak",
        )

        # Piper TTS — persistent process for low-latency local speech.
        # Only initialized when tts_output is "local".
        self._piper_proc: Optional[subprocess.Popen] = None
        self._piper_rate: str = "22050"
        self._piper_lock: threading.Lock = threading.Lock()
        # Track active aplay process for cancellation.
        self._aplay_proc: Optional[subprocess.Popen] = None
        self._aplay_lock: threading.Lock = threading.Lock()
        # Generation counter — incremented on each new TTS request.
        # After piper inference, if the counter has moved, the audio
        # is stale and should be discarded.
        self._tts_generation: int = 0
        if self._tts_output == "local":
            self._init_piper()

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
        # Suppression counter — incremented when local TTS starts,
        # decremented when it ends.  Wake detection is suppressed
        # when count > 0.  Using a counter instead of a boolean
        # prevents races between overlapping TTS threads.
        self._suppress_count: int = 0
        self._suppress_lock: threading.Lock = threading.Lock()

        # PyAudio resources.
        self._pa: Optional["pyaudio.PyAudio"] = None
        self._stream: Optional["pyaudio.Stream"] = None

    def _init_mqtt(self) -> None:
        """Connect to the MQTT broker.

        Raises:
            ImportError: paho-mqtt not installed.
            Exception: Broker unreachable.
        """
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

        self._mqtt_client.on_message = self._on_mqtt_message
        self._mqtt_client.connect(self._mqtt_broker, self._mqtt_port)
        self._mqtt_client.subscribe(C.TOPIC_PLAYBACK, qos=0)
        self._mqtt_client.subscribe(C.TOPIC_TTS_TEXT, qos=0)
        self._mqtt_client.subscribe(C.TOPIC_FLUSH, qos=1)
        self._mqtt_client.loop_start()
        logger.info(
            "MQTT connected: %s:%d as %s",
            self._mqtt_broker, self._mqtt_port, client_id,
        )

    def _on_mqtt_message(
        self, client: Any, userdata: Any, msg: Any,
    ) -> None:
        """Dispatch incoming MQTT messages by topic.

        Args:
            client:   MQTT client instance.
            userdata: User data (unused).
            msg:      MQTT message.
        """
        if msg.topic == C.TOPIC_PLAYBACK:
            self._on_playback_message(msg)
        elif msg.topic == C.TOPIC_TTS_TEXT:
            self._on_tts_text_message(msg)
        elif msg.topic == C.TOPIC_FLUSH:
            self._on_flush_message(msg)

    def _cancel_speech(self) -> None:
        """Kill any in-progress aplay subprocess.

        Called when a new utterance starts processing or new TTS text
        arrives, ensuring stale responses don't play over fresh ones.
        """
        with self._aplay_lock:
            if self._aplay_proc and self._aplay_proc.poll() is None:
                self._aplay_proc.kill()
                self._aplay_proc.wait()
                logger.info("Cancelled in-progress speech")
                self._aplay_proc = None

    def _on_flush_message(self, msg: Any) -> None:
        """Handle flush broadcast from the coordinator.

        Bumps the TTS generation counter so any in-flight piper
        inference is discarded, and kills any active aplay process.
        The satellite comes up clean for the next request.

        Args:
            msg: MQTT message (payload ignored — presence is the signal).
        """
        self._tts_generation += 1
        self._cancel_speech()
        logger.info(
            "FLUSH received — generation bumped to %d, speech cancelled",
            self._tts_generation,
        )

    def _on_playback_message(self, msg: Any) -> None:
        """Handle playback state messages from the coordinator.

        Used only to cancel stale speech when a new utterance starts.
        Suppression is handled locally by _speak_local_suppressed —
        NOT by this MQTT message, because QoS 0 messages can be
        dropped, which would leave suppression stuck True forever.

        Args:
            msg: MQTT message with JSON payload.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)
            room: str = data.get("room", "")
            playing: bool = data.get("playing", False)

            if room == self._room and playing:
                # New utterance being processed — cancel stale speech.
                self._cancel_speech()
                logger.info("New utterance — cancelled stale speech")
        except Exception as exc:
            logger.debug("Playback message parse error: %s", exc)

    def _on_tts_text_message(self, msg: Any) -> None:
        """Receive TTS text from the coordinator and speak it locally.

        Uses espeak-ng via subprocess to synthesize speech through
        the default ALSA output device.

        Args:
            msg: MQTT message with JSON payload {room, text}.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)
            room: str = data.get("room", "")
            text: str = data.get("text", "")

            if room != self._room or not text:
                return

            # Increment generation — any in-flight TTS with an older
            # generation will be discarded after inference.
            self._tts_generation += 1
            gen: int = self._tts_generation
            logger.info("TTS received (gen=%d): '%s'", gen, text[:60])
            # Cancel any in-progress playback.
            self._cancel_speech()

            if self._tts_output == "mqtt":
                # Publish to remote speaker daemon (e.g. Daedalus).
                payload: str = json.dumps({"text": text})
                self._mqtt_client.publish(self._tts_topic, payload, qos=1)
                logger.info("TTS forwarded to %s", self._tts_topic)
                return

            # Local: speak in a thread so it doesn't block the MQTT
            # callback loop.  Suppress wake detection for the duration
            # — piper + playback takes seconds, and the mic will pick
            # up the speaker output.
            threading.Thread(
                target=self._speak_local_suppressed,
                args=(text, gen),
                daemon=True,
            ).start()
        except Exception as exc:
            logger.error("TTS text message error: %s", exc)

    def _speak_local_suppressed(self, text: str, generation: int) -> None:
        """Speak text locally with wake word suppression.

        Suppresses wake detection before speaking and re-enables it
        after playback completes, preventing the mic from re-triggering
        on the speaker output.  Checks generation counter to discard
        stale responses.

        Uses a counter instead of a boolean so overlapping TTS threads
        don't clobber each other's suppression state.

        Args:
            text:       Text to speak.
            generation: TTS generation counter at time of request.
        """
        with self._suppress_lock:
            self._suppress_count += 1
        logger.info("Local TTS started (gen=%d, suppress=%d)", generation, self._suppress_count)
        try:
            self._speak_local(text, generation)
        finally:
            with self._suppress_lock:
                self._suppress_count = max(0, self._suppress_count - 1)
            logger.info("Local TTS ended (suppress=%d)", self._suppress_count)

    def _init_piper(self) -> None:
        """Start persistent piper process for low-latency TTS.

        Piper reads lines from stdin and writes raw PCM to stdout.
        The model is loaded once at startup (~10s) and stays warm
        for sub-second inference on subsequent requests.
        """
        piper_model: str = self._config.get(
            "piper_model", os.path.expanduser("~/models/en_US-lessac-medium.onnx"),
        )
        piper_bin: str = self._config.get(
            "piper_bin", os.path.expanduser("~/venv/bin/piper"),
        )
        if not os.path.exists(piper_bin) or not os.path.exists(piper_model):
            logger.warning("Piper not available: bin=%s model=%s", piper_bin, piper_model)
            return

        # Read sample rate from the model's JSON config.
        model_json: str = piper_model + ".json"
        if os.path.exists(model_json):
            with open(model_json, "r") as mf:
                mcfg = json.load(mf)
            self._piper_rate = str(mcfg.get("audio", {}).get("sample_rate", 22050))

        try:
            self._piper_proc = subprocess.Popen(
                [piper_bin, "--model", piper_model, "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "Piper TTS started: model=%s rate=%s",
                os.path.basename(piper_model), self._piper_rate,
            )
        except Exception as exc:
            logger.error("Failed to start piper: %s", exc)
            self._piper_proc = None

    def _speak_local(self, text: str, generation: int = 0) -> None:
        """Synthesize and play text through the local audio output.

        Uses persistent piper process for sub-second inference.
        Falls back to espeak-ng if piper is unavailable.  Checks
        generation counter after inference — if a newer request
        arrived while piper was working, the audio is discarded.

        Args:
            text:       Text to speak.
            generation: TTS generation counter at time of request.
        """
        with self._piper_lock:
            if self._piper_proc and self._piper_proc.poll() is None:
                try:
                    # Write text line to piper stdin — triggers inference.
                    line: bytes = (text.strip() + "\n").encode("utf-8")
                    self._piper_proc.stdin.write(line)
                    self._piper_proc.stdin.flush()

                    # Piper outputs raw PCM to stdout.  Read until the
                    # audio stream pauses (no more data available).
                    import select
                    chunks: list[bytes] = []
                    fd = self._piper_proc.stdout.fileno()
                    while True:
                        ready, _, _ = select.select([fd], [], [], 0.3)
                        if ready:
                            data = os.read(fd, 65536)
                            if not data:
                                break
                            chunks.append(data)
                        else:
                            # No data for 300ms — utterance is done.
                            if chunks:
                                break

                    # Check if this response is still current.
                    if generation and generation != self._tts_generation:
                        logger.info(
                            "Discarding stale TTS (gen=%d, current=%d): '%s'",
                            generation, self._tts_generation, text[:40],
                        )
                        return

                    if chunks:
                        raw_audio: bytes = b"".join(chunks)
                        # Play via aplay — use Popen so we can cancel.
                        with self._aplay_lock:
                            self._aplay_proc = subprocess.Popen(
                                ["aplay", "-r", self._piper_rate, "-f", "S16_LE", "-t", "raw", "-"],
                                stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                        try:
                            self._aplay_proc.stdin.write(raw_audio)
                            self._aplay_proc.stdin.close()
                            self._aplay_proc.wait(timeout=30)
                        except Exception:
                            pass  # Killed by _cancel_speech — normal.
                        finally:
                            with self._aplay_lock:
                                self._aplay_proc = None
                        logger.info("Spoke (piper): '%s'", text[:40])
                        return
                except Exception as exc:
                    logger.warning("Piper speak failed: %s", exc)
                    # Restart piper if it died.
                    self._piper_proc = None
                    self._init_piper()

        # Fallback: espeak-ng.
        try:
            subprocess.run(
                ["espeak-ng", "-s", "160", "--", text],
                timeout=30,
                capture_output=True,
            )
            logger.info("Spoke (espeak-ng): '%s'", text[:40])
        except FileNotFoundError:
            logger.error("No TTS engine installed (tried piper, espeak-ng)")
        except subprocess.TimeoutExpired:
            logger.error("espeak-ng timed out speaking: '%s'", text[:40])
        except Exception as exc:
            logger.error("Local TTS failed: %s", exc)

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
            model_path: str = wake_cfg.get("model_path", "")
            if model_path and not os.path.exists(model_path):
                raise FileNotFoundError(model_path)
            # Default: use built-in hey_mycroft if no custom model.
            if not model_path:
                model_path = "hey_mycroft_v0.1"
                logger.info(
                    "No wake word model configured — using built-in '%s'",
                    model_path,
                )
            self._wake = WakeDetector(
                model_path=model_path,
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

        # Initialize subsystems with graceful failure handling.
        # Each subsystem is required — if any fails, the satellite
        # cannot operate and exits with a clear error message.
        try:
            self._init_mqtt()
        except ImportError:
            logger.error(
                "paho-mqtt not installed. Install with: "
                "pip install paho-mqtt"
            )
            return
        except Exception as exc:
            logger.error(
                "MQTT connection failed (%s:%d): %s. "
                "Check that the broker is running.",
                self._mqtt_broker, self._mqtt_port, exc,
            )
            return

        try:
            self._init_audio()
        except Exception as exc:
            logger.error(
                "Audio initialization failed: %s. "
                "Check that a microphone is connected and not in use "
                "by another process.",
                exc,
            )
            return

        try:
            self._init_wake()
        except ImportError as exc:
            logger.error(
                "Wake word initialization failed: %s. "
                "Install with: pip install openwakeword",
                exc,
            )
            return
        except FileNotFoundError as exc:
            logger.error(
                "Wake word model not found: %s. "
                "Provide a model path in the config or use --mock-wake "
                "for development.",
                exc,
            )
            return
        except Exception as exc:
            logger.error(
                "Wake word initialization failed: %s", exc,
            )
            return

        logger.info(
            "Satellite [%s] listening%s...",
            self._room,
            " (mock wake — press Enter)" if self._mock_wake else "",
        )

        last_heartbeat: float = 0.0
        if self._stream is None:
            logger.error("No audio stream — cannot start")
            return

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
                if self._suppress_count > 0:
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
