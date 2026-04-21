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
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional, Union

try:
    import numpy as np
    _HAS_NUMPY: bool = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

from voice import constants as C
from voice.protocol import encode
from voice.satellite.capture import UtteranceCapture

logger: logging.Logger = logging.getLogger("glowup.voice.satellite")

# ---------------------------------------------------------------------------
# Optional reolink_aio import — only required when a satellite instance is
# configured with ``audio.sink = "baichuan"`` (e.g. the Front Doorbell
# satellite pushing TTS audio into a Reolink doorbell via the Baichuan
# talk protocol).  All other deployments must not see an ImportError.
# ---------------------------------------------------------------------------

try:
    from reolink_aio.api import Host as _ReolinkHost
    _HAS_REOLINK_AIO: bool = True
except ImportError:
    _ReolinkHost = None  # type: ignore[assignment,misc]
    _HAS_REOLINK_AIO = False

# ---------------------------------------------------------------------------
# Optional imports
# ---------------------------------------------------------------------------

try:
    import pyaudio
    _HAS_PYAUDIO: bool = True
except ImportError:
    pyaudio = None  # type: ignore[assignment]
    _HAS_PYAUDIO = False

from types import SimpleNamespace

from infrastructure.mqtt_resilient_client import MqttResilientClient

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
        # MqttResilientClient wraps the paho client with on_disconnect
        # logging, a silence watchdog, and fresh-client_id-per-rebuild
        # recovery.  Same attribute name as before so test_gate.py and
        # the handful of callers that read ``_mqtt_client.publish(...)``
        # keep working — the helper's ``publish`` has paho's signature.
        self._mqtt_client: Optional[MqttResilientClient] = None

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

        # Explicit source/sink selection.  Legacy deployments omit these
        # and fall through to the original ALSA/PyAudio auto-detection.
        #
        # audio.source:
        #   None       — legacy auto-detect (Linux=ALSA, macOS=PyAudio)
        #   "alsa"     — force ALSA arecord
        #   "pyaudio"  — force PyAudio
        #   "rtsp"     — pull audio from an RTSP URL via ffmpeg
        # audio.sink:
        #   "alsa"     — (default) aplay to ALSA default device
        #   "baichuan" — push PCM to a Reolink camera via baichuan.talk
        self._audio_source: Optional[str] = audio_cfg.get("source")
        self._audio_sink: str = audio_cfg.get("sink", "alsa")

        # RTSP source state — populated by _init_audio_rtsp when used.
        self._rtsp_proc: Optional[subprocess.Popen] = None

        # Baichuan TTS sink state — populated by _init_baichuan_sink when
        # used.  The satellite daemon is synchronous, but reolink_aio is
        # asyncio; we own a dedicated event loop on a background thread
        # and dispatch talk() calls via run_coroutine_threadsafe.
        self._bc_host: Any = None
        self._bc_loop: Optional[asyncio.AbstractEventLoop] = None
        self._bc_thread: Optional[threading.Thread] = None
        self._bc_channel: int = 0

        # TTS output routing — "local" (default) speaks through piper/espeak
        # on this device, "mqtt" publishes text to a topic for a remote
        # speaker daemon (e.g. Daedalus) to play through its speakers.
        self._tts_output: str = config.get("tts_output", "local")
        self._tts_topic: str = config.get(
            "tts_topic", "glowup/tts/speak",
        )

        # Piper TTS — pool of pre-warmed single-use piper processes.
        # Each process handles one utterance then exits.  EOF on stdout
        # is the end-of-stream signal — no timeouts.
        self._piper_pool: Optional["PiperPool"] = None
        # Track active piper + aplay processes for cancellation.
        # Cancel = kill both processes.  Piper's stdout EOF fires,
        # the read loop exits, no orphaned PCM, no pipe corruption.
        self._active_piper: Optional[subprocess.Popen] = None
        self._active_aplay: Optional[subprocess.Popen] = None
        self._active_lock: threading.Lock = threading.Lock()

        # ALSA playback device for aplay. If unset, _init_audio_alsa
        # defaults it to the same USB card as capture (duplex devices
        # like Jabra speakerphones). Config key: audio.alsa_playback_device
        # (e.g. "plughw:2,0"). Without this, aplay uses ALSA default,
        # which on a Pi with HDMI present is card 0 HDMI — silent.
        self._alsa_playback_device: Optional[str] = config.get(
            "audio", {},
        ).get("alsa_playback_device")
        # Utterance sequence — incremented each time this satellite
        # publishes a new utterance.  The coordinator echoes the seq
        # back in TTS responses.  The satellite discards any TTS whose
        # seq doesn't match the latest published utterance, preventing
        # out-of-order responses from playing.
        self._utterance_seq: int = 0

        # Generation counter — incremented on each new TTS request.
        # Used to discard stale TTS that arrives after a newer request.
        self._tts_generation: int = 0
        if self._tts_output == "local":
            self._init_piper_pool()

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

        # Voice-gate state.  When ``gated`` is true, the satellite
        # discards audio before it reaches the wake detector unless a
        # retained MQTT gate message has explicitly opened the gate
        # with a bounded expiry.  Default-off: gated satellites boot
        # closed and stay closed until an interior room opens them.
        # The ring buffer is still fed even when closed so the RTSP
        # or ALSA source never backs up.
        #
        # Gate slug is the room name lowercased with spaces replaced
        # by underscores — used as the MQTT topic suffix.
        gate_cfg: dict[str, Any] = config.get("voice_gate", {})
        self._gated: bool = bool(gate_cfg.get("gated", False))
        self._gate_slug: str = (
            self._room.lower().replace(" ", "_")
        )
        self._gate_topic: str = (
            f"{C.TOPIC_VOICE_GATE_PREFIX}/{self._gate_slug}"
        )
        self._gate_open: bool = False
        self._gate_expires: float = 0.0
        self._gate_lock: threading.Lock = threading.Lock()

        # -- Deep health probe state ------------------------------------
        # Monotonic timestamps updated by the corresponding hot paths.
        # Read by _run_deep_health_check() to prove each subsystem is
        # still making forward progress even though the heartbeat
        # loop could be alive on its own.  0.0 means "never seen"
        # and is treated as unhealthy the moment a check is issued.
        # Protected by _health_lock because the capture thread, wake
        # thread, and MQTT callback thread all stamp concurrently.
        self._last_audio_frame_ts: float = 0.0
        self._last_wake_eval_ts: float = 0.0
        self._last_utterance_ts: float = 0.0
        self._audio_frames_total: int = 0
        self._health_lock: threading.Lock = threading.Lock()

        # Reply topic for deep-health responses — computed once from
        # the room slug to match the hub's subscription pattern.
        self._health_reply_topic: str = (
            f"{C.TOPIC_HEALTH_REPLY_PREFIX}/{self._gate_slug}"
        )

    def _init_mqtt(self) -> None:
        """Construct and start the resilient MQTT client.

        Non-blocking — ``MqttResilientClient.start()`` uses
        ``connect_async`` internally so a briefly-unreachable broker
        at startup does not abort satellite initialization.  The
        helper's watchdog and paho's internal reconnect logic will
        establish the session as soon as the broker is reachable,
        and the ``on_connected`` hook logs when it happens.

        Subscriptions are registered as a fixed list at construction
        and re-applied on every (re)connect by the helper.  With
        ``clean_session=True`` (paho MQTT 3.1.1 default) the broker
        drops subscriptions on disconnect, so restoring them on every
        new session is what keeps the satellite from going silently
        deaf after a broker blip — mbclock demonstrated that failure
        mode on 2026-04-16 (subs lost at ~20:29, unnoticed for 12h).

        Raises:
            ImportError: ``paho-mqtt`` is not installed.
        """
        subscriptions: list[tuple[str, int]] = [
            (C.TOPIC_PLAYBACK, 0),
            (C.TOPIC_TTS_TEXT, 0),
            (C.TOPIC_FLUSH, 1),
            (C.TOPIC_THINKING, 0),
            # Deep health probe — hub broadcasts a request and every
            # satellite replies with its own subsystem snapshot.
            # QoS 1 so a single dropped packet does not silently hide
            # a hung satellite from the hub's periodic prober.
            (C.TOPIC_HEALTH_REQUEST, 1),
        ]
        # Only gated satellites subscribe to a gate topic.  Non-gated
        # rooms (Dining Room, Main Bedroom, etc.) never see a gate
        # message and their audio loop never consults gate state.
        if self._gated:
            subscriptions.append((self._gate_topic, 1))

        client = MqttResilientClient(
            broker=self._mqtt_broker,
            port=self._mqtt_port,
            client_id_prefix=f"satellite_{self._room}",
            subscriptions=subscriptions,
            on_message=self._dispatch_mqtt_message,
            on_connected=self._on_mqtt_connected,
        )
        if not client.is_available:
            raise ImportError("paho-mqtt not installed")
        client.start()
        self._mqtt_client = client

    def _on_mqtt_connected(self) -> None:
        """Log the satellite-specific ready message after subscribe.

        Called by the helper on every successful (re)connect, after
        all configured subscriptions have been applied.
        """
        if self._gated:
            logger.info(
                "Voice gate enabled — subscribed to %s (default closed)",
                self._gate_topic,
            )
        logger.info("MQTT subscribed (on_connect) — receive path live")

    def _dispatch_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Dispatch incoming MQTT messages by topic.

        Called by ``MqttResilientClient`` on paho's network thread.
        Wraps the ``(topic, payload)`` pair in a ``SimpleNamespace``
        shim so the per-topic ``_on_*_message`` handlers continue to
        accept a paho-style ``msg`` object — this preserves the
        handler signatures the existing test suite depends on
        (``voice/tests/test_gate.py`` exercises ``_on_gate_message``
        with a fabricated ``SimpleNamespace`` message).
        """
        msg = SimpleNamespace(topic=topic, payload=payload)
        if topic == C.TOPIC_PLAYBACK:
            self._on_playback_message(msg)
        elif topic == C.TOPIC_TTS_TEXT:
            self._on_tts_text_message(msg)
        elif topic == C.TOPIC_FLUSH:
            self._on_flush_message(msg)
        elif topic == C.TOPIC_THINKING:
            self._on_thinking_message(msg)
        elif topic == C.TOPIC_HEALTH_REQUEST:
            self._on_health_request_message(msg)
        elif self._gated and topic == self._gate_topic:
            self._on_gate_message(msg)

    def _on_gate_message(self, msg: Any) -> None:
        """Handle a voice-gate retained message.

        Parses ``{"enabled": bool, "expires_at": <unix_ts>}`` and
        updates ``self._gate_open`` / ``self._gate_expires`` under the
        gate lock.  Clamps ``expires_at - now`` to
        :data:`C.VOICE_GATE_MAX_SECONDS` as a belt-and-suspenders
        check — the coordinator is expected to clamp at publish time,
        but the satellite re-checks because the coordinator is outside
        the satellite's trust boundary.  A malformed payload closes
        the gate (fail-safe).

        Args:
            msg: MQTT message with JSON payload.
        """
        now: float = time.time()
        try:
            data: dict[str, Any] = json.loads(msg.payload)
            enabled: bool = bool(data.get("enabled", False))
            expires_at: float = float(data.get("expires_at", 0.0))
        except (ValueError, TypeError) as exc:
            # Corrupt payload — fail safe to closed per Rule #1.
            logger.warning(
                "Gate message parse error on %s: %s — closing gate",
                self._gate_topic, exc,
            )
            with self._gate_lock:
                self._gate_open = False
                self._gate_expires = 0.0
            return

        if enabled:
            # Clamp to the hard max.  The coordinator should already
            # have clamped, but the satellite must not trust upstream.
            max_expires: float = now + float(C.VOICE_GATE_MAX_SECONDS)
            if expires_at > max_expires:
                logger.warning(
                    "Gate expires_at %.0f > max %.0f — clamping",
                    expires_at, max_expires,
                )
                expires_at = max_expires

            if expires_at <= now:
                # Past-expiry enable arrives — treat as closed.
                with self._gate_lock:
                    was_open: bool = self._gate_open
                    self._gate_open = False
                    self._gate_expires = 0.0
                if was_open:
                    logger.info("Gate already expired on receipt — closed")
                return

            with self._gate_lock:
                self._gate_open = True
                self._gate_expires = expires_at
            remaining: float = expires_at - now
            logger.info(
                "Gate OPEN on %s for %.0fs (until %s)",
                self._gate_topic, remaining,
                time.strftime("%H:%M:%S", time.localtime(expires_at)),
            )
        else:
            with self._gate_lock:
                was_open = self._gate_open
                self._gate_open = False
                self._gate_expires = 0.0
            if was_open:
                logger.info("Gate CLOSED on %s", self._gate_topic)

    def _gate_permits_audio(self) -> bool:
        """Return True if the main audio loop may feed the wake detector.

        Non-gated satellites always return True.  Gated satellites
        return True only while the gate is open and not yet expired;
        an auto-expiry transition publishes a retained closed message
        back so the dashboard and other listeners see the state change.

        Returns:
            True if wake detection should run, False to discard audio.
        """
        if not self._gated:
            return True

        now: float = time.time()
        with self._gate_lock:
            if not self._gate_open:
                return False
            if now >= self._gate_expires:
                # Auto-expiry.  Flip state and fall through to publish
                # the retained close outside the lock.
                self._gate_open = False
                self._gate_expires = 0.0
                expired: bool = True
            else:
                expired = False

        if expired:
            logger.info(
                "Gate auto-expired on %s — republishing closed",
                self._gate_topic,
            )
            self._publish_gate_closed()
            return False

        return True

    def _publish_gate_closed(self) -> None:
        """Publish a retained gate-closed message.

        Called on auto-expiry so downstream listeners (dashboard,
        other satellites, audits) see the gate transition without
        having to poll.  Retained so a reconnecting client gets the
        current (closed) state immediately.
        """
        if self._mqtt_client is None:
            return
        payload: bytes = json.dumps(
            {"enabled": False, "expires_at": 0}
        ).encode("utf-8")
        try:
            self._mqtt_client.publish(
                self._gate_topic, payload, qos=1, retain=True,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            # Logged but not raised — failing to republish does not
            # stop the satellite from honoring the closed state locally.
            logger.warning(
                "Failed to republish gate closed on %s: %s",
                self._gate_topic, exc,
            )

    def _cancel_speech(self) -> None:
        """Kill any in-progress piper and aplay processes.

        Called when a new utterance starts processing or new TTS text
        arrives, ensuring stale responses don't play over fresh ones.

        Killing piper closes its stdout, which causes the read loop
        in _speak_local to hit EOF and exit cleanly.  No orphaned PCM,
        no pipe drain needed.
        """
        with self._active_lock:
            killed: bool = False
            if self._active_aplay and self._active_aplay.poll() is None:
                self._active_aplay.kill()
                self._active_aplay.wait()
                killed = True
            if self._active_piper and self._active_piper.poll() is None:
                self._active_piper.kill()
                self._active_piper.wait()
                killed = True
            self._active_aplay = None
            self._active_piper = None
            if killed:
                logger.info("Cancelled in-progress speech (piper + aplay killed)")

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

    def _on_thinking_message(self, msg: Any) -> None:
        """Play local "working" audio cue for slow actions.

        The coordinator sends this instead of a "Waiting on the
        assistant" TTS message.  The satellite plays a short audio
        clip (Star Trek TOS computer "Working...") through aplay.
        No piper needed — it's a pre-recorded WAV file.

        When the real answer arrives via tts_text, _cancel_speech
        will kill this aplay if it's still playing.

        Args:
            msg: MQTT message with JSON payload {room, timestamp}.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)
            room: str = data.get("room", "")

            if room != self._room:
                return

            # Path to the pre-recorded "working" audio clip.
            working_wav: str = os.path.join(
                os.path.expanduser("~/models"), "tos_working.wav",
            )
            if not os.path.exists(working_wav):
                logger.warning("Working audio not found: %s", working_wav)
                return

            # Play through aplay in a thread so we don't block MQTT.
            # Register aplay for cancellation so the real answer can
            # preempt it.
            def _play_working() -> None:
                with self._suppress_lock:
                    self._suppress_count += 1
                logger.info("Playing 'working' audio cue")
                try:
                    cmd: list[str] = ["aplay", "-q"]
                    if self._alsa_playback_device:
                        cmd += ["-D", self._alsa_playback_device]
                    cmd.append(working_wav)
                    aplay: subprocess.Popen = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    with self._active_lock:
                        self._active_aplay = aplay
                    aplay.wait()
                    if aplay.returncode and aplay.returncode < 0:
                        logger.info("Working audio cancelled by incoming TTS")
                    else:
                        logger.info("Working audio finished")
                except (OSError, subprocess.SubprocessError) as exc:
                    logger.warning("Working audio failed: %s", exc)
                finally:
                    with self._active_lock:
                        self._active_aplay = None
                    with self._suppress_lock:
                        self._suppress_count = max(0, self._suppress_count - 1)

            threading.Thread(target=_play_working, daemon=True).start()
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.error("Thinking message error: %s", exc)

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
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
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

            # Sequence check — discard TTS for a stale utterance.
            # The coordinator echoes the satellite's utterance seq.
            # If we've published a newer utterance since, this
            # response is out of order and must be dropped.
            msg_seq: int = data.get("seq", 0)
            if msg_seq and msg_seq != self._utterance_seq:
                logger.info(
                    "Discarding stale TTS (seq=%d, current=%d): '%s'",
                    msg_seq, self._utterance_seq, text[:40],
                )
                return

            # Increment generation — any in-flight TTS with an older
            # generation will be discarded after inference.
            self._tts_generation += 1
            gen: int = self._tts_generation
            logger.info("TTS received (gen=%d, seq=%d): '%s'", gen, msg_seq, text[:60])
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
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
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

    def _init_piper_pool(self) -> None:
        """Start the PiperPool for single-use TTS processes.

        Each process loads the model once, handles one utterance,
        then exits.  EOF on stdout is the end-of-stream signal.
        """
        from voice.piper_pool import PiperPool

        piper_model: str = self._config.get(
            "piper_model", os.path.expanduser("~/models/en_US-lessac-medium.onnx"),
        )
        piper_bin: str = self._config.get(
            "piper_bin", os.path.expanduser("~/venv/bin/piper"),
        )

        pool: PiperPool = PiperPool(
            model=piper_model,
            piper_bin=piper_bin,
            size=2,
        )
        if pool.start():
            self._piper_pool = pool
        else:
            logger.error("PiperPool failed to start")

    def _speak_local(self, text: str, generation: int = 0) -> None:
        """Synthesize TTS and play through the configured audio sink.

        Dispatches on ``audio.sink``:

        - ``"baichuan"`` — push PCM to a Reolink camera speaker via the
          Baichuan talk protocol (``baichuan.talk``)
        - anything else — the original aplay-to-ALSA path

        Args:
            text:       Text to speak.
            generation: TTS generation counter at time of request.
        """
        if self._audio_sink == "baichuan":
            self._speak_via_baichuan_sink(text, generation)
        else:
            self._speak_via_alsa_sink(text, generation)

    def _speak_via_alsa_sink(self, text: str, generation: int = 0) -> None:
        """Synthesize and play through the local ALSA output.

        Acquires a single-use piper process from the pool, writes
        the text, closes stdin, and streams PCM to aplay until EOF.
        No timeouts — EOF is the only end-of-stream signal.

        On cancel, _cancel_speech kills both piper and aplay.  Piper's
        stdout closes, the read loop exits on EOF.  No orphaned PCM.

        Args:
            text:       Text to speak.
            generation: TTS generation counter at time of request.
        """
        if self._piper_pool is not None:
            # Check generation before acquiring from pool.
            if generation and generation != self._tts_generation:
                logger.info(
                    "Discarding stale TTS (gen=%d, current=%d): '%s'",
                    generation, self._tts_generation, text[:40],
                )
                return

            piper: subprocess.Popen = self._piper_pool.acquire()
            rate: str = str(self._piper_pool.sample_rate)

            # Register piper as the active process so _cancel_speech
            # can kill it.
            with self._active_lock:
                self._active_piper = piper

            try:
                # Write text and close stdin — piper will produce PCM
                # on stdout then exit.  EOF is our end-of-stream signal.
                line: bytes = (text.strip() + "\n").encode("utf-8")
                piper.stdin.write(line)
                piper.stdin.close()

                # Start aplay and register it for cancellation.
                aplay_cmd: list[str] = ["aplay"]
                if self._alsa_playback_device:
                    aplay_cmd += ["-D", self._alsa_playback_device]
                aplay_cmd += ["-r", rate, "-f", "S16_LE", "-t", "raw", "-"]
                aplay: subprocess.Popen = subprocess.Popen(
                    aplay_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with self._active_lock:
                    self._active_aplay = aplay

                # Stream PCM from piper stdout to aplay stdin until EOF.
                # No select(), no timeout.  os.read returns b"" at EOF.
                fd: int = piper.stdout.fileno()
                pcm_bytes: int = 0
                try:
                    while True:
                        data: bytes = os.read(fd, 65536)
                        if not data:
                            break
                        pcm_bytes += len(data)
                        aplay.stdin.write(data)
                    aplay.stdin.close()
                    aplay.wait(timeout=30)
                except (BrokenPipeError, OSError):
                    # aplay was killed by _cancel_speech — normal.
                    pass

                piper.wait(timeout=5)

                # Log outcome.
                if piper.returncode and piper.returncode < 0:
                    logger.info(
                        "Speech cancelled: '%s' (%d PCM bytes before kill)",
                        text[:40], pcm_bytes,
                    )
                else:
                    logger.info(
                        "Spoke (piper): '%s' (%d PCM bytes)",
                        text[:40], pcm_bytes,
                    )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                logger.warning("Piper speak failed: %s", exc)
                # Kill the piper process if it's still alive.
                if piper.poll() is None:
                    piper.kill()
                    piper.wait()
            finally:
                with self._active_lock:
                    self._active_piper = None
                    self._active_aplay = None
            return

        # Fallback path — PiperPool unavailable.  Use espeak-ng so the
        # satellite still produces *some* audible output when Piper is
        # broken.  Only reachable when self._piper_pool is None.
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
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("Local TTS failed: %s", exc)

    def _speak_via_baichuan_sink(
        self, text: str, generation: int = 0,
    ) -> None:
        """Synthesize TTS and push PCM to a Reolink camera speaker.

        The Baichuan talk protocol (implemented by reolink_aio and
        extended in PR #165) takes a complete PCM buffer per call at
        16 kHz mono int16.  Piper emits PCM at its model's native rate
        (22050 Hz for the medium voices), so this method:

        1. Spawns a piper process and writes the text
        2. Collects all PCM bytes from piper stdout until EOF
        3. Resamples to 16 kHz if the model rate differs
        4. Dispatches ``baichuan.talk`` on the asyncio loop thread
           via ``run_coroutine_threadsafe``

        Args:
            text:       Text to speak.
            generation: TTS generation counter at time of request.
        """
        if self._piper_pool is None:
            logger.warning("Baichuan sink: no piper pool")
            return
        if self._bc_host is None or self._bc_loop is None:
            logger.warning("Baichuan sink: Host not initialized")
            return

        # Discard stale TTS if a newer request has arrived.
        if generation and generation != self._tts_generation:
            logger.info(
                "Discarding stale TTS (gen=%d, current=%d): '%s'",
                generation, self._tts_generation, text[:40],
            )
            return

        piper: subprocess.Popen = self._piper_pool.acquire()
        piper_rate: int = self._piper_pool.sample_rate

        with self._active_lock:
            self._active_piper = piper

        pcm_bytes: int = 0
        try:
            # Write text and close stdin — piper will produce PCM.
            line: bytes = (text.strip() + "\n").encode("utf-8")
            piper.stdin.write(line)
            piper.stdin.close()

            # Drain PCM from piper stdout until EOF.  Collect to a
            # single buffer — talk() wants the whole audio at once.
            # Typical utterances are 1–3 s = 32–96 KB of PCM, trivial.
            fd: int = piper.stdout.fileno()
            chunks: list[bytes] = []
            try:
                while True:
                    data: bytes = os.read(fd, 65536)
                    if not data:
                        break
                    chunks.append(data)
                    pcm_bytes += len(data)
            except (BrokenPipeError, OSError):
                # piper was killed by _cancel_speech — treat as cancel.
                pass

            piper.wait(timeout=5)

            if piper.returncode and piper.returncode < 0:
                logger.info(
                    "Speech cancelled: '%s' (%d PCM bytes before kill)",
                    text[:40], pcm_bytes,
                )
                return

            if not chunks:
                logger.debug("Baichuan sink: empty PCM buffer, nothing to send")
                return

            pcm: bytes = b"".join(chunks)

            # Resample to 16 kHz if Piper's native rate differs.
            target_rate: int = 16000
            if piper_rate != target_rate:
                pcm = self._resample_pcm_bytes(pcm, piper_rate, target_rate)

            # Dispatch talk() onto the asyncio loop thread.  Use a
            # generous timeout — 1.08 s of audio takes about that long
            # to send at 16 kHz, plus NVR round-trip and protocol setup.
            # Cap at 30 s for safety.
            fut = asyncio.run_coroutine_threadsafe(
                self._bc_host.baichuan.talk(
                    channel=self._bc_channel,
                    audio_data=pcm,
                ),
                self._bc_loop,
            )
            try:
                fut.result(timeout=30)
                logger.info(
                    "Spoke (baichuan ch%d): '%s' (%d PCM bytes, %d Hz)",
                    self._bc_channel, text[:40], len(pcm), target_rate,
                )
            except (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
                logger.error("baichuan.talk failed: %s", exc)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning("Baichuan speak failed: %s", exc)
            if piper.poll() is None:
                piper.kill()
                piper.wait()
        finally:
            with self._active_lock:
                self._active_piper = None

    @staticmethod
    def _resample_pcm_bytes(
        pcm: bytes, src_rate: int, dst_rate: int,
    ) -> bytes:
        """Resample int16 mono PCM bytes between arbitrary rates.

        Uses numpy linear interpolation — not audiophile-grade, but
        sufficient for speech intelligibility through the doorbell's
        IMA ADPCM codec, which is the quality ceiling here anyway.

        Args:
            pcm:      Raw int16 mono PCM.
            src_rate: Source sample rate (e.g. 22050 from Piper).
            dst_rate: Target sample rate (e.g. 16000 for baichuan.talk).

        Returns:
            Resampled int16 mono PCM bytes.
        """
        if src_rate == dst_rate:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        ratio: float = dst_rate / src_rate
        new_len: int = max(1, int(len(samples) * ratio))
        indices = np.linspace(0, len(samples) - 1, new_len)
        resampled = np.interp(indices, np.arange(len(samples)), samples)
        return resampled.astype(np.int16).tobytes()

    def _init_baichuan_sink(self) -> None:
        """Connect to the Reolink host for baichuan TTS output.

        Spawns a dedicated asyncio event loop on a background thread,
        instantiates a reolink_aio ``Host``, and blocks until
        ``get_host_data()`` succeeds.  Raises if the reolink_aio
        optional dependency is missing or the NVR refuses the
        connection.
        """
        if not _HAS_REOLINK_AIO:
            raise ImportError(
                "reolink_aio not installed — required for audio.sink "
                "'baichuan'. Install with: pip install reolink_aio",
            )

        bc_cfg: dict[str, Any] = self._config.get("audio", {}).get(
            "baichuan", {},
        )
        host_addr: str = bc_cfg.get("host", "")
        port: int = int(bc_cfg.get("port", 80))
        username: str = bc_cfg.get("username", "")
        password: str = bc_cfg.get("password", "")
        self._bc_channel = int(bc_cfg.get("channel", 0))

        if not host_addr:
            raise RuntimeError(
                "audio.baichuan.host required when audio.sink == 'baichuan'",
            )

        # Dedicated asyncio loop thread — owns the Reolink connection.
        # The Host object must be constructed AND driven from the loop
        # thread: reolink_aio's Host.__init__ calls
        # ``asyncio.get_running_loop()`` internally, so constructing it
        # from the sync init thread raises "no running event loop".
        self._bc_loop = asyncio.new_event_loop()

        def _run_loop() -> None:
            assert self._bc_loop is not None
            asyncio.set_event_loop(self._bc_loop)
            self._bc_loop.run_forever()

        self._bc_thread = threading.Thread(
            target=_run_loop,
            name="baichuan-loop",
            daemon=True,
        )
        self._bc_thread.start()

        assert _ReolinkHost is not None

        async def _connect() -> Any:
            """Construct the Host and fetch host data on the loop thread."""
            host = _ReolinkHost(
                host_addr, username, password, port=port,
            )
            await host.get_host_data()
            return host

        # Block until the host data is populated (talk() needs it).
        # 60 s covers the observed 52 s worst-case when the NVR is
        # under session pressure.
        fut = asyncio.run_coroutine_threadsafe(_connect(), self._bc_loop)
        self._bc_host = fut.result(timeout=60)
        logger.info(
            "Baichuan TTS sink connected: nvr=%s:%d channel=%d",
            host_addr, port, self._bc_channel,
        )

    def _init_audio(self) -> None:
        """Open the audio input stream.

        Dispatches on ``audio.source`` in the config:

        - ``"alsa"``    — ALSA arecord subprocess
        - ``"pyaudio"`` — PyAudio stream
        - ``"rtsp"``    — ffmpeg RTSP capture, stdout PCM pipe

        If ``audio.source`` is absent (legacy deployments) falls back to
        the original Linux-vs-macOS auto-detection so existing satellite
        configs keep working without modification.

        For ALSA and RTSP the main read loop uses pipe semantics
        (``self._use_alsa == True``) — ``self._stream`` is a file-like
        object that returns bytes.  For PyAudio ``self._use_alsa`` is
        False and ``self._stream.read(n_frames)`` is called instead.
        """
        if self._audio_source == "rtsp":
            self._init_audio_rtsp()
            return
        if self._audio_source == "alsa":
            self._use_alsa = True
            self._init_audio_alsa()
            return
        if self._audio_source == "pyaudio":
            self._use_alsa = False
            self._init_audio_pyaudio()
            return

        # Legacy auto-detect — explicit audio.source unset.
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

    def _init_audio_rtsp(self) -> None:
        """Open audio via an ffmpeg RTSP subprocess.

        Pulls the audio track from the configured RTSP URL, decodes to
        16-bit little-endian mono PCM at the target sample rate, and
        exposes ffmpeg's stdout as the read stream.  The main loop and
        UtteranceCapture both read this stream as a byte pipe (same
        pattern as the ALSA arecord source), so setting
        ``self._use_alsa = True`` is a correct reuse of the pipe-read
        code path even though this source is not ALSA.

        Config (under ``audio.rtsp``)::

            {
                "url": "rtsp://user:pass@host:554/Preview_14_sub",
                "ffmpeg_bin": "ffmpeg"   // optional
            }
        """
        rtsp_cfg: dict[str, Any] = self._config.get("audio", {}).get(
            "rtsp", {},
        )
        url: str = rtsp_cfg.get("url", "")
        if not url:
            raise RuntimeError(
                "audio.rtsp.url is required when audio.source == 'rtsp'",
            )
        ffmpeg_bin: str = rtsp_cfg.get("ffmpeg_bin", "ffmpeg")

        # RTSP source already outputs at target rate — no resample.
        self._hw_rate = self._sample_rate
        self._needs_resample = False
        self._hw_chunk = self._chunk_samples

        # ffmpeg command:
        # -loglevel error        Quiet ffmpeg output (we own logging).
        # -rtsp_transport tcp    TCP is more reliable than UDP over WiFi.
        # -i <url>               Source RTSP stream.
        # -vn                    Discard video tracks — audio only.
        # -ac 1                  Force mono.
        # -ar <rate>             Target sample rate.
        # -acodec pcm_s16le      16-bit little-endian PCM.
        # -f s16le -             Write raw PCM to stdout.
        cmd: list[str] = [
            ffmpeg_bin,
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", url,
            "-vn",
            "-ac", "1",
            "-ar", str(self._sample_rate),
            "-acodec", "pcm_s16le",
            "-f", "s16le",
            "-",
        ]
        self._rtsp_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Pipe-reader semantics; see docstring above.
        self._stream = self._rtsp_proc.stdout  # type: ignore[assignment]
        self._use_alsa = True
        # Kept as a distinct attribute for parity with the ALSA path
        # so stop() can find the arecord-or-equivalent process.
        self._alsa_proc = None

        # Redact the password before logging the URL.
        safe_url: str = url
        if "@" in safe_url and "://" in safe_url:
            scheme, rest = safe_url.split("://", 1)
            if "@" in rest:
                creds, host_part = rest.split("@", 1)
                if ":" in creds:
                    user, _pw = creds.split(":", 1)
                    safe_url = f"{scheme}://{user}:****@{host_part}"
        logger.info(
            "RTSP audio stream opened: url=%s rate=%d chunk=%d",
            safe_url, self._sample_rate, self._hw_chunk,
        )

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
                        # Default playback to the same USB card —
                        # duplex devices (Jabra SPEAK etc.) expect it.
                        if self._alsa_playback_device is None:
                            self._alsa_playback_device = f"plughw:{card},0"
                            logger.info(
                                "Defaulting ALSA playback to %s",
                                self._alsa_playback_device,
                            )
                        break
            except (OSError, subprocess.SubprocessError) as exc:
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

        self._utterance_seq += 1
        header: dict[str, Any] = {
            "room": self._room,
            "sample_rate": self._sample_rate,
            "channels": C.CHANNELS,
            "bit_depth": C.BIT_DEPTH,
            "timestamp": time.time(),
            "wake_score": float(wake_score),
            "seq": self._utterance_seq,
        }

        payload: bytes = encode(header, pcm)

        self._mqtt_client.publish(
            C.TOPIC_UTTERANCE, payload, qos=1,
        )
        # Deep-health heartbeat: stamp after the publish so the
        # utterance_publish check in _run_deep_health_check only
        # counts successfully emitted audio, not capture attempts.
        with self._health_lock:
            self._last_utterance_ts = time.time()

        duration: float = len(pcm) / (self._sample_rate * C.BYTES_PER_SAMPLE)
        logger.info(
            "Published %.1fs utterance (%d bytes) from %s",
            duration, len(payload), self._room,
        )

    def _publish_heartbeat(self) -> None:
        """Publish a heartbeat status message."""
        if self._mqtt_client is None:
            return

        # Snapshot gate state for the heartbeat payload so a dashboard
        # tile can render without subscribing to the gate topic.
        # Non-gated satellites report ``gated: false`` and null expiry.
        with self._gate_lock:
            gate_open: bool = self._gate_open
            gate_expires: float = self._gate_expires

        status: dict[str, Any] = {
            "room": self._room,
            "timestamp": time.time(),
            "mock_wake": self._mock_wake,
            "gated": self._gated,
            "gate_open": gate_open if self._gated else True,
            "gate_expires_at": gate_expires if self._gated else 0.0,
        }
        topic: str = f"{C.TOPIC_STATUS_PREFIX}/{self._room}"
        self._mqtt_client.publish(
            topic,
            json.dumps(status).encode("utf-8"),
            qos=0,
        )

    # ---------------------------------------------------------------------
    # Deep health probe — responds to hub-broadcast health requests.
    # ---------------------------------------------------------------------
    #
    # Protocol:
    #   Hub publishes C.TOPIC_HEALTH_REQUEST with payload
    #     {"id": "<corr-id>", "room": "<target>|null}
    #   Every satellite receives the broadcast.  Each decides whether
    #   to reply based on the "room" filter (null/missing = all).
    #   Replies go to self._health_reply_topic with payload
    #     {"id": ..., "room": ..., "timestamp": ..., "ok": bool,
    #      "checks": {name: {ok, detail, age_s, duration_ms}},
    #      "recommended_action": str|null}
    #
    # The reply re-uses the satellite's own running state rather than
    # re-importing modules — the running daemon IS the health check,
    # which is stronger than a file-exists probe.

    def _on_health_request_message(self, msg: Any) -> None:
        """Handle a broadcast health-check request from the hub.

        Args:
            msg: MQTT message with JSON payload containing the
                 correlation id and optional room filter.
        """
        try:
            data: dict[str, Any] = json.loads(msg.payload)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Health request parse error: %s — ignoring", exc,
            )
            return
        corr_id: str = str(data.get("id", ""))
        target_room: Optional[str] = data.get("room")
        # Room filter: null/missing means "reply to all"; otherwise
        # only the named room replies.  Case-sensitive exact match
        # against self._room.
        if target_room and target_room != self._room:
            return
        try:
            report: dict[str, Any] = self._run_deep_health_check(corr_id)
        except Exception as exc:
            # Bullet/idiot-proof: never let a health check crash the
            # satellite.  Log and reply with an error so the hub can
            # still see "something answered."
            logger.exception(
                "Deep health check raised: %s", exc,
            )
            report = {
                "id": corr_id,
                "room": self._room,
                "timestamp": time.time(),
                "ok": False,
                "checks": {},
                "recommended_action": (
                    f"deep health check crashed: {exc!r} — "
                    f"inspect journalctl -u glowup-satellite on "
                    f"the host running room {self._room!r}"
                ),
            }
        self._publish_health_reply(report)

    def _run_deep_health_check(self, corr_id: str) -> dict[str, Any]:
        """Snapshot every satellite subsystem and return a report.

        This runs in the MQTT callback thread and must be cheap.
        It samples the subsystem monotonic timestamps under the
        health lock, classifies each against its staleness
        threshold, and synthesizes a single ``recommended_action``
        string that future-Claude can act on without re-reading
        the entire report.  Order of subsystem evaluation is the
        order a human would triage: MQTT → audio capture → wake
        inference → utterance publish pipeline.

        Args:
            corr_id: Correlation id from the originating request,
                     echoed into the reply so the hub can match
                     request → reply pairs.

        Returns:
            Dict shaped as documented in the protocol comment
            above the calling handler.
        """
        now: float = time.time()
        with self._health_lock:
            audio_ts: float = self._last_audio_frame_ts
            wake_ts: float = self._last_wake_eval_ts
            utt_ts: float = self._last_utterance_ts
            frames_total: int = self._audio_frames_total

        def age_of(ts: float) -> float:
            """Seconds since ``ts``; ``inf`` for never-seen (0.0)."""
            return (now - ts) if ts > 0.0 else float("inf")

        def mk(
            ok: bool, detail: str, age_s: float,
        ) -> dict[str, Any]:
            """Build one check entry with consistent shape."""
            return {
                "ok": ok,
                "detail": detail,
                "age_s": age_s if age_s != float("inf") else None,
            }

        checks: dict[str, dict[str, Any]] = {}

        # -- MQTT: is our own client still talking to the broker? --
        mqtt_ok: bool = False
        mqtt_detail: str = "client not initialised"
        if self._mqtt_client is not None:
            # MqttResilientClient exposes ``is_connected`` as a
            # property, not a method — paho's own client has
            # ``is_connected()`` as a method.  The helper's property
            # reflects the state tracked via ``on_connect`` /
            # ``on_disconnect`` callbacks, which is the authoritative
            # signal for whether we believe the session is live.
            mqtt_ok = bool(self._mqtt_client.is_connected)
            mqtt_detail = (
                "connected"
                if mqtt_ok
                else "MqttResilientClient reports disconnected"
            )
        checks["mqtt"] = mk(mqtt_ok, mqtt_detail, 0.0)

        # -- Audio capture: are PCM frames still arriving? --
        audio_age: float = age_of(audio_ts)
        audio_ok: bool = (
            audio_ts > 0.0 and audio_age < C.SAT_AUDIO_FRAME_STALE_S
        )
        if audio_ts == 0.0:
            audio_detail = (
                "no audio frames ever received — capture thread "
                "may have failed to start"
            )
        elif audio_ok:
            audio_detail = (
                f"last frame {audio_age:.2f}s ago "
                f"({frames_total} frames total)"
            )
        else:
            audio_detail = (
                f"last frame {audio_age:.1f}s ago (threshold "
                f"{C.SAT_AUDIO_FRAME_STALE_S}s) — capture thread "
                "is hung or the audio device disappeared"
            )
        checks["audio_capture"] = mk(audio_ok, audio_detail, audio_age)

        # -- Wake inference: has the detector evaluated recently? --
        wake_age: float = age_of(wake_ts)
        wake_ok: bool = (
            wake_ts > 0.0 and wake_age < C.SAT_WAKE_EVAL_STALE_S
        )
        if wake_ts == 0.0:
            wake_detail = (
                "wake detector never ran — main loop may be "
                "blocked before inference or mock_wake suppresses it"
            )
            # Mock wake never evaluates the detector; that's intended,
            # not a failure.
            if self._mock_wake:
                wake_ok = True
                wake_detail = (
                    "mock_wake=true — detector is intentionally "
                    "bypassed; no inference expected"
                )
        elif wake_ok:
            wake_detail = f"last inference {wake_age:.2f}s ago"
        else:
            wake_detail = (
                f"last inference {wake_age:.1f}s ago (threshold "
                f"{C.SAT_WAKE_EVAL_STALE_S}s) — wake thread is hung"
            )
        checks["wake_inference"] = mk(wake_ok, wake_detail, wake_age)

        # -- Utterance publish: informational, never a failure. --
        utt_age: float = age_of(utt_ts)
        if utt_ts == 0.0:
            utt_detail = "no utterance published this session"
        elif utt_age < C.SAT_UTTERANCE_IDLE_WARN_S:
            utt_detail = f"last utterance {utt_age:.0f}s ago"
        else:
            utt_detail = (
                f"last utterance {utt_age / 60:.0f}min ago "
                "(no one has spoken — not a failure)"
            )
        checks["utterance_publish"] = mk(True, utt_detail, utt_age)

        # -- Gate state (only meaningful for gated rooms). --
        if self._gated:
            with self._gate_lock:
                gate_open: bool = self._gate_open
                gate_expires: float = self._gate_expires
            gate_ok: bool = True  # Closed or open, both are valid.
            gate_detail: str = (
                f"gate {'OPEN' if gate_open else 'closed'}"
            )
            if gate_open and gate_expires > 0:
                gate_detail += f", expires in {gate_expires - now:.0f}s"
            checks["voice_gate"] = mk(gate_ok, gate_detail, 0.0)

        # -- Rollup: any failing check makes the satellite unhealthy.
        all_ok: bool = all(
            v["ok"] for v in checks.values()
        )

        # -- Recommended action — the single field future-me reads
        # first on a degraded satellite.  Concatenates failing checks
        # into an ordered triage instruction.  None means "nothing
        # to do; the satellite is healthy."
        recommended_action: Optional[str] = None
        if not all_ok:
            failing: list[str] = [
                name for name, v in checks.items() if not v["ok"]
            ]
            # Prioritise the most upstream failure — a dead MQTT
            # client masks everything else.
            if "mqtt" in failing:
                recommended_action = (
                    f"room {self._room!r}: MQTT client is not "
                    "connected.  Check broker reachability from the "
                    "satellite host, then "
                    "`sudo systemctl restart glowup-satellite` "
                    "on that host."
                )
            elif "audio_capture" in failing:
                recommended_action = (
                    f"room {self._room!r}: audio capture thread is "
                    "hung.  `sudo systemctl restart glowup-satellite` "
                    "on the host.  If it recurs, check "
                    "`arecord -l` and the ALSA default device."
                )
            elif "wake_inference" in failing:
                recommended_action = (
                    f"room {self._room!r}: wake-word inference is "
                    "stale while audio capture is live.  The wake "
                    "thread is hung; restart glowup-satellite on "
                    "the host and watch for openwakeword errors in "
                    "journalctl."
                )
            else:
                recommended_action = (
                    f"room {self._room!r}: failing checks "
                    f"{failing} — inspect the individual check "
                    "details in this report."
                )

        return {
            "id": corr_id,
            "room": self._room,
            "timestamp": now,
            "ok": all_ok,
            "checks": checks,
            "recommended_action": recommended_action,
        }

    def _publish_health_reply(self, report: dict[str, Any]) -> None:
        """Publish a health report on the per-room reply topic.

        Args:
            report: Dict returned by ``_run_deep_health_check``.
        """
        if self._mqtt_client is None:
            return
        try:
            self._mqtt_client.publish(
                self._health_reply_topic,
                json.dumps(report).encode("utf-8"),
                qos=1,
            )
        except Exception as exc:
            # Logged but not raised — the hub will simply observe
            # a stale reply and fall back to heartbeat staleness.
            logger.warning(
                "Failed to publish health reply on %s: %s",
                self._health_reply_topic, exc,
            )

    def start(self) -> None:
        """Start the satellite daemon.

        Blocks until stopped via SIGTERM/SIGINT or ``stop()`` call.
        """
        self._running = True

        # Initialize subsystems with graceful failure handling.
        # Each subsystem is required — if any fails, the satellite
        # cannot operate and exits with a clear error message.
        # ``_init_mqtt`` is non-blocking: ``MqttResilientClient.start``
        # uses ``connect_async`` so an unreachable broker at startup
        # no longer aborts satellite init.  The helper retries and
        # logs on its own schedule; the satellite continues to boot.
        try:
            self._init_mqtt()
        except ImportError:
            logger.error(
                "paho-mqtt not installed. Install with: "
                "pip install paho-mqtt"
            )
            return

        try:
            self._init_audio()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
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
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error(
                "Wake word initialization failed: %s", exc,
            )
            return

        # Baichuan TTS sink — only initialized when audio.sink is set
        # to "baichuan".  Happens after wake init so a sink failure
        # doesn't mask earlier misconfiguration errors.
        if self._audio_sink == "baichuan":
            try:
                self._init_baichuan_sink()
            except ImportError as exc:
                logger.error(
                    "Baichuan TTS sink missing dependency: %s", exc,
                )
                return
            except (TimeoutError, asyncio.TimeoutError) as exc:
                logger.error(
                    "Baichuan TTS sink timed out connecting to NVR: %s. "
                    "NVR may be at connection max — reboot NVR to clear "
                    "stale sessions.",
                    exc,
                )
                return
            except (ConnectionError, OSError) as exc:
                logger.error(
                    "Baichuan TTS sink connection refused by NVR: %s. "
                    "Check audio.baichuan config and NVR reachability.",
                    exc,
                )
                return
            except RuntimeError as exc:
                logger.error(
                    "Baichuan TTS sink config error: %s", exc,
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
                except (OSError, IOError) as exc:
                    logger.warning("Audio read error: %s", exc)
                    time.sleep(0.1)
                    continue

                # Feed pre-wake ring buffer.  This runs even when the
                # gate is closed so RTSP/ALSA sources never back up.
                self._capture.feed_ring(raw)

                # Deep-health heartbeat: every successful raw read
                # stamps the audio-capture liveness timestamp.  Put
                # it here (not inside the gate/suppress branches) so
                # a gated-closed satellite still proves its capture
                # thread is alive.  See _run_deep_health_check.
                with self._health_lock:
                    self._last_audio_frame_ts = time.time()
                    self._audio_frames_total += 1

                # Voice gate — default-off for untrusted satellites.
                # When closed, no wake detection, no capture, no
                # publish.  The ring buffer above still drains the
                # source so ffmpeg/ALSA doesn't stall.
                if self._gated and not self._gate_permits_audio():
                    continue

                # Skip wake detection while speaker is playing TTS
                # to prevent the mic from re-triggering on its own output.
                if self._suppress_count > 0:
                    continue

                # Feed wake detector.
                audio_array: np.ndarray = np.frombuffer(
                    raw, dtype=np.int16,
                )
                score: Optional[float] = self._wake.feed(audio_array)
                # Deep-health heartbeat: the feed() call either ran
                # the model (real detector) or polled the mock keyboard
                # thread.  Either way, stamp — it proves the wake
                # path is still reaching this point in the loop.
                with self._health_lock:
                    self._last_wake_eval_ts = time.time()

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

        # RTSP ffmpeg subprocess — terminate cleanly so the NVR sees
        # the RTSP session end and doesn't hang onto a stale slot.
        if self._rtsp_proc is not None:
            self._rtsp_proc.terminate()
            try:
                self._rtsp_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("rtsp ffmpeg did not exit — killing")
                self._rtsp_proc.kill()
                self._rtsp_proc.wait(timeout=2)
            self._rtsp_proc = None
            self._stream = None
        elif hasattr(self, "_alsa_proc") and self._alsa_proc is not None:
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
            except (OSError, RuntimeError) as exc:
                logger.debug("Stream cleanup error: %s", exc)
            self._stream = None

        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

        if self._mqtt_client is not None:
            self._mqtt_client.stop()
            self._mqtt_client = None

        if self._piper_pool is not None:
            self._piper_pool.stop()
            self._piper_pool = None

        # Tear down the Baichuan TTS sink.  Logout first so the NVR
        # releases the session slot, then stop the asyncio loop.
        if self._bc_host is not None and self._bc_loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._bc_host.logout(), self._bc_loop,
                )
                fut.result(timeout=5)
                logger.info("Baichuan logout successful")
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(
                    "Baichuan logout timed out — NVR session may leak",
                )
            except (ConnectionError, OSError) as exc:
                logger.warning("Baichuan logout connection error: %s", exc)
            except asyncio.InvalidStateError as exc:
                logger.debug("Baichuan logout loop state error: %s", exc)
            self._bc_host = None
        if self._bc_loop is not None:
            try:
                self._bc_loop.call_soon_threadsafe(self._bc_loop.stop)
            except RuntimeError as exc:
                logger.debug("Baichuan loop stop error: %s", exc)
            self._bc_loop = None
            self._bc_thread = None

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
        """Stop the satellite daemon on signal."""
        logger.info("Received signal %d — shutting down", sig)
        daemon.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    daemon.start()


if __name__ == "__main__":
    main()
