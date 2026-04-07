"""Shared constants for the GlowUp voice system.

All satellites and the coordinator import from here to stay in sync
on MQTT topics, audio format, and timing parameters.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

# ---------------------------------------------------------------------------
# MQTT topics
# ---------------------------------------------------------------------------

# Satellite → coordinator: utterance audio + metadata (single message).
TOPIC_UTTERANCE: str = "glowup/voice/utterance"

# Satellite → coordinator: heartbeat (JSON, every HEARTBEAT_INTERVAL_S).
TOPIC_STATUS_PREFIX: str = "glowup/voice/status"

# Coordinator → satellites: playback state (JSON with room + state).
# Satellites suppress wake detection while their room is playing.
TOPIC_PLAYBACK: str = "glowup/voice/playback"

# Coordinator → satellites: TTS text for local speech synthesis.
# JSON payload: {"room": "...", "text": "..."}.
# Satellites with local TTS (espeak/piper) speak the text through
# their own audio output instead of relying on coordinator playback.
TOPIC_TTS_TEXT: str = "glowup/voice/tts_text"

# Coordinator → satellites: flush all in-flight requests.
# Satellites cancel any pending TTS and bump their generation counter.
# JSON payload: {"timestamp": ...}.
TOPIC_FLUSH: str = "glowup/voice/flush"

# Coordinator → satellites: thinking signal for slow actions.
# Satellite plays a short "working" audio cue locally instead of
# the coordinator sending a separate "Waiting on the assistant" TTS
# message.  Eliminates the two-message preempt path.
# JSON payload: {"room": "...", "timestamp": ...}.
TOPIC_THINKING: str = "glowup/voice/thinking"

# ---------------------------------------------------------------------------
# Audio format — all satellites must produce this format
# ---------------------------------------------------------------------------

# Sample rate in Hz.  16 kHz is the sweet spot for speech recognition:
# high enough for Whisper accuracy, low enough for Pi 3B + MQTT bandwidth.
SAMPLE_RATE: int = 16000

# Mono audio — single channel from the beamformed output.
CHANNELS: int = 1

# 16-bit signed little-endian PCM.
BIT_DEPTH: int = 16

# Bytes per sample (BIT_DEPTH / 8 * CHANNELS).
BYTES_PER_SAMPLE: int = BIT_DEPTH // 8 * CHANNELS

# Audio chunk size for wake word detection (samples per frame).
# 1280 samples at 16 kHz = 80 ms per frame.  openWakeWord expects
# this frame size for inference.
CHUNK_SAMPLES: int = 1280

# Chunk size in bytes.
CHUNK_BYTES: int = CHUNK_SAMPLES * BYTES_PER_SAMPLE

# ---------------------------------------------------------------------------
# Capture parameters
# ---------------------------------------------------------------------------

# Maximum utterance duration (seconds).  Prevents runaway captures if
# silence detection fails.
MAX_UTTERANCE_S: float = 10.0

# Silence timeout (seconds).  Stop capturing after this much silence
# following speech.  1.0s is snappy for commands; increase for
# conversational queries where the speaker pauses to think.
SILENCE_TIMEOUT_S: float = 1.5

# Minimum utterance duration (seconds).  Reject captures shorter than
# this — likely accidental triggers.
MIN_UTTERANCE_S: float = 0.3

# RMS amplitude below which audio is considered silence.
# Tuned for 16-bit PCM; typical quiet room is 50-150 RMS.
# Laptop mics run hotter — use 50 for built-in mics, 200 for ReSpeaker.
SILENCE_RMS_THRESHOLD: int = 50

# Pre-wake audio buffer (seconds).  Keep this much audio before the
# wake word trigger to capture clipped beginnings.
PRE_WAKE_BUFFER_S: float = 0.2

# ---------------------------------------------------------------------------
# Wake word parameters
# ---------------------------------------------------------------------------

# Default wake word confidence threshold (0.0 - 1.0).
WAKE_THRESHOLD: float = 0.20

# VAD threshold for openWakeWord's built-in Silero VAD (0.0 - 1.0).
# Set to 0 to disable.  0.5 is a good starting point.
VAD_THRESHOLD: float = 0.5

# Number of consecutive above-threshold frames required to trigger.
# Prevents transient spikes from firing the wake word.
CONFIDENCE_WINDOW: int = 2

# Cooldown period after wake detection (seconds).  Prevents re-trigger
# during utterance capture or response playback.
# Short cooldown covers only the capture duration + MQTT transit.
# The MQTT playback suppression flag handles the rest — the coordinator
# sends "playing=true" at pipeline start and "playing=false" after
# the last AirPlay stream finishes.
COOLDOWN_S: float = 6.0

# ---------------------------------------------------------------------------
# Coordinator parameters
# ---------------------------------------------------------------------------

# Maximum worker threads for concurrent utterance processing.
MAX_WORKERS: int = 4

# Capabilities refresh interval (seconds).  The coordinator fetches
# available effects, groups, and devices from GlowUp API at this rate
# to build the Ollama system prompt.
CAPABILITIES_REFRESH_S: float = 300.0

# Ollama intent parsing timeout (seconds).
INTENT_TIMEOUT_S: float = 15.0

# Maximum retries for invalid JSON from Ollama.
INTENT_MAX_RETRIES: int = 1

# ---------------------------------------------------------------------------
# Flush command
# ---------------------------------------------------------------------------

# Flush command patterns — matched against STT output (lowercase, stripped).
# The user says "Hey <wake_word> flush it" — only the post-wake utterance
# is transcribed, so we match against the bare phrase.
FLUSH_PATTERNS: frozenset[str] = frozenset({
    "flush it",
    "flush it.",
    "flush",
    "flush.",
})

# ---------------------------------------------------------------------------
# Satellite heartbeat
# ---------------------------------------------------------------------------

# How often each satellite publishes a heartbeat (seconds).
HEARTBEAT_INTERVAL_S: float = 60.0
