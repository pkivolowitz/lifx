"""Media source abstraction — raw data producers.

Provides a source-agnostic interface for anything that produces raw media
frames (audio PCM, video pixels).  Concrete implementations use ffmpeg
subprocesses to decode RTSP streams and local files into raw data, which
is then pushed to registered :class:`SignalExtractor` instances.

Each source runs in its own daemon thread, reading the ffmpeg stdout pipe
in a tight loop.  Raw data never touches the SignalBus (too high bandwidth);
instead, chunks are pushed directly to extractors via callback.

Concrete sources:
    RtspSource  — RTSP stream via ffmpeg (cameras, NVR)
    FileSource  — local audio/video file via ffmpeg

Factory function:
    create_source — construct the right source type from config dict
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.media.source")

# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def _resolve_credentials(config: dict[str, Any]) -> str:
    """Build an RTSP URL with credentials from a separate file.

    Supports two config patterns:

    1. Direct URL (not recommended — credentials in config file)::

        {"url": "rtsp://admin:pass@10.0.0.39:554/..."}

    2. Credentials file (recommended — keeps secrets out of config)::

        {
            "url": "rtsp://{user}:{password}@10.0.0.39:554/...",
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
        url:         RTSP URL (e.g. ``rtsp://admin:pass@10.0.0.39:554/Preview_02_main``)
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
    else:
        raise ValueError(
            f"Unknown media source type '{source_type}' for '{name}'. "
            f"Supported types: rtsp, file"
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
