"""Screen capture source with adaptive resolution and Gaussian pyramid.

Captures the primary display via ffmpeg (platform-specific) and produces
raw RGB24 frames.  Each frame is downsampled through a Gaussian pyramid
for multi-resolution analysis:

    Level 0: capture resolution (adaptive, default 640x360)
    Level 1: half (320x180)
    Level 2: quarter (160x90)
    Level 3: analysis target (32x18)

The VisionExtractor runs against the pyramid levels — fast analysis
at the bottom, reaching up for spatial detail when needed.

The capture resolution adapts based on processing time.  If frame
processing consistently takes less than half the frame interval,
resolution increases.  If it exceeds the frame interval, resolution
decreases.  This keeps the gaming PC responsive.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import platform
import subprocess
import threading
import time
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.media.screen")

# ---------------------------------------------------------------------------
# Optional dependency detection
# ---------------------------------------------------------------------------

try:
    import numpy as np
    _HAS_NUMPY: bool = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Adaptive resolution levels (width, height).  The source starts at
# the default and moves up or down based on processing headroom.
RESOLUTION_LADDER: list[tuple[int, int]] = [
    (320, 180),
    (640, 360),
    (960, 540),
    (1280, 720),
]

# Default starting index in the resolution ladder.
DEFAULT_RESOLUTION_INDEX: int = 1  # 640x360

# Target capture frame rate (Hz).  Higher than RTSP's 4 fps because
# screen content changes faster than security cameras.
DEFAULT_SCREEN_FPS: int = 15

# Number of Gaussian pyramid levels to build.
PYRAMID_LEVELS: int = 4

# Analysis target resolution (bottom of pyramid).
ANALYSIS_WIDTH: int = 32
ANALYSIS_HEIGHT: int = 18

# Adaptive resolution tuning.
# If avg processing time < frame_interval * this, consider going up.
HEADROOM_THRESHOLD_UP: float = 0.4
# If avg processing time > frame_interval * this, go down.
HEADROOM_THRESHOLD_DOWN: float = 0.8
# Number of frames to average before adapting.
ADAPTATION_WINDOW: int = 30

# Reconnection parameters.
RECONNECT_INITIAL: float = 1.0
RECONNECT_MAX: float = 30.0
RECONNECT_MULTIPLIER: float = 2.0


# ---------------------------------------------------------------------------
# Gaussian pyramid
# ---------------------------------------------------------------------------

def _downsample_numpy(
    frame: "np.ndarray", target_w: int, target_h: int,
) -> "np.ndarray":
    """Downsample an RGB frame using numpy area averaging.

    This is a fast, decent-quality downsampler that approximates a
    Gaussian pyramid level by averaging rectangular blocks of pixels.

    Args:
        frame:    RGB frame as numpy array (H, W, 3), dtype uint8.
        target_w: Target width.
        target_h: Target height.

    Returns:
        Downsampled frame as numpy array (target_h, target_w, 3).
    """
    h, w = frame.shape[:2]
    if h == target_h and w == target_w:
        return frame

    # Block averaging: reshape into blocks and take the mean.
    # Truncate to exact multiple of target dimensions.
    block_h: int = h // target_h
    block_w: int = w // target_w
    crop_h: int = block_h * target_h
    crop_w: int = block_w * target_w
    cropped = frame[:crop_h, :crop_w]

    # Reshape: (target_h, block_h, target_w, block_w, 3) → mean over blocks.
    reshaped = cropped.reshape(target_h, block_h, target_w, block_w, 3)
    return reshaped.mean(axis=(1, 3)).astype(np.uint8)


def _downsample_python(
    pixels: list[int], w: int, h: int, target_w: int, target_h: int,
) -> list[int]:
    """Downsample RGB pixels using pure Python box filter.

    Fallback for when numpy is not available.

    Args:
        pixels:   Flat list of RGB values [r,g,b,r,g,b,...].
        w:        Source width.
        h:        Source height.
        target_w: Target width.
        target_h: Target height.

    Returns:
        Flat list of downsampled RGB values.
    """
    block_w: int = w // target_w
    block_h: int = h // target_h
    result: list[int] = []

    for ty in range(target_h):
        for tx in range(target_w):
            r_sum: int = 0
            g_sum: int = 0
            b_sum: int = 0
            count: int = 0
            for by in range(block_h):
                for bx in range(block_w):
                    sy: int = ty * block_h + by
                    sx: int = tx * block_w + bx
                    idx: int = (sy * w + sx) * 3
                    r_sum += pixels[idx]
                    g_sum += pixels[idx + 1]
                    b_sum += pixels[idx + 2]
                    count += 1
            result.extend([r_sum // count, g_sum // count, b_sum // count])

    return result


def build_pyramid(
    frame_bytes: bytes, width: int, height: int, levels: int = PYRAMID_LEVELS,
) -> list[Any]:
    """Build a Gaussian downsample pyramid from a raw RGB24 frame.

    Args:
        frame_bytes: Raw RGB24 bytes (width * height * 3).
        width:       Frame width.
        height:      Frame height.
        levels:      Number of pyramid levels (including the original).

    Returns:
        List of frames from highest resolution (index 0) to lowest.
        Each frame is a numpy array (H, W, 3) if numpy is available,
        otherwise a dict with 'pixels' (flat list), 'w', 'h'.
    """
    if _HAS_NUMPY:
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(
            height, width, 3
        )
        pyramid: list[Any] = [frame]
        current = frame
        for _ in range(levels - 1):
            new_h: int = max(1, current.shape[0] // 2)
            new_w: int = max(1, current.shape[1] // 2)
            current = _downsample_numpy(current, new_w, new_h)
            pyramid.append(current)
        return pyramid
    else:
        pixels: list[int] = list(frame_bytes)
        pyramid_py: list[Any] = [
            {"pixels": pixels, "w": width, "h": height}
        ]
        cur_pixels = pixels
        cur_w, cur_h = width, height
        for _ in range(levels - 1):
            new_w = max(1, cur_w // 2)
            new_h = max(1, cur_h // 2)
            cur_pixels = _downsample_python(
                cur_pixels, cur_w, cur_h, new_w, new_h
            )
            cur_w, cur_h = new_w, new_h
            pyramid_py.append(
                {"pixels": cur_pixels, "w": cur_w, "h": cur_h}
            )
        return pyramid_py


# ---------------------------------------------------------------------------
# ScreenSource
# ---------------------------------------------------------------------------

# Callback type: receives the pyramid (list of frames) each capture.
PyramidCallback = Callable[[list[Any], int, int], None]


class ScreenSource:
    """Screen capture source with adaptive resolution.

    Captures the primary display via ffmpeg, builds a Gaussian
    downsample pyramid per frame, and dispatches the pyramid to
    registered callbacks (VisionExtractor).

    Unlike MediaSource (which handles audio chunks), ScreenSource
    deals with complete video frames and pyramid construction.

    Config keys:
        fps:              Capture frame rate (default 15).
        resolution_index: Starting index in RESOLUTION_LADDER (default 1).
        device:           Platform-specific capture device specifier.
        adaptive:         Enable adaptive resolution (default True).
    """

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize the screen capture source.

        Args:
            name:   Unique source name (e.g. "screen").
            config: Source configuration dict.
        """
        self.name: str = name
        self._fps: int = config.get("fps", DEFAULT_SCREEN_FPS)
        self._res_index: int = config.get(
            "resolution_index", DEFAULT_RESOLUTION_INDEX
        )
        self._device: str = config.get("device", "")
        self._adaptive: bool = config.get("adaptive", True)

        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._callbacks: list[PyramidCallback] = []
        self._lock: threading.Lock = threading.Lock()

        # Adaptive resolution state.
        self._frame_times: list[float] = []

    @property
    def width(self) -> int:
        """Current capture width."""
        return RESOLUTION_LADDER[self._res_index][0]

    @property
    def height(self) -> int:
        """Current capture height."""
        return RESOLUTION_LADDER[self._res_index][1]

    @property
    def fps(self) -> int:
        """Capture frame rate."""
        return self._fps

    def add_callback(self, callback: PyramidCallback) -> None:
        """Register a pyramid callback.

        Args:
            callback: Function accepting (pyramid, width, height).
        """
        with self._lock:
            self._callbacks.append(callback)

    def remove_callback(self, callback: PyramidCallback) -> None:
        """Unregister a pyramid callback.

        Args:
            callback: Previously registered callback.
        """
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

    def start(self) -> None:
        """Start capturing the screen."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"screen-capture-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop capturing."""
        self._running = False
        self._kill_process()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None

    def is_alive(self) -> bool:
        """Check if capture is running."""
        return (self._running and self._thread is not None
                and self._thread.is_alive())

    # ------------------------------------------------------------------
    # ffmpeg command construction
    # ------------------------------------------------------------------

    def _build_ffmpeg_cmd(self) -> list[str]:
        """Build the ffmpeg command for screen capture.

        Returns:
            Command-line arguments list.

        Raises:
            RuntimeError: If the platform is not supported.
        """
        system: str = platform.system()
        w, h = self.width, self.height
        device: str = self._device

        cmd: list[str] = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
        ]

        if system == "Darwin":
            # macOS: AVFoundation screen capture.
            if not device:
                device = "Capture screen 0"
            cmd.extend([
                "-f", "avfoundation",
                "-framerate", str(self._fps),
                "-i", device,
            ])
        elif system == "Linux":
            # Linux: X11 screen grab.
            if not device:
                device = ":0.0"
            cmd.extend([
                "-f", "x11grab",
                "-framerate", str(self._fps),
                "-i", device,
            ])
        else:
            raise RuntimeError(
                f"Screen capture not supported on {system}. "
                f"Supported: macOS (avfoundation), Linux (x11grab)."
            )

        # Output: raw RGB24, scaled to current resolution.
        cmd.extend([
            "-vf", f"scale={w}:{h}",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ])

        return cmd

    # ------------------------------------------------------------------
    # Capture loop
    # ------------------------------------------------------------------

    def _kill_process(self) -> None:
        """Terminate the ffmpeg subprocess."""
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=3.0)
            except Exception:
                pass
            self._process = None

    def _capture_loop(self) -> None:
        """Main capture loop: read frames, build pyramids, dispatch.

        Handles ffmpeg lifecycle, adaptive resolution, and
        reconnection on failure.
        """
        backoff: float = RECONNECT_INITIAL

        while self._running:
            # Start ffmpeg.
            cmd: list[str] = self._build_ffmpeg_cmd()
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=691200,
                )
                logger.info(
                    "Screen capture started: %dx%d @ %d fps (pid %d)",
                    self.width, self.height, self._fps,
                    self._process.pid,
                )
            except FileNotFoundError:
                logger.error(
                    "ffmpeg not found — screen capture requires ffmpeg"
                )
                self._running = False
                return
            except Exception as exc:
                logger.error("Failed to start screen capture: %s", exc)
                self._running = False
                return

            backoff = RECONNECT_INITIAL
            frame_size: int = self.width * self.height * 3

            while self._running and self._process:
                t_start: float = time.monotonic()

                # Read one complete frame, accumulating partial reads.
                try:
                    chunks: list[bytes] = []
                    remaining: int = frame_size
                    while remaining > 0:
                        chunk: bytes = self._process.stdout.read(remaining)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    frame_bytes: bytes = b"".join(chunks)
                except Exception:
                    break

                if len(frame_bytes) < frame_size:
                    logger.warning(
                        "Screen capture '%s': incomplete frame (EOF?)",
                        self.name,
                    )
                    break

                # Build the Gaussian pyramid.
                pyramid: list[Any] = build_pyramid(
                    frame_bytes, self.width, self.height,
                )

                # Dispatch to callbacks.
                with self._lock:
                    cbs: list[PyramidCallback] = list(self._callbacks)
                for cb in cbs:
                    try:
                        cb(pyramid, self.width, self.height)
                    except Exception as exc:
                        logger.error(
                            "Screen callback error on '%s': %s",
                            self.name, exc,
                        )

                # Track processing time for adaptive resolution.
                t_elapsed: float = time.monotonic() - t_start
                if self._adaptive:
                    self._adapt_resolution(t_elapsed)

            # Cleanup and reconnect.
            self._kill_process()
            if self._running:
                logger.info(
                    "Screen capture '%s': reconnecting in %.1fs",
                    self.name, backoff,
                )
                deadline: float = time.time() + backoff
                while self._running and time.time() < deadline:
                    time.sleep(0.5)
                backoff = min(
                    backoff * RECONNECT_MULTIPLIER, RECONNECT_MAX
                )

        logger.info("Screen capture '%s': stopped", self.name)

    # ------------------------------------------------------------------
    # Adaptive resolution
    # ------------------------------------------------------------------

    def _adapt_resolution(self, frame_time: float) -> None:
        """Adjust capture resolution based on processing headroom.

        Tracks the average frame processing time over a window.  If
        the average is consistently below the headroom threshold, the
        resolution increases.  If it exceeds the threshold, it
        decreases.

        Changing resolution restarts the ffmpeg process on the next
        frame read failure (the current process produces the old
        resolution).

        Args:
            frame_time: Time in seconds to process the last frame.
        """
        self._frame_times.append(frame_time)
        if len(self._frame_times) < ADAPTATION_WINDOW:
            return

        avg: float = sum(self._frame_times) / len(self._frame_times)
        self._frame_times.clear()

        frame_interval: float = 1.0 / self._fps

        if avg < frame_interval * HEADROOM_THRESHOLD_UP:
            # Room to go higher.
            if self._res_index < len(RESOLUTION_LADDER) - 1:
                self._res_index += 1
                logger.info(
                    "Screen capture adaptive: UP to %dx%d "
                    "(avg %.1fms, budget %.1fms)",
                    self.width, self.height,
                    avg * 1000, frame_interval * 1000,
                )
                self._kill_process()  # Force restart at new resolution.
        elif avg > frame_interval * HEADROOM_THRESHOLD_DOWN:
            # Struggling — go lower.
            if self._res_index > 0:
                self._res_index -= 1
                logger.info(
                    "Screen capture adaptive: DOWN to %dx%d "
                    "(avg %.1fms, budget %.1fms)",
                    self.width, self.height,
                    avg * 1000, frame_interval * 1000,
                )
                self._kill_process()  # Force restart at new resolution.
