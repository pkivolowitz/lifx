"""Media source abstraction — raw data producers.

Provides a source-agnostic interface for anything that produces raw media
frames (audio PCM, video pixels).  Concrete implementations use ffmpeg
subprocesses to decode RTSP streams and local files into raw data, which
is then pushed to registered :class:`SignalExtractor` instances.

Each source runs in its own daemon thread, reading the ffmpeg stdout pipe
in a tight loop.  Raw data never touches the SignalBus (too high bandwidth);
instead, chunks are pushed directly to extractors via callback.

Concrete sources:
    RtspSource      — RTSP stream via ffmpeg (cameras, NVR)
    FileSource      — local audio/video file via ffmpeg
    DirectorySource — shuffled directory playback via ffmpeg
    MicSource       — system microphone via ffmpeg (avfoundation / pulse)

Factory function:
    create_source — construct the right source type from config dict
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.2"

import json
import logging
import os
import platform
import random
import select
import shutil
import socket
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.media.source")

# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _resolve_credentials(config: dict[str, Any]) -> str:
    """Build an RTSP URL with credentials from a separate file.

    Supports two config patterns:

    1. Direct URL (not recommended — credentials in config file)::

        {"url": "rtsp://user:pass@camera-host:554/..."}

    2. Credentials file (recommended — keeps secrets out of config)::

        {
            "url": "rtsp://{user}:{password}@camera-host:554/...",
            "credentials_file": "/etc/glowup/rtsp_creds.json"
        }

    The credentials file is a simple JSON object::

        {"user": "admin", "password": "s3cret"}

    File must be owner-readable only (chmod 600).

    Args:
        config: Source configuration dict.

    Returns:
        Fully resolved RTSP URL string.

    Raises:
        ValueError: If URL is missing or credentials file is unreadable.
    """
    url: str = config.get("url", "")
    if not url:
        raise ValueError("RTSP source requires a 'url' field")

    creds_path: str = config.get("credentials_file", "")
    if not creds_path:
        return url

    # Expand ~ and env vars.
    creds_path = os.path.expanduser(creds_path)
    creds_path = os.path.expandvars(creds_path)

    if not os.path.isfile(creds_path):
        raise ValueError(
            f"Credentials file not found: {creds_path}"
        )

    # Warn if file permissions are too open.
    mode: int = os.stat(creds_path).st_mode & 0o777
    if mode & 0o077:
        logger.warning(
            "Credentials file %s has permissions %o — "
            "should be 600 (owner-only read/write)",
            creds_path, mode,
        )

    with open(creds_path, "r") as f:
        creds: dict[str, str] = json.load(f)

    user: str = creds.get("user", "")
    password: str = creds.get("password", "")

    return url.format(user=user, password=password)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default audio sample rate for RTSP sources (Hz).
DEFAULT_SAMPLE_RATE: int = 16000

# Default audio channel count.
DEFAULT_CHANNELS: int = 1

# Chunk size for audio reads (bytes).  At 16 kHz mono 16-bit PCM,
# 3200 bytes = 1600 samples = 100 ms of audio.
AUDIO_CHUNK_BYTES: int = 3200

# Video chunk size (bytes).  For 160x90 RGB24 = 43200 bytes per frame.
VIDEO_FRAME_BYTES: int = 160 * 90 * 3

# Default video resolution for signal extraction (heavily downscaled).
VIDEO_WIDTH: int = 160
VIDEO_HEIGHT: int = 90

# Default video frame rate for extraction (fps).
VIDEO_FPS: int = 4

# Reconnection backoff parameters (seconds).
RECONNECT_INITIAL: float = 1.0
RECONNECT_MAX: float = 60.0
RECONNECT_MULTIPLIER: float = 2.0

# Maximum consecutive read failures before declaring source dead.
MAX_READ_FAILURES: int = 5


# ---------------------------------------------------------------------------
# Extractor callback type
# ---------------------------------------------------------------------------

# Extractors register a callback: (chunk: bytes) -> None
ExtractorCallback = Callable[[bytes], None]


# ---------------------------------------------------------------------------
# MediaSource ABC
# ---------------------------------------------------------------------------

class MediaSource(ABC):
    """Abstract base class for raw media data producers.

    Subclasses implement :meth:`_build_ffmpeg_cmd` to produce the ffmpeg
    command line, and optionally override :meth:`_chunk_size` for custom
    read sizes.  The base class handles threading, pipe reading, extractor
    dispatch, and reconnection logic.

    Attributes:
        name:        Unique identifier from config.
        source_type: Type string (``"rtsp"``, ``"file"``, etc.).
        media_type:  ``"audio"`` or ``"video"``.
    """

    def __init__(self, name: str, source_type: str, media_type: str,
                 config: dict[str, Any]) -> None:
        """Initialize a media source.

        Args:
            name:        Unique source name.
            source_type: Source type identifier.
            media_type:  ``"audio"`` or ``"video"``.
            config:      Source-specific configuration dict.
        """
        self.name: str = name
        self.source_type: str = source_type
        self.media_type: str = media_type
        self._config: dict[str, Any] = config
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._extractors: list[ExtractorCallback] = []
        self._lock: threading.Lock = threading.Lock()

    @property
    def sample_rate(self) -> int:
        """Audio sample rate in Hz.

        Returns:
            Sample rate (default 16000).
        """
        return self._config.get("sample_rate", DEFAULT_SAMPLE_RATE)

    @property
    def channels(self) -> int:
        """Audio channel count.

        Returns:
            Number of audio channels (default 1 = mono).
        """
        return self._config.get("channels", DEFAULT_CHANNELS)

    def add_extractor(self, callback: ExtractorCallback) -> None:
        """Register an extractor callback for raw data chunks.

        Args:
            callback: Function accepting a bytes chunk.
        """
        with self._lock:
            self._extractors.append(callback)

    def inject_chunk(self, chunk: bytes) -> None:
        """Inject a synthetic PCM chunk through the extractor chain.

        Used for calibration pulse injection.  The chunk is dispatched
        to all registered extractors exactly as if it came from the
        ffmpeg pipe, so it travels through both the FFT/SignalBus path
        and the TCP audio stream path simultaneously.

        Thread-safe: acquires the extractor lock.

        Args:
            chunk: Raw PCM bytes to inject.
        """
        with self._lock:
            callbacks: list[ExtractorCallback] = list(self._extractors)
        for cb in callbacks:
            try:
                cb(chunk)
            except Exception as exc:
                logger.error(
                    "Extractor error during inject on '%s': %s",
                    self.name, exc,
                )

    def remove_extractor(self, callback: ExtractorCallback) -> None:
        """Unregister an extractor callback.

        Args:
            callback: Previously registered callback.
        """
        with self._lock:
            try:
                self._extractors.remove(callback)
            except ValueError:
                pass

    def start(self) -> None:
        """Start the source's reader thread and ffmpeg subprocess."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._reader_loop,
            name=f"glowup-media-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the reader thread and kill the ffmpeg subprocess."""
        self._running = False
        self._kill_process()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None

    def is_alive(self) -> bool:
        """Check if the source is running.

        Returns:
            ``True`` if the reader thread is active.
        """
        return self._running and self._thread is not None and self._thread.is_alive()

    @abstractmethod
    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build the ffmpeg command line for this source.

        Returns:
            List of command-line arguments for ``subprocess.Popen``.
        """

    def _chunk_size(self) -> int:
        """Return the number of bytes to read per iteration.

        Returns:
            Chunk size in bytes.
        """
        if self.media_type == "video":
            return VIDEO_FRAME_BYTES
        return AUDIO_CHUNK_BYTES

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_process(self) -> bool:
        """Spawn the ffmpeg subprocess.

        Returns:
            ``True`` if the process started successfully.
        """
        cmd: list[str] = self._build_ffmpeg_cmd()
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            logger.info("Started ffmpeg for source '%s': pid %d",
                        self.name, self._process.pid)
            return True
        except FileNotFoundError:
            logger.error(
                "ffmpeg not found — media sources require ffmpeg. "
                "Install with: brew install ffmpeg (macOS) or "
                "sudo apt install ffmpeg (Linux)"
            )
            return False
        except Exception as exc:
            logger.error("Failed to start ffmpeg for '%s': %s",
                         self.name, exc)
            return False

    def _kill_process(self) -> None:
        """Terminate the ffmpeg subprocess if running."""
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=3.0)
            except Exception:
                pass
            self._process = None

    def _reader_loop(self) -> None:
        """Main reader loop with reconnection logic.

        Reads chunks from the ffmpeg stdout pipe and dispatches them to
        all registered extractor callbacks.  On pipe EOF or read error,
        the subprocess is restarted with exponential backoff.
        """
        backoff: float = RECONNECT_INITIAL
        chunk_size: int = self._chunk_size()

        while self._running:
            # Start or restart the ffmpeg process.
            if not self._start_process():
                # ffmpeg not found — fatal, stop trying.
                self._running = False
                return

            failures: int = 0
            backoff = RECONNECT_INITIAL  # Reset on successful start.

            while self._running and self._process:
                try:
                    chunk: bytes = self._process.stdout.read(chunk_size)
                    if not chunk:
                        # EOF — stream ended or ffmpeg exited.
                        logger.warning(
                            "Source '%s': ffmpeg pipe EOF", self.name
                        )
                        break

                    # Dispatch to extractors.
                    with self._lock:
                        callbacks: list[ExtractorCallback] = list(
                            self._extractors
                        )
                    for cb in callbacks:
                        try:
                            cb(chunk)
                        except Exception as exc:
                            logger.error(
                                "Extractor error on source '%s': %s",
                                self.name, exc
                            )
                    failures = 0

                except Exception as exc:
                    failures += 1
                    logger.error(
                        "Source '%s' read error (%d/%d): %s",
                        self.name, failures, MAX_READ_FAILURES, exc
                    )
                    if failures >= MAX_READ_FAILURES:
                        break

            # Cleanup and reconnect.
            self._kill_process()
            if self._running:
                logger.info(
                    "Source '%s': reconnecting in %.1fs",
                    self.name, backoff
                )
                # Interruptible sleep.
                deadline: float = time.time() + backoff
                while self._running and time.time() < deadline:
                    time.sleep(0.5)
                backoff = min(backoff * RECONNECT_MULTIPLIER, RECONNECT_MAX)

        logger.info("Source '%s': reader loop exited", self.name)


# ---------------------------------------------------------------------------
# RtspSource
# ---------------------------------------------------------------------------

class RtspSource(MediaSource):
    """RTSP stream source via ffmpeg.

    Connects to an RTSP URL (typically a camera or NVR) and pipes raw
    PCM audio or raw video frames to registered extractors.

    Config keys:
        url:         RTSP URL (e.g. ``rtsp://user:pass@camera-host:554/Preview_02_main``)
        stream:      ``"audio"`` or ``"video"`` (default ``"audio"``)
        sample_rate: Audio sample rate in Hz (default 16000)
        channels:    Audio channels (default 1)
    """

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize an RTSP source.

        Args:
            name:   Unique source name.
            config: Source configuration dict.

        Raises:
            ValueError: If ``url`` is missing from config.
        """
        url: str = _resolve_credentials(config)
        media_type: str = config.get("stream", "audio")
        super().__init__(name, "rtsp", media_type, config)
        self._url: str = url

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build ffmpeg command for RTSP stream decoding.

        Returns:
            Command-line arguments list.
        """
        cmd: list[str] = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self._url,
        ]

        if self.media_type == "audio":
            cmd.extend([
                "-vn",                    # Discard video.
                "-acodec", "pcm_s16le",   # Raw 16-bit signed PCM.
                "-ar", str(self.sample_rate),
                "-ac", str(self.channels),
                "-f", "s16le",            # Raw output format.
                "pipe:1",                 # Write to stdout.
            ])
        else:
            # Video: heavily downscaled for signal extraction.
            cmd.extend([
                "-an",                    # Discard audio.
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-s", f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
                "-r", str(VIDEO_FPS),
                "pipe:1",
            ])

        return cmd


# ---------------------------------------------------------------------------
# FileSource
# ---------------------------------------------------------------------------

class FileSource(MediaSource):
    """Local audio/video file source via ffmpeg.

    Reads a file and pipes raw data to extractors.  Useful for testing
    and for music-reactive effects driven by local audio files.

    Config keys:
        path:        Local file path.
        stream:      ``"audio"`` or ``"video"`` (default ``"audio"``)
        loop:        Loop the file (default ``False``)
        sample_rate: Audio sample rate in Hz (default 16000)
    """

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize a file source.

        Args:
            name:   Unique source name.
            config: Source configuration dict.

        Raises:
            ValueError: If ``path`` is missing from config.
        """
        path: str = config.get("path", "")
        if not path:
            raise ValueError(f"File source '{name}' requires a 'path' field")
        media_type: str = config.get("stream", "audio")
        super().__init__(name, "file", media_type, config)
        self._path: str = path
        self._loop: bool = config.get("loop", False)

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build ffmpeg command for file decoding.

        Returns:
            Command-line arguments list.
        """
        cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

        if self._loop:
            cmd.extend(["-stream_loop", "-1"])

        cmd.extend(["-i", self._path])

        if self.media_type == "audio":
            cmd.extend([
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", str(self.sample_rate),
                "-ac", str(self.channels),
                "-f", "s16le",
                "pipe:1",
            ])
        else:
            cmd.extend([
                "-an",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "-s", f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
                "-r", str(VIDEO_FPS),
                "pipe:1",
            ])

        return cmd


# ---------------------------------------------------------------------------
# DirectorySource
# ---------------------------------------------------------------------------

# Audio file extensions recognized by the directory scanner.
AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".mp3", ".flac", ".wav", ".ogg", ".aac", ".m4a", ".wma", ".opus",
    ".aiff", ".alac",
})


class DirectorySource(MediaSource):
    """Shuffled directory playback via ffmpeg.

    Scans a directory (optionally recursive) for audio files, shuffles
    them, and plays them back-to-back.  When all tracks finish, the
    playlist is reshuffled and playback loops from the top.

    Each track runs as a separate ffmpeg subprocess.  On EOF (track
    finished), the next track starts immediately with no gap.

    Config keys:
        path:        Directory path containing audio files.
        recursive:   Scan subdirectories (default ``True``).
        sample_rate: Audio sample rate in Hz (default 44100).
        extensions:  List of file extensions to include (default:
                     all common audio formats).
    """

    # Higher sample rate default for music playback.
    DIR_DEFAULT_SAMPLE_RATE: int = 44100

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize a directory source.

        Args:
            name:   Unique source name.
            config: Source configuration dict.

        Raises:
            ValueError: If ``path`` is missing or not a directory.
        """
        path: str = config.get("path", "")
        if not path:
            raise ValueError(
                f"Directory source '{name}' requires a 'path' field"
            )
        expanded: str = os.path.expanduser(path)
        if not os.path.isdir(expanded):
            raise ValueError(
                f"Directory source '{name}': path is not a directory: "
                f"{expanded}"
            )
        # Default sample rate to 44100 for music.
        if "sample_rate" not in config:
            config = dict(config, sample_rate=self.DIR_DEFAULT_SAMPLE_RATE)
        super().__init__(name, "directory", "audio", config)
        self._dir: str = expanded
        self._recursive: bool = config.get("recursive", True)
        # Optional user-specified extensions override.
        ext_list: Optional[list[str]] = config.get("extensions")
        if ext_list is not None:
            self._extensions: frozenset[str] = frozenset(
                e if e.startswith(".") else f".{e}" for e in ext_list
            )
        else:
            self._extensions = AUDIO_EXTENSIONS
        # The current playlist and position — managed by _reader_loop.
        self._playlist: list[str] = []
        self._track_index: int = 0
        # Currently playing track name for status display.
        self._current_track: str = ""

    @property
    def current_track(self) -> str:
        """The filename of the currently playing track.

        Returns:
            Basename of the current file, or empty string if idle.
        """
        return self._current_track

    def _scan_files(self) -> list[str]:
        """Scan the directory for audio files.

        Returns:
            List of absolute paths to audio files.
        """
        results: list[str] = []
        if self._recursive:
            for root, _dirs, files in os.walk(self._dir):
                for fname in files:
                    if Path(fname).suffix.lower() in self._extensions:
                        results.append(os.path.join(root, fname))
        else:
            for fname in os.listdir(self._dir):
                fpath: str = os.path.join(self._dir, fname)
                if (os.path.isfile(fpath)
                        and Path(fname).suffix.lower() in self._extensions):
                    results.append(fpath)
        return results

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build ffmpeg command for the current track.

        Returns:
            Command-line arguments list.
        """
        path: str = self._playlist[self._track_index]
        cmd: list[str] = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-f", "s16le",
            "pipe:1",
        ]
        return cmd

    def _reader_loop(self) -> None:
        """Override the base reader loop for multi-track playback.

        Scans the directory, shuffles, and plays tracks sequentially.
        On EOF for each track, advances to the next.  When all tracks
        are exhausted, reshuffles and starts over.
        """
        chunk_size: int = self._chunk_size()

        while self._running:
            # Scan and shuffle.
            files: list[str] = self._scan_files()
            if not files:
                logger.error(
                    "Directory source '%s': no audio files found in %s",
                    self.name, self._dir,
                )
                # Wait before retrying (files might appear later).
                deadline: float = time.time() + 30.0
                while self._running and time.time() < deadline:
                    time.sleep(0.5)
                continue

            random.shuffle(files)
            self._playlist = files
            logger.info(
                "Directory source '%s': %d tracks queued (shuffled)",
                self.name, len(files),
            )

            for idx in range(len(files)):
                if not self._running:
                    break
                self._track_index = idx
                track_path: str = files[idx]
                self._current_track = os.path.basename(track_path)
                logger.info(
                    "Directory source '%s': [%d/%d] %s",
                    self.name, idx + 1, len(files),
                    self._current_track,
                )

                if not self._start_process():
                    self._running = False
                    return

                # Read until EOF (track finished).
                while self._running and self._process:
                    try:
                        chunk: bytes = self._process.stdout.read(
                            chunk_size
                        )
                        if not chunk:
                            # Track finished — advance to next.
                            break

                        with self._lock:
                            callbacks: list[ExtractorCallback] = list(
                                self._extractors
                            )
                        for cb in callbacks:
                            try:
                                cb(chunk)
                            except Exception as exc:
                                logger.error(
                                    "Extractor error on '%s' track '%s': %s",
                                    self.name, self._current_track, exc,
                                )
                    except Exception as exc:
                        logger.error(
                            "Directory source '%s' read error: %s",
                            self.name, exc,
                        )
                        break

                self._kill_process()

            # All tracks done — loop: reshuffle and play again.
            if self._running:
                logger.info(
                    "Directory source '%s': playlist complete, reshuffling",
                    self.name,
                )

        self._current_track = ""
        logger.info("Directory source '%s': reader loop exited", self.name)


# ---------------------------------------------------------------------------
# MicSource
# ---------------------------------------------------------------------------

# Default sample rate for microphone capture (Hz).
# 44100 Hz captures the full audible range (up to 22 kHz Nyquist).
MIC_DEFAULT_SAMPLE_RATE: int = 44100


class MicSource(MediaSource):
    """System microphone capture via ffmpeg.

    Captures from the default audio input device using the platform's
    native audio framework:

    * **macOS**: ``avfoundation`` (``-i :default``)
    * **Linux**: ``pulse`` (PulseAudio, ``-i default``)

    On first use, macOS will prompt for microphone permission.

    Config keys:
        sample_rate: Audio sample rate in Hz (default 44100)
        channels:    Audio channels (default 1 = mono)
        device:      Device specifier (default: platform default input)
    """

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize a microphone source.

        Args:
            name:   Unique source name.
            config: Source configuration dict.
        """
        # Default to 44100 Hz for mic (higher than RTSP default).
        if "sample_rate" not in config:
            config = dict(config, sample_rate=MIC_DEFAULT_SAMPLE_RATE)
        super().__init__(name, "mic", "audio", config)
        self._device: str = config.get("device", "")

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build ffmpeg command for microphone capture.

        Returns:
            Command-line arguments list.

        Raises:
            RuntimeError: If the platform is not macOS or Linux.
        """
        system: str = platform.system()
        device: str = self._device

        cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

        if system == "Darwin":
            # macOS: AVFoundation — ":default" = default audio input.
            if not device:
                device = ":default"
            cmd.extend(["-f", "avfoundation", "-i", device])
        elif system == "Linux":
            # Linux: PulseAudio.
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
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-f", "s16le",
            "pipe:1",
        ])

        return cmd


# ---------------------------------------------------------------------------
# TCP Audio Streamer
# ---------------------------------------------------------------------------

# Default TCP port for audio streaming.
DEFAULT_AUDIO_STREAM_PORT: int = 8421


class AudioStreamServer:
    """TCP server that streams raw PCM audio to connected clients.

    Registers as an extractor callback on a :class:`MediaSource`.  Each
    connected TCP client receives the same raw PCM byte stream.  Clients
    that can't keep up are disconnected rather than blocking the source.

    Designed for consumption by ffplay::

        ffplay -f s16le -ar 44100 -ch_layout mono -nodisp tcp://host:8421

    Attributes:
        port: The TCP port the server listens on.
    """

    def __init__(self, port: int = DEFAULT_AUDIO_STREAM_PORT) -> None:
        """Initialize the stream server.

        Args:
            port: TCP port to listen on.
        """
        self.port: int = port
        self._server_sock: Optional[socket.socket] = None
        self._clients: list[socket.socket] = []
        self._lock: threading.Lock = threading.Lock()
        self._accept_thread: Optional[threading.Thread] = None
        self._running: bool = False

    def start(self) -> None:
        """Start listening for TCP connections."""
        if self._running:
            return
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_REUSEADDR, 1,
        )
        self._server_sock.settimeout(1.0)
        self._server_sock.bind(("0.0.0.0", self.port))
        self._server_sock.listen(4)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            daemon=True,
            name="audio-stream-accept",
        )
        self._accept_thread.start()
        logger.info(
            "Audio stream server listening on tcp://0.0.0.0:%d", self.port,
        )

    def stop(self) -> None:
        """Stop the server and disconnect all clients."""
        self._running = False
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        with self._lock:
            for client in self._clients:
                try:
                    client.close()
                except Exception:
                    pass
            self._clients.clear()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=3.0)
            self._accept_thread = None
        logger.info("Audio stream server stopped")

    def on_chunk(self, chunk: bytes) -> None:
        """Extractor callback — broadcast a PCM chunk to all clients.

        Clients that fail to receive are disconnected immediately.

        Args:
            chunk: Raw PCM audio bytes.
        """
        with self._lock:
            dead: list[socket.socket] = []
            for client in self._clients:
                try:
                    client.sendall(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    dead.append(client)
            for client in dead:
                try:
                    client.close()
                except Exception:
                    pass
                self._clients.remove(client)
                logger.info("Audio stream client disconnected")

    def _accept_loop(self) -> None:
        """Accept incoming TCP connections."""
        while self._running:
            try:
                client, addr = self._server_sock.accept()
                # Disable Nagle's algorithm for lower latency.
                client.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1,
                )
                with self._lock:
                    self._clients.append(client)
                logger.info("Audio stream client connected from %s", addr)
            except socket.timeout:
                continue
            except OSError:
                break


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_source(name: str, config: dict[str, Any],
                  bus: Any) -> MediaSource:
    """Create a MediaSource from a config dict.

    Also creates and attaches the default signal extractor(s) for the
    source's media type.

    Args:
        name:   Unique source name.
        config: Source configuration dict with at least a ``"type"`` key.
        bus:    The :class:`SignalBus` for extractor output.

    Returns:
        A configured (but not started) :class:`MediaSource`.

    Raises:
        ValueError: If the source type is not recognized.
    """
    from .extractors import create_extractors  # Deferred import.

    source_type: str = config.get("type", "")

    if source_type == "rtsp":
        source: MediaSource = RtspSource(name, config)
    elif source_type == "file":
        source = FileSource(name, config)
    elif source_type == "directory":
        source = DirectorySource(name, config)
    elif source_type == "mic":
        source = MicSource(name, config)
    else:
        raise ValueError(
            f"Unknown media source type '{source_type}' for '{name}'. "
            f"Supported types: rtsp, file, directory, mic"
        )

    # Attach extractors.
    extractor_configs: dict[str, Any] = config.get("extractors", {})
    extractors = create_extractors(
        source_name=name,
        media_type=source.media_type,
        sample_rate=source.sample_rate,
        extractor_configs=extractor_configs,
        bus=bus,
    )
    for ext in extractors:
        source.add_extractor(ext.process)

    return source
