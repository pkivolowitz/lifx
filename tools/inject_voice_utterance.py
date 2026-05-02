"""Inject a raw-PCM utterance onto glowup/voice/utterance for end-to-end testing.

Bypasses the satellite microphone + wake detector and publishes a fully
formed voice/utterance message directly to the broker.  Used to exercise
the coordinator STT → intent → executor → TTS path without standing in
front of a satellite — the failure mode this addresses is "is the
coordinator pipeline live after a reboot".  The mic, ALSA capture, and
wake detector are NOT covered by injection; those still require a real
voice in the room.

Wire format is the one defined by ``voice.protocol.encode``:
4-byte BE u32 header length + JSON header + raw PCM (16-bit LE mono
16 kHz).

Usage::

    python3 tools/inject_voice_utterance.py \\
        voice/tests/fixtures/hey_glowup_what_day_is_today.wav \\
        --room "Living Room"

The ``--room`` value populates the header so the coordinator's pipeline
treats the response as originating from that satellite — TTS will be
delegated back to that room's speaker.  ``--broker`` defaults to the
hub mosquitto at 10.0.0.214.

Accepts either a WAV (16-bit LE mono 16 kHz) which is parsed via the
``wave`` module, or a raw .pcm file containing already-stripped PCM
samples.  Dispatch is by file extension.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import os
import sys
import time
import wave

_DEFAULT_BROKER: str = "10.0.0.214"
_DEFAULT_ROOM: str = "Living Room"
_DEFAULT_WAKE_SCORE: float = 0.99
_EXPECTED_RATE: int = 16000
_EXPECTED_CHANNELS: int = 1
_EXPECTED_SAMPWIDTH: int = 2  # 16-bit


def _read_pcm(path: str) -> bytes:
    """Return raw 16-bit LE mono 16 kHz PCM samples from ``path``.

    .wav inputs are parsed and validated against the protocol's
    expected format.  .pcm inputs are read verbatim with no validation
    (the caller is asserting the bytes are already in the right shape).

    Args:
        path: WAV file ending in .wav, or raw PCM file ending in .pcm.

    Returns:
        Raw little-endian 16-bit PCM samples.

    Raises:
        ValueError: WAV input is the wrong sample rate / channel count
                    / sample width.
    """
    ext: str = os.path.splitext(path)[1].lower()
    if ext == ".pcm":
        with open(path, "rb") as f:
            return f.read()
    if ext == ".wav":
        with wave.open(path, "rb") as w:
            rate: int = w.getframerate()
            channels: int = w.getnchannels()
            sampwidth: int = w.getsampwidth()
            if rate != _EXPECTED_RATE:
                raise ValueError(
                    f"WAV sample rate {rate} != expected {_EXPECTED_RATE}"
                )
            if channels != _EXPECTED_CHANNELS:
                raise ValueError(
                    f"WAV channels {channels} != expected {_EXPECTED_CHANNELS}"
                )
            if sampwidth != _EXPECTED_SAMPWIDTH:
                raise ValueError(
                    f"WAV sampwidth {sampwidth} != expected "
                    f"{_EXPECTED_SAMPWIDTH} (16-bit)"
                )
            return w.readframes(w.getnframes())
    raise ValueError(f"Unsupported extension {ext!r}; want .wav or .pcm")


def main() -> int:
    """Parse args, build a protocol message, publish."""
    parser = argparse.ArgumentParser(
        description="Inject a voice utterance onto glowup/voice/utterance",
    )
    parser.add_argument(
        "audio",
        help="Path to WAV (16-bit LE mono 16 kHz) or raw .pcm file",
    )
    parser.add_argument(
        "--room",
        default=_DEFAULT_ROOM,
        help=f"Originating room (default: {_DEFAULT_ROOM!r})",
    )
    parser.add_argument(
        "--broker",
        default=_DEFAULT_BROKER,
        help=f"MQTT broker host (default: {_DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--wake-score",
        type=float,
        default=_DEFAULT_WAKE_SCORE,
        help=f"Synthetic wake score in header (default: {_DEFAULT_WAKE_SCORE})",
    )
    args = parser.parse_args()

    # Repo root must be on sys.path so ``voice.protocol`` imports.
    repo_root: str = os.path.abspath(
        os.path.join(os.path.dirname(__file__), ".."),
    )
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from voice import constants as C
    from voice.protocol import encode

    pcm: bytes = _read_pcm(args.audio)
    duration_s: float = len(pcm) / _EXPECTED_SAMPWIDTH / _EXPECTED_RATE

    header: dict = {
        "room": args.room,
        "sample_rate": _EXPECTED_RATE,
        "channels": _EXPECTED_CHANNELS,
        "bit_depth": 8 * _EXPECTED_SAMPWIDTH,
        "timestamp": time.time(),
        "wake_score": args.wake_score,
    }
    payload: bytes = encode(header, pcm)

    print(
        f"injecting {len(pcm)} PCM bytes ({duration_s:.2f}s) "
        f"for room={args.room!r} via {args.broker}"
    )

    import paho.mqtt.client as mqtt

    client = mqtt.Client()
    client.connect(args.broker, 1883, 30)
    client.loop_start()
    info = client.publish(C.TOPIC_UTTERANCE, payload, qos=0)
    info.wait_for_publish(5)
    client.loop_stop()
    client.disconnect()
    print(f"published to {C.TOPIC_UTTERANCE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
