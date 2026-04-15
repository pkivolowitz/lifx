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

# Hub → satellite: deep health check request.  Broadcast topic; all
# satellites receive every request and each decides whether to reply
# based on the optional "room" filter in the payload.  Payload:
# ``{"id": "<corr-id>", "room": "<target-room>|null}``.  A null or
# missing "room" means every satellite replies.  QoS 1 so a request
# can't be silently dropped under transient network trouble.
TOPIC_HEALTH_REQUEST: str = "glowup/voice/health/request"

# Satellite → hub: deep health reply.  Full topic is
# ``glowup/voice/health/reply/<room_slug>``.  Payload is the JSON
# dict returned by _run_deep_health_check() with the originating
# correlation id echoed so the hub can correlate requests to
# replies — see voice/satellite/daemon.py _publish_health_reply.
TOPIC_HEALTH_REPLY_PREFIX: str = "glowup/voice/health/reply"

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

# ---------------------------------------------------------------------------
# Satellite deep health probe — subsystem staleness thresholds
# ---------------------------------------------------------------------------
# These thresholds are interpreted by the satellite's
# _run_deep_health_check().  Each check reports "ok" iff the
# corresponding monotonic timestamp was updated within the threshold.
# Tune per-subsystem — audio capture should be extremely fresh (ms
# scale), wake inference only slightly staler, utterance publishes
# can go long without being wrong (no one has spoken).

# Max age (s) of the most recent raw PCM frame delivered by the
# capture thread before the audio pipeline is considered dead.
# Capture runs continuously at CHUNK_SAMPLES/SAMPLE_RATE cadence
# (80 ms per frame), so 5s is >60 frames of slack.
SAT_AUDIO_FRAME_STALE_S: float = 5.0

# Max age (s) of the most recent wake-word inference evaluation.
# Wake inference runs once per audio frame; same cadence as above.
SAT_WAKE_EVAL_STALE_S: float = 5.0

# Max age (s) after which the absence of an utterance publish is
# notable but not a failure — reported as a duration, not a
# pass/fail, because "nobody has spoken" is a valid silent state.
SAT_UTTERANCE_IDLE_WARN_S: float = 3600.0

# Hub-side staleness threshold for satellite heartbeats.  Longer
# than HEARTBEAT_INTERVAL_S so a single dropped heartbeat (network
# hiccup) doesn't flip the satellite to unhealthy.  Covers up to
# three consecutive missed heartbeats.
SAT_HEARTBEAT_STALE_S: float = 3 * HEARTBEAT_INTERVAL_S + 10.0

# Hub-side periodic deep-check interval.  Every this many seconds
# the hub publishes a broadcast TOPIC_HEALTH_REQUEST and collects
# whatever replies arrive before the next cycle.  5 minutes is the
# sweet spot — fast enough to catch a hung subsystem inside one
# debugging session, slow enough that it never floods the bus.
HUB_SATELLITE_PROBE_INTERVAL_S: float = 300.0

# How long the hub waits for synchronous deep-check replies on an
# on-demand POST /api/satellites/{room}/health/check.  Must comfortably
# exceed the worst-case _run_deep_health_check() latency (audio
# probe + module imports + MQTT round-trip).
HUB_SATELLITE_PROBE_TIMEOUT_S: float = 5.0

# ---------------------------------------------------------------------------
# Voice gate — default-off mic gating for untrusted satellites
# ---------------------------------------------------------------------------

# Retained MQTT topic prefix for per-room gate state.  Full topic is
# ``glowup/voice/gate/<room_slug>`` where room_slug = room name lowercased
# with spaces replaced by underscores.  Payload is JSON
# ``{"enabled": bool, "expires_at": <unix_ts>}``, retained so a
# restarting satellite recovers its last known gate state.
TOPIC_VOICE_GATE_PREFIX: str = "glowup/voice/gate"

# Hard cap on how long a gate can be held open in a single enable.
# Any requested duration longer than this is clamped to this value,
# with a spoken acknowledgement.  Perry's rule 2026-04-11: never
# silently accept an open-ended gate.  Two hours is the ceiling.
VOICE_GATE_MAX_SECONDS: int = 2 * 60 * 60

# Hardcoded allowlist of interior rooms permitted to enable a voice
# gate.  Hardcoded (not config-driven) so an exterior satellite that
# somehow bypasses its own gate cannot enable itself or another
# exterior satellite.  Room names are matched exactly (case-sensitive)
# against the ``room`` field of the originating utterance.
VOICE_GATE_ALLOWED_ROOMS: frozenset[str] = frozenset({
    "Dining Room",
    "Main Bedroom",
})
