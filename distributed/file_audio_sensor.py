"""File audio sensor — decode audio files and stream via UDP.

Reads any audio file (MP3, WAV, FLAC, OGG, AAC, etc.) using ffmpeg,
streams raw PCM to the FFT pipeline via UDP, and simultaneously
plays through speakers so the music and lights are synchronized.

This is the file-based counterpart of :class:`AudioSensor` (which
captures from a microphone).  Both produce the same UDP wire format,
so the existing ``AudioExtractor`` (FFT) works without changes.

Usage::

    python3 -m distributed.file_audio_sensor \\
        --file song.mp3 --target 192.0.2.63:9420

The ``--target`` is the compute node running ``AudioExtractor``
(FFT → MQTT bands → lights).  For local FFT (no remote node),
point to localhost.

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .protocol import DTYPE_INT16_PCM
from .udp_channel import UdpSender

logger: logging.Logger = logging.getLogger("glowup.file_audio_sensor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default sample rate (Hz).
DEFAULT_SAMPLE_RATE: int = 44100

# Default channels (mono for signal processing).
DEFAULT_CHANNELS: int = 1

# Default chunk duration in milliseconds.
DEFAULT_CHUNK_MS: int = 100

# Bytes per sample for s16le PCM.
BYTES_PER_SAMPLE: int = 2

# Signal name for the PCM stream.
DEFAULT_SIGNAL_NAME: str = "sensor:audio:pcm_raw"

# Default speed multiplier (1.0 = real-time).
DEFAULT_SPEED: float = 1.0


# ---------------------------------------------------------------------------
# FileAudioSensor
# ---------------------------------------------------------------------------

class FileAudioSensor:
    """Decode an audio file and stream PCM chunks via UDP.

    Uses ffmpeg to decode any audio format to raw PCM, then streams
    chunks to a remote compute node (or localhost) using the same
    UDP wire protocol as :class:`AudioSensor`.

    Optionally plays the audio through speakers using ffplay so the
    user hears the music synchronized with the lights.

    Args:
        file_path:    Path to the audio file.
        target_ip:    Remote compute node IP address.
        target_port:  Remote compute node UDP port.
        sample_rate:  Audio sample rate in Hz.
        channels:     Audio channel count (1 = mono).
        chunk_ms:     Chunk duration in milliseconds.
        signal_name:  Signal name for the UDP wire protocol header.
        play_audio:   If True, play through speakers simultaneously.
        speed:        Playback speed multiplier (1.0 = normal).
    """

    def __init__(self, file_path: str, target_ip: str, target_port: int,
                 sample_rate: int = DEFAULT_SAMPLE_RATE,
                 channels: int = DEFAULT_CHANNELS,
                 chunk_ms: int = DEFAULT_CHUNK_MS,
                 signal_name: str = DEFAULT_SIGNAL_NAME,
                 play_audio: bool = True,
                 speed: float = DEFAULT_SPEED) -> None:
        """Initialize the file audio sensor.

        Args:
            file_path:    Path to the audio file.
            target_ip:    Compute node IP.
            target_port:  Compute node UDP port.
            sample_rate:  Sample rate in Hz.
            channels:     Channel count.
            chunk_ms:     Chunk duration in ms.
            signal_name:  Signal name for wire protocol.
            play_audio:   Play through speakers.
            speed:        Playback speed.
        """
        self._file_path: str = file_path
        self._target_ip: str = target_ip
        self._target_port: int = target_port
        self._sample_rate: int = sample_rate
        self._channels: int = channels
        self._chunk_ms: int = chunk_ms
        self._signal_name: str = signal_name
        self._play_audio: bool = play_audio
        self._speed: float = speed

        # Compute chunk size in bytes.
        samples_per_chunk: int = (sample_rate * chunk_ms) // 1000
        self._chunk_bytes: int = samples_per_chunk * channels * BYTES_PER_SAMPLE

        # UDP sender.
        self._sender: UdpSender = UdpSender(
            targets=[(target_ip, target_port)],
        )

        # State.
        self._decode_process: Optional[subprocess.Popen] = None
        self._play_process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self._frames_sent: int = 0
        self._bytes_sent: int = 0
        self._start_time: float = 0.0

    def start(self) -> None:
        """Start decoding, streaming, and optionally playing audio."""
        path: Path = Path(self._file_path)
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {path}")

        self._stop_event.clear()
        self._frames_sent = 0
        self._bytes_sent = 0
        self._start_time = time.monotonic()

        # Start audio playback if requested.
        if self._play_audio:
            self._start_playback()

        # Start the decode + stream thread.
        self._reader_thread = threading.Thread(
            target=self._decode_and_stream,
            daemon=True,
            name="file-audio-sensor",
        )
        self._reader_thread.start()

        logger.info(
            "File audio sensor started — %s → %s:%d (%d Hz, %d ch, %d ms chunks)",
            path.name, self._target_ip, self._target_port,
            self._sample_rate, self._channels, self._chunk_ms,
        )

    def stop(self) -> None:
        """Stop decoding and clean up."""
        self._stop_event.set()

        for proc in (self._decode_process, self._play_process):
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        self._decode_process = None
        self._play_process = None

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5)
            self._reader_thread = None

        self._sender.close()

        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "File audio sensor stopped — %d frames, %.1f KB in %.1f s",
            self._frames_sent, self._bytes_sent / 1024.0, elapsed,
        )

    def _start_playback(self) -> None:
        """Start ffplay to play the audio through speakers.

        Runs in the background — the decode thread handles timing.
        """
        cmd: list[str] = [
            "ffplay", "-hide_banner", "-loglevel", "error",
            "-nodisp", "-autoexit",
        ]
        if self._speed != 1.0:
            cmd.extend(["-af", f"atempo={self._speed}"])
        cmd.append(self._file_path)

        try:
            self._play_process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("Playing audio through speakers")
        except FileNotFoundError:
            logger.warning(
                "ffplay not found — audio will stream to lights but "
                "not play through speakers. Install ffmpeg for playback."
            )

    def _decode_and_stream(self) -> None:
        """Decode the file to PCM and stream chunks via UDP."""
        cmd: list[str] = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", self._file_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            "-f", "s16le",
            "pipe:1",
        ]

        try:
            self._decode_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            logger.error("ffmpeg not found — install with: brew install ffmpeg")
            return

        # Compute the real-time interval per chunk for pacing.
        chunk_duration: float = self._chunk_ms / 1000.0
        wall_start: float = time.monotonic()
        chunk_index: int = 0

        while not self._stop_event.is_set():
            chunk: bytes = self._decode_process.stdout.read(self._chunk_bytes)
            if not chunk:
                # End of file.
                break

            # Send via UDP.
            self._sender.send(
                self._signal_name, chunk, dtype=DTYPE_INT16_PCM,
            )
            self._frames_sent += 1
            self._bytes_sent += len(chunk)
            chunk_index += 1

            # Pace to real-time (adjusted for speed).
            target_time: float = wall_start + (chunk_index * chunk_duration / self._speed)
            now: float = time.monotonic()
            sleep_s: float = target_time - now
            if sleep_s > 0.001:
                time.sleep(sleep_s - 0.001)
            while time.monotonic() < target_time:
                pass

        logger.info("File decode complete — %d frames sent", self._frames_sent)

    @property
    def stats(self) -> dict[str, float]:
        """Current statistics.

        Returns:
            Dict with frames_sent, bytes_sent, elapsed_s, fps.
        """
        elapsed: float = time.monotonic() - self._start_time
        fps: float = self._frames_sent / elapsed if elapsed > 0 else 0.0
        return {
            "frames_sent": self._frames_sent,
            "bytes_sent": self._bytes_sent,
            "elapsed_s": elapsed,
            "fps": fps,
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Command-line entry point for the file audio sensor."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "GlowUp File Audio Sensor — decode audio files and stream "
            "PCM to the FFT pipeline via UDP"
        ),
    )
    parser.add_argument(
        "--file", required=True,
        help="Path to an audio file (MP3, WAV, FLAC, OGG, AAC, etc.)",
    )
    parser.add_argument(
        "--target", required=True,
        help="Target compute node as IP:PORT (e.g. 192.0.2.63:9420)",
    )
    parser.add_argument(
        "--rate", type=int, default=DEFAULT_SAMPLE_RATE,
        help=f"Sample rate in Hz (default: {DEFAULT_SAMPLE_RATE})",
    )
    parser.add_argument(
        "--channels", type=int, default=DEFAULT_CHANNELS,
        help=f"Audio channels (default: {DEFAULT_CHANNELS})",
    )
    parser.add_argument(
        "--chunk-ms", type=int, default=DEFAULT_CHUNK_MS,
        help=f"Chunk duration in ms (default: {DEFAULT_CHUNK_MS})",
    )
    parser.add_argument(
        "--signal-name", default=DEFAULT_SIGNAL_NAME,
        help=f"Signal name (default: '{DEFAULT_SIGNAL_NAME}')",
    )
    parser.add_argument(
        "--no-play", dest="play", action="store_false",
        help="Don't play audio through speakers (stream only)",
    )
    parser.add_argument(
        "--speed", type=float, default=DEFAULT_SPEED,
        help=f"Playback speed multiplier (default: {DEFAULT_SPEED})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )

    args: argparse.Namespace = parser.parse_args()

    # Parse target.
    try:
        ip_str, port_str = args.target.rsplit(":", 1)
        target_ip: str = ip_str
        target_port: int = int(port_str)
    except (ValueError, IndexError):
        parser.error("--target must be IP:PORT (e.g. 192.0.2.63:9420)")
        return

    # Configure logging.
    level: int = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    sensor: FileAudioSensor = FileAudioSensor(
        file_path=args.file,
        target_ip=target_ip,
        target_port=target_port,
        sample_rate=args.rate,
        channels=args.channels,
        chunk_ms=args.chunk_ms,
        signal_name=args.signal_name,
        play_audio=args.play,
        speed=args.speed,
    )

    # Handle Ctrl+C.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        logger.info("Shutting down...")
        sensor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sensor.start()

    # Wait for completion or Ctrl+C.
    try:
        while sensor._reader_thread and sensor._reader_thread.is_alive():
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        pass

    sensor.stop()


if __name__ == "__main__":
    main()
