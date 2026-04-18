"""Audio sensor — capture microphone audio and stream via UDP.

Standalone sensor script that captures raw PCM audio from the system
microphone using ffmpeg, then streams it to a remote compute node via
UDP for distributed signal processing (FFT, beat detection, etc.).

Runs on any Mac or Linux machine with ffmpeg installed.  Pairs with
a :class:`WorkerAgent` running ``AudioExtractor`` on the receiving end.

Usage::

    python3 -m distributed.audio_sensor --target 192.0.2.63:9420

The script captures mono 16-bit PCM at the configured sample rate,
chunks it into frames (~100ms each by default), and sends each frame
as a UDP datagram using the GlowUp binary wire protocol.

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import logging
import platform
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from .protocol import DTYPE_INT16_PCM
from .udp_channel import UdpSender

logger: logging.Logger = logging.getLogger("glowup.audio_sensor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default sample rate (Hz).  44100 captures full audible range.
DEFAULT_SAMPLE_RATE: int = 44100

# Default channels (mono for signal processing).
DEFAULT_CHANNELS: int = 1

# Default chunk duration in milliseconds.  Controls latency vs. overhead.
# 100ms at 44100 Hz mono 16-bit = 8820 bytes per chunk — well within UDP.
DEFAULT_CHUNK_MS: int = 100

# Bytes per sample for s16le PCM.
BYTES_PER_SAMPLE: int = 2

# Signal name prefix for the PCM stream.
DEFAULT_SIGNAL_NAME: str = "sensor:audio:pcm_raw"

# Reconnect delay after ffmpeg exits unexpectedly (seconds).
RECONNECT_DELAY: float = 2.0

# Maximum consecutive reconnect attempts before giving up.
MAX_RECONNECT_ATTEMPTS: int = 10


# ---------------------------------------------------------------------------
# AudioSensor
# ---------------------------------------------------------------------------

class AudioSensor:
    """Capture microphone audio and stream chunks via UDP.

    Spawns ffmpeg to capture raw PCM from the default audio input,
    then reads chunks in a dedicated thread and sends them via
    :class:`UdpSender` to the configured target(s).

    Args:
        target_ip:   Remote compute node IP address.
        target_port: Remote compute node UDP port.
        sample_rate: Audio sample rate in Hz.
        channels:    Audio channel count (1 = mono).
        chunk_ms:    Chunk duration in milliseconds.
        signal_name: Signal name for the UDP wire protocol header.
        device:      Platform-specific device specifier (optional).
    """

    def __init__(self, target_ip: str, target_port: int,
                 sample_rate: int = DEFAULT_SAMPLE_RATE,
                 channels: int = DEFAULT_CHANNELS,
                 chunk_ms: int = DEFAULT_CHUNK_MS,
                 signal_name: str = DEFAULT_SIGNAL_NAME,
                 device: str = "") -> None:
        """Initialize the audio sensor.

        Args:
            target_ip:   Remote compute node IP.
            target_port: Remote compute node UDP port.
            sample_rate: Sample rate in Hz.
            channels:    Channel count.
            chunk_ms:    Chunk duration in ms.
            signal_name: Signal name for wire protocol.
            device:      Platform device specifier.
        """
        self._target_ip: str = target_ip
        self._target_port: int = target_port
        self._sample_rate: int = sample_rate
        self._channels: int = channels
        self._chunk_ms: int = chunk_ms
        self._signal_name: str = signal_name
        self._device: str = device

        # Compute chunk size in bytes.
        samples_per_chunk: int = (sample_rate * chunk_ms) // 1000
        self._chunk_bytes: int = samples_per_chunk * channels * BYTES_PER_SAMPLE

        # UDP sender.
        self._sender: UdpSender = UdpSender(
            targets=[(target_ip, target_port)],
        )

        # State.
        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self._frames_sent: int = 0
        self._bytes_sent: int = 0
        self._start_time: float = 0.0

    def start(self) -> None:
        """Start capturing audio and streaming via UDP."""
        if self._reader_thread is not None:
            logger.warning("Audio sensor already running")
            return

        self._stop_event.clear()
        self._frames_sent = 0
        self._bytes_sent = 0
        self._start_time = time.monotonic()

        self._reader_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="audio-sensor",
        )
        self._reader_thread.start()

        logger.info(
            "Audio sensor started — %d Hz, %d ch, %d ms chunks → %s:%d",
            self._sample_rate, self._channels, self._chunk_ms,
            self._target_ip, self._target_port,
        )

    def stop(self) -> None:
        """Stop capturing and clean up."""
        self._stop_event.set()
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=3)
            except Exception as exc:
                logger.debug("Error terminating audio capture process: %s", exc)
                try:
                    self._process.kill()
                except Exception as exc2:
                    logger.debug("Error killing audio capture process: %s", exc2)
            self._process = None

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5)
            self._reader_thread = None

        self._sender.close()

        elapsed: float = time.monotonic() - self._start_time
        logger.info(
            "Audio sensor stopped — %d frames, %.1f KB sent in %.1f s",
            self._frames_sent, self._bytes_sent / 1024.0, elapsed,
        )

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build the ffmpeg command for microphone capture.

        Returns:
            Command-line argument list.

        Raises:
            RuntimeError: If the platform is unsupported.
        """
        system: str = platform.system()
        device: str = self._device

        cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

        if system == "Darwin":
            if not device:
                device = ":default"
            cmd.extend(["-f", "avfoundation", "-i", device])
        elif system == "Linux":
            if not device:
                device = "default"
            cmd.extend(["-f", "pulse", "-i", device])
        else:
            raise RuntimeError(
                f"Microphone capture not supported on {system}. "
                f"Supported: macOS (avfoundation), Linux (pulse)."
            )

        cmd.extend([
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            "-f", "s16le",
            "pipe:1",
        ])
        return cmd

    def _capture_loop(self) -> None:
        """Reader thread: spawn ffmpeg, read chunks, send via UDP.

        Reconnects on failure up to MAX_RECONNECT_ATTEMPTS times.
        """
        attempts: int = 0

        while not self._stop_event.is_set():
            try:
                cmd: list[str] = self._build_ffmpeg_cmd()
                logger.info("Starting ffmpeg: %s", " ".join(cmd))

                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                attempts = 0  # Reset on successful start.

                self._read_and_send()

            except Exception as exc:
                logger.error("Audio capture error: %s", exc)

            # Process exited or failed.
            if self._stop_event.is_set():
                break

            attempts += 1
            if attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.error(
                    "Too many reconnect attempts (%d) — giving up",
                    attempts,
                )
                break

            logger.info(
                "Reconnecting in %.1f s (attempt %d/%d)...",
                RECONNECT_DELAY, attempts, MAX_RECONNECT_ATTEMPTS,
            )
            self._stop_event.wait(RECONNECT_DELAY)

    def _read_and_send(self) -> None:
        """Read PCM chunks from ffmpeg stdout and send via UDP."""
        if self._process is None or self._process.stdout is None:
            return

        while not self._stop_event.is_set():
            chunk: bytes = self._process.stdout.read(self._chunk_bytes)
            if not chunk:
                # ffmpeg exited or pipe closed.
                break

            # Send the raw PCM chunk via UDP.
            self._sender.send(
                self._signal_name, chunk,
                dtype=DTYPE_INT16_PCM,
            )
            self._frames_sent += 1
            self._bytes_sent += len(chunk)

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
    """Command-line entry point for the audio sensor."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp Audio Sensor — stream mic audio via UDP",
    )
    parser.add_argument(
        "--target", required=True,
        help="Target compute node as IP:PORT (e.g. 192.0.2.63:9420)",
    )
    parser.add_argument(
        "--rate", type=int, default=DEFAULT_SAMPLE_RATE,
        help=f"Sample rate in Hz (default {DEFAULT_SAMPLE_RATE})",
    )
    parser.add_argument(
        "--channels", type=int, default=DEFAULT_CHANNELS,
        help=f"Audio channels (default {DEFAULT_CHANNELS})",
    )
    parser.add_argument(
        "--chunk-ms", type=int, default=DEFAULT_CHUNK_MS,
        help=f"Chunk duration in ms (default {DEFAULT_CHUNK_MS})",
    )
    parser.add_argument(
        "--signal-name", default=DEFAULT_SIGNAL_NAME,
        help=f"Signal name (default '{DEFAULT_SIGNAL_NAME}')",
    )
    parser.add_argument(
        "--device", default="",
        help="Platform audio device specifier (default: system default)",
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
        return  # Unreachable, but makes type checker happy.

    # Configure logging.
    level: int = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    sensor: AudioSensor = AudioSensor(
        target_ip=target_ip,
        target_port=target_port,
        sample_rate=args.rate,
        channels=args.channels,
        chunk_ms=args.chunk_ms,
        signal_name=args.signal_name,
        device=args.device,
    )

    # Handle Ctrl+C gracefully.
    def _shutdown(signum: int, frame: object) -> None:
        """Signal handler for clean shutdown."""
        logger.info("Shutting down...")
        sensor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    sensor.start()

    # Print stats periodically.
    try:
        while True:
            time.sleep(5.0)
            s: dict[str, float] = sensor.stats
            logger.info(
                "Stats: %d frames, %.1f KB, %.1f fps",
                int(s["frames_sent"]),
                s["bytes_sent"] / 1024.0,
                s["fps"],
            )
    except (KeyboardInterrupt, SystemExit):
        sensor.stop()


if __name__ == "__main__":
    main()
