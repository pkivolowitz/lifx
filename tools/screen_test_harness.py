#!/usr/bin/env python3
"""Screen-reactive test harness.

Opens two windows:
1. A small "TV" window playing a movie via ffmpeg/SDL
2. A larger "room" window behind it showing simulated light zones
   as a colored border around the TV — exactly what a LIFX strip
   behind a real TV would look like.

The harness captures the TV window's content, runs VisionExtractor,
and renders the ScreenLight effect onto the border zones in real time.

Supports two modes:
  --mode strip    Multizone strip (spatial edge colors, default)
  --mode bulb     Single bulb (uniform dominant color + energy)

Usage:
    python3 tools/screen_test_harness.py --movie /path/to/movie.mp4
    python3 tools/screen_test_harness.py --movie /path/to/movie.mp4 --mode bulb

Requirements:
    - pygame
    - numpy
    - ffmpeg (for movie decoding)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import math
import subprocess
import sys
import threading
import time
from typing import Any, Optional

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy is required. Install with: pip install numpy",
          file=sys.stderr)
    sys.exit(1)

try:
    import pygame
except ImportError:
    print("ERROR: pygame is required. Install with: pip install pygame",
          file=sys.stderr)
    sys.exit(1)

# Add project root to path.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media.vision import VisionExtractor
from media.screen_source import build_pyramid
from media import SignalBus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# TV window size (the "screen" being watched).
TV_WIDTH: int = 640
TV_HEIGHT: int = 360

# Border thickness around the TV (simulated strip width).
BORDER_PX: int = 60

# Room window size (TV + border on all sides).
ROOM_WIDTH: int = TV_WIDTH + BORDER_PX * 2
ROOM_HEIGHT: int = TV_HEIGHT + BORDER_PX * 2

# Number of edge zones for the strip simulation.
STRIP_ZONES: int = 60

# Movie decode FPS.
MOVIE_FPS: int = 24

# Background color for the room (dark, like an actual room).
ROOM_BG: tuple[int, int, int] = (15, 15, 20)


# ---------------------------------------------------------------------------
# Color conversion
# ---------------------------------------------------------------------------

def hsb_to_rgb(h: float, s: float, b: float) -> tuple[int, int, int]:
    """Convert HSB [0,1] to RGB [0,255].

    Args:
        h: Hue [0, 1].
        s: Saturation [0, 1].
        b: Brightness [0, 1].

    Returns:
        (r, g, b) each [0, 255].
    """
    if s == 0.0:
        v: int = int(b * 255)
        return (v, v, v)

    h6: float = h * 6.0
    i: int = int(h6) % 6
    f: float = h6 - int(h6)
    p: float = b * (1.0 - s)
    q: float = b * (1.0 - s * f)
    t: float = b * (1.0 - s * (1.0 - f))

    if i == 0:
        r, g, bl = b, t, p
    elif i == 1:
        r, g, bl = q, b, p
    elif i == 2:
        r, g, bl = p, b, t
    elif i == 3:
        r, g, bl = p, q, b
    elif i == 4:
        r, g, bl = t, p, b
    else:
        r, g, bl = b, p, q

    return (int(r * 255), int(g * 255), int(bl * 255))


# ---------------------------------------------------------------------------
# Zone rendering
# ---------------------------------------------------------------------------

def render_strip_border(
    surface: pygame.Surface,
    edge_hues: list[float],
    edge_bris: list[float],
    dominant_sat: float,
    mode: str,
    dominant_hue: float = 0.0,
    brightness: float = 0.5,
) -> None:
    """Render the simulated light strip as a glowing border.

    Args:
        surface:      Pygame surface (room window).
        edge_hues:    Per-zone hue values [0, 1].
        edge_bris:    Per-zone brightness values [0, 1].
        dominant_sat: Overall saturation [0, 1].
        mode:         "strip" (spatial) or "bulb" (uniform).
        dominant_hue: Dominant screen hue (for bulb mode).
        brightness:   Overall brightness (for bulb mode).
    """
    n: int = len(edge_hues)

    # Perimeter of the border in pixels.
    # Top + right + bottom + left.
    top_px: int = ROOM_WIDTH
    right_px: int = ROOM_HEIGHT
    bottom_px: int = ROOM_WIDTH
    left_px: int = ROOM_HEIGHT
    total_px: int = top_px + right_px + bottom_px + left_px

    for i in range(n):
        if mode == "bulb":
            color: tuple[int, int, int] = hsb_to_rgb(
                dominant_hue, dominant_sat, brightness,
            )
        else:
            color = hsb_to_rgb(
                edge_hues[i], dominant_sat, edge_bris[i],
            )

        # Map zone i to a position on the perimeter.
        frac: float = i / n
        pos: int = int(frac * total_px)

        if pos < top_px:
            # Top edge.
            x: int = pos
            rect: pygame.Rect = pygame.Rect(
                x - BORDER_PX // 2, 0,
                max(1, total_px // n), BORDER_PX,
            )
        elif pos < top_px + right_px:
            # Right edge.
            y: int = pos - top_px
            rect = pygame.Rect(
                ROOM_WIDTH - BORDER_PX, y - BORDER_PX // 2,
                BORDER_PX, max(1, total_px // n),
            )
        elif pos < top_px + right_px + bottom_px:
            # Bottom edge.
            x = ROOM_WIDTH - (pos - top_px - right_px)
            rect = pygame.Rect(
                x - BORDER_PX // 2, ROOM_HEIGHT - BORDER_PX,
                max(1, total_px // n), BORDER_PX,
            )
        else:
            # Left edge.
            y = ROOM_HEIGHT - (pos - top_px - right_px - bottom_px)
            rect = pygame.Rect(
                0, y - BORDER_PX // 2,
                BORDER_PX, max(1, total_px // n),
            )

        # Clamp to surface bounds.
        rect = rect.clip(surface.get_rect())
        if rect.width > 0 and rect.height > 0:
            pygame.draw.rect(surface, color, rect)


# ---------------------------------------------------------------------------
# Movie decoder thread
# ---------------------------------------------------------------------------

class MovieDecoder:
    """Decode a movie file via ffmpeg into raw RGB frames.

    Runs ffmpeg in a subprocess, reads raw RGB24 frames, and stores
    the latest frame for the main loop to consume.

    Args:
        path: Path to the movie file.
        width: Output width.
        height: Output height.
        fps: Target frame rate.
    """

    def __init__(
        self, path: str, width: int = TV_WIDTH,
        height: int = TV_HEIGHT, fps: int = MOVIE_FPS,
    ) -> None:
        self.path: str = path
        self.width: int = width
        self.height: int = height
        self.fps: int = fps
        self._frame: Optional[bytes] = None
        self._lock: threading.Lock = threading.Lock()
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None

    @property
    def frame(self) -> Optional[bytes]:
        """The latest decoded frame (RGB24 bytes)."""
        with self._lock:
            return self._frame

    def start(self) -> None:
        """Start decoding."""
        self._running = True
        self._thread = threading.Thread(
            target=self._decode_loop, daemon=True,
            name="movie-decoder",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop decoding."""
        self._running = False
        if self._process:
            try:
                self._process.kill()
                self._process.wait(timeout=3.0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)

    def _decode_loop(self) -> None:
        """Read frames from ffmpeg."""
        cmd: list[str] = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", self.path,
            "-vf", f"scale={self.width}:{self.height}",
            "-r", str(self.fps),
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
        except FileNotFoundError:
            print("ERROR: ffmpeg not found", file=sys.stderr)
            return

        frame_size: int = self.width * self.height * 3
        frame_interval: float = 1.0 / self.fps

        while self._running:
            t_start: float = time.monotonic()
            data: bytes = self._process.stdout.read(frame_size)
            if not data or len(data) < frame_size:
                break
            with self._lock:
                self._frame = data
            # Pace to real time.
            elapsed: float = time.monotonic() - t_start
            sleep_time: float = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        self._running = False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the screen-reactive test harness."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Screen-reactive LIFX test harness",
    )
    parser.add_argument(
        "--movie", required=True,
        help="Path to movie file (any ffmpeg-compatible format)",
    )
    parser.add_argument(
        "--mode", choices=["strip", "bulb"], default="strip",
        help="Simulation mode: strip (spatial edges) or bulb (uniform)",
    )
    parser.add_argument(
        "--zones", type=int, default=STRIP_ZONES,
        help=f"Number of edge zones for strip mode (default {STRIP_ZONES})",
    )
    parser.add_argument(
        "--sensitivity", type=float, default=1.5,
        help="Brightness sensitivity (default 1.5)",
    )
    parser.add_argument(
        "--contrast", type=float, default=1.5,
        help="Dynamic range gamma (default 1.5)",
    )
    args: argparse.Namespace = parser.parse_args()

    if not os.path.isfile(args.movie):
        print(f"ERROR: Movie file not found: {args.movie}",
              file=sys.stderr)
        sys.exit(1)

    # --- Initialize pygame ---
    pygame.init()
    screen: pygame.Surface = pygame.display.set_mode(
        (ROOM_WIDTH, ROOM_HEIGHT),
    )
    pygame.display.set_caption(
        f"GlowUp Screen Test — {args.mode} mode"
    )

    # --- Start movie decoder ---
    decoder: MovieDecoder = MovieDecoder(args.movie)
    decoder.start()

    # --- Set up vision pipeline ---
    bus: SignalBus = SignalBus()
    extractor: VisionExtractor = VisionExtractor(
        source_name="screen",
        bus=bus,
        edge_regions=args.zones,
    )

    # --- Main loop ---
    clock: pygame.time.Clock = pygame.time.Clock()
    running: bool = True

    print(f"Test harness running — {args.mode} mode, "
          f"{args.zones} zones")
    print(f"Sensitivity={args.sensitivity}, Contrast={args.contrast}")
    print("Close the window to stop.\n")

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

        # Get the latest movie frame.
        frame_bytes: Optional[bytes] = decoder.frame
        if frame_bytes is None:
            clock.tick(30)
            continue

        # Build pyramid and run vision extraction.
        pyramid: list[Any] = build_pyramid(
            frame_bytes, TV_WIDTH, TV_HEIGHT,
        )
        extractor.process_pyramid(pyramid, TV_WIDTH, TV_HEIGHT)

        # Read signals from bus.
        src: str = "screen"
        brightness: float = float(bus.read(f"{src}:vision:brightness", 0.0))
        energy: float = float(bus.read(f"{src}:vision:energy", 0.0))
        flash: float = float(bus.read(f"{src}:vision:flash", 0.0))
        dominant_hue: float = float(
            bus.read(f"{src}:vision:dominant_hue", 0.0)
        )
        dominant_sat: float = float(
            bus.read(f"{src}:vision:dominant_sat", 0.5)
        )
        edge_colors: Any = bus.read(
            f"{src}:vision:edge_colors", [0.0] * args.zones,
        )
        edge_brightness: Any = bus.read(
            f"{src}:vision:edge_brightness", [0.0] * args.zones,
        )

        # Apply sensitivity and contrast to edge brightness.
        if isinstance(edge_brightness, list):
            processed_bri: list[float] = []
            for b in edge_brightness:
                b = min(1.0, b * args.sensitivity)
                if args.contrast != 1.0 and b > 0.0:
                    b = b ** args.contrast
                b = min(1.0, b + flash * 0.4)
                processed_bri.append(b)
        else:
            processed_bri = [0.5] * args.zones

        # --- Render ---
        screen.fill(ROOM_BG)

        # Render the strip border.
        if isinstance(edge_colors, list):
            render_strip_border(
                screen, edge_colors, processed_bri,
                dominant_sat, args.mode,
                dominant_hue=dominant_hue,
                brightness=min(1.0, brightness * args.sensitivity),
            )

        # Render the movie frame in the center (the "TV").
        frame_array: np.ndarray = np.frombuffer(
            frame_bytes, dtype=np.uint8,
        ).reshape(TV_HEIGHT, TV_WIDTH, 3)
        # pygame expects (width, height, 3) surface from numpy.
        tv_surface: pygame.Surface = pygame.surfarray.make_surface(
            frame_array.swapaxes(0, 1),
        )
        screen.blit(tv_surface, (BORDER_PX, BORDER_PX))

        # Status text.
        font: pygame.font.Font = pygame.font.SysFont("monospace", 12)
        status: str = (
            f"bri={brightness:.2f}  energy={energy:.2f}  "
            f"flash={flash:.2f}  hue={dominant_hue*360:.0f}°  "
            f"sat={dominant_sat:.2f}"
        )
        text: pygame.Surface = font.render(status, True, (200, 200, 200))
        screen.blit(text, (5, ROOM_HEIGHT - 16))

        pygame.display.flip()
        clock.tick(MOVIE_FPS)

    # --- Cleanup ---
    decoder.stop()
    pygame.quit()
    print("Done.")


if __name__ == "__main__":
    main()
