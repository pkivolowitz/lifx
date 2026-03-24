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
from colorspace import srgb_to_oklab, oklab_to_srgb

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default TV window size (the "screen" being watched).
DEFAULT_TV_WIDTH: int = 640
DEFAULT_TV_HEIGHT: int = 360

# Border thickness around the TV (simulated strip width).
BORDER_PX: int = 60

# These are set by main() based on --aspect.
TV_WIDTH: int = DEFAULT_TV_WIDTH
TV_HEIGHT: int = DEFAULT_TV_HEIGHT
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

try:
    from scipy.ndimage import gaussian_filter
    _HAS_SCIPY: bool = True
except ImportError:
    _HAS_SCIPY = False


def _blur_surface(surface: pygame.Surface, radius: int = 15) -> pygame.Surface:
    """Gaussian blur using scipy.

    Args:
        surface: Source pygame surface.
        radius:  Blur sigma in pixels.

    Returns:
        Blurred copy of the surface.
    """
    # surfarray gives (W, H, 3) — sigma axes match (x, y, channel).
    arr: np.ndarray = pygame.surfarray.array3d(surface)
    blurred: np.ndarray = gaussian_filter(
        arr.astype(np.float32), sigma=(radius, radius, 0),
    ).clip(0, 255).astype(np.uint8)
    return pygame.surfarray.make_surface(blurred)


# Width of the color strip painted just inside the TV edge (pixels).
GLOW_STRIP_WIDTH: int = 16

# Gaussian blur sigma. 15 = soft glow without killing frame rate.
BLUR_RADIUS: int = 8


def render_glow_border(
    surface: pygame.Surface,
    edge_hues: list[float],
    edge_bris: list[float],
    dominant_sat: float,
    mode: str,
    dominant_hue: float = 0.0,
    brightness: float = 0.5,
) -> None:
    """Render ambient glow around the TV.

    Paints bright color zones just inside the TV border on a
    temporary surface, blurs it heavily, then composites it onto
    the room surface.  The result looks like light bleeding outward
    from the screen edges — what a real strip behind a TV produces.

    Args:
        surface:      Room pygame surface.
        edge_hues:    Per-zone hue values [0, 1].
        edge_bris:    Per-zone brightness values [0, 1].
        dominant_sat: Overall saturation [0, 1].
        mode:         "strip" (spatial) or "bulb" (uniform).
        dominant_hue: Dominant screen hue (for bulb mode).
        brightness:   Overall brightness (for bulb mode).
    """
    n: int = len(edge_hues)
    w: int = surface.get_width()
    h: int = surface.get_height()

    # Create a black surface for the glow source.
    glow: pygame.Surface = pygame.Surface((w, h))
    glow.fill((0, 0, 0))

    # TV rectangle position.
    tv_x: int = BORDER_PX
    tv_y: int = BORDER_PX
    tv_w: int = TV_WIDTH
    tv_h: int = TV_HEIGHT

    # Perimeter in pixels: top + right + bottom + left of the TV.
    peri_top: int = tv_w
    peri_right: int = tv_h
    peri_bottom: int = tv_w
    peri_left: int = tv_h
    peri_total: int = peri_top + peri_right + peri_bottom + peri_left

    if mode == "bulb":
        # Single bulb: one uniform color wash behind the entire TV.
        color: tuple[int, int, int] = hsb_to_rgb(
            dominant_hue, min(1.0, dominant_sat * 0.7),
            brightness,
        )
        glow.fill(color)
    else:
        for i in range(n):
            color = hsb_to_rgb(
                edge_hues[i],
                min(1.0, dominant_sat * 0.7),
                edge_bris[i],
            )

            # Map zone i and i+1 to exact perimeter positions.
            frac_lo: float = i / n
            frac_hi: float = (i + 1) / n
            pos_lo: float = frac_lo * peri_total
            pos_hi: float = frac_hi * peri_total

            # Walk through the four edges. Each zone may span one edge.
            # For simplicity, use the midpoint to pick the edge.
            pos_mid: float = (pos_lo + pos_hi) / 2.0
            seg_len: float = pos_hi - pos_lo

            if pos_mid < peri_top:
                # Top edge (left to right).
                x: float = tv_x + pos_lo
                rect: pygame.Rect = pygame.Rect(
                    int(x), tv_y - GLOW_STRIP_WIDTH,
                    max(1, int(seg_len)), GLOW_STRIP_WIDTH * 2,
                )
            elif pos_mid < peri_top + peri_right:
                # Right edge (top to bottom).
                local: float = pos_lo - peri_top
                y: float = tv_y + local
                rect = pygame.Rect(
                    tv_x + tv_w - GLOW_STRIP_WIDTH, int(y),
                    GLOW_STRIP_WIDTH * 2, max(1, int(seg_len)),
                )
            elif pos_mid < peri_top + peri_right + peri_bottom:
                # Bottom edge (right to left).
                local = pos_lo - peri_top - peri_right
                x = tv_x + tv_w - local - seg_len
                rect = pygame.Rect(
                    int(x), tv_y + tv_h - GLOW_STRIP_WIDTH,
                    max(1, int(seg_len)), GLOW_STRIP_WIDTH * 2,
                )
            else:
                # Left edge (bottom to top).
                local = pos_lo - peri_top - peri_right - peri_bottom
                y = tv_y + tv_h - local - seg_len
                rect = pygame.Rect(
                    tv_x - GLOW_STRIP_WIDTH, int(y),
                    GLOW_STRIP_WIDTH * 2, max(1, int(seg_len)),
                )

            rect = rect.clip(glow.get_rect())
            if rect.width > 0 and rect.height > 0:
                pygame.draw.rect(glow, color, rect)

        # Fill corners with oklab midpoint of the two adjacent zones.
        # Painted before the blur so the Gaussian spreads them naturally.
        d_sat: float = min(1.0, dominant_sat * 0.7)
        corner_peri: list[float] = [
            0.0,                                          # top-left
            float(peri_top),                              # top-right
            float(peri_top + peri_right),                 # bottom-right
            float(peri_top + peri_right + peri_bottom),   # bottom-left
        ]
        corner_xy: list[tuple[int, int]] = [
            (tv_x - GLOW_STRIP_WIDTH, tv_y - GLOW_STRIP_WIDTH),
            (tv_x + tv_w - GLOW_STRIP_WIDTH, tv_y - GLOW_STRIP_WIDTH),
            (tv_x + tv_w - GLOW_STRIP_WIDTH, tv_y + tv_h - GLOW_STRIP_WIDTH),
            (tv_x - GLOW_STRIP_WIDTH, tv_y + tv_h - GLOW_STRIP_WIDTH),
        ]
        corner_size: int = GLOW_STRIP_WIDTH * 2
        for ci in range(4):
            frac_c: float = corner_peri[ci] / peri_total
            iz_after: int = int(frac_c * n) % n
            iz_before: int = (iz_after - 1) % n
            rgb_a: tuple[int, int, int] = hsb_to_rgb(
                edge_hues[iz_before], d_sat, edge_bris[iz_before],
            )
            rgb_b: tuple[int, int, int] = hsb_to_rgb(
                edge_hues[iz_after], d_sat, edge_bris[iz_after],
            )
            L1, a1, b1 = srgb_to_oklab(rgb_a[0] / 255.0, rgb_a[1] / 255.0, rgb_a[2] / 255.0)
            L2, a2, b2 = srgb_to_oklab(rgb_b[0] / 255.0, rgb_b[1] / 255.0, rgb_b[2] / 255.0)
            rm, gm, bm = oklab_to_srgb(
                (L1 + L2) * 0.5, (a1 + a2) * 0.5, (b1 + b2) * 0.5,
            )
            c_color: tuple[int, int, int] = (
                max(0, min(255, int(rm * 255))),
                max(0, min(255, int(gm * 255))),
                max(0, min(255, int(bm * 255))),
            )
            cx, cy = corner_xy[ci]
            crect: pygame.Rect = pygame.Rect(cx, cy, corner_size, corner_size)
            crect = crect.clip(glow.get_rect())
            if crect.width > 0 and crect.height > 0:
                pygame.draw.rect(glow, c_color, crect)

    # Blur the glow source heavily.
    blurred: pygame.Surface = _blur_surface(glow, BLUR_RADIUS)

    # Composite: add the blurred glow onto the room surface.
    # Use BLEND_ADD so the glow brightens the dark room.
    surface.blit(blurred, (0, 0), special_flags=pygame.BLEND_ADD)


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
        start_time: Optional[str] = None,
    ) -> None:
        self.path: str = path
        self.width: int = width
        self.height: int = height
        self.fps: int = fps
        self.start_time: Optional[str] = start_time
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
        ]
        if self.start_time:
            cmd.extend(["-ss", self.start_time])
        cmd.extend([
            "-i", self.path,
            "-vf", f"scale={self.width}:{self.height}",
            "-r", str(self.fps),
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ])
        print(f"  Decoder cmd: {' '.join(cmd)}", flush=True)
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print("ERROR: ffmpeg not found", file=sys.stderr)
            return

        frame_size: int = self.width * self.height * 3
        frame_interval: float = 1.0 / self.fps
        print(f"  Decoder: reading {frame_size} bytes/frame from ffmpeg...",
              flush=True)

        frame_count: int = 0
        while self._running:
            t_start: float = time.monotonic()
            # Read exactly frame_size bytes, accumulating partial reads.
            buf: bytearray = bytearray()
            while len(buf) < frame_size:
                chunk: bytes = self._process.stdout.read(
                    frame_size - len(buf)
                )
                if not chunk:
                    break
                buf.extend(chunk)
            data: bytes = bytes(buf)
            if len(data) < frame_size:
                print(f"  Decoder: EOF after {frame_count} frames "
                      f"(got {len(data)} of {frame_size} bytes)",
                      flush=True)
                break
            frame_count += 1
            if frame_count == 1:
                print(f"  Decoder: first frame received", flush=True)
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
        "--start", default=None, metavar="TIME",
        help="Seek to this position before playing (e.g. '30:00' or '1:00:00')",
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
    parser.add_argument(
        "--aspect", default=None, metavar="W:H",
        help=(
            "TV aspect ratio (e.g. '2.35:1' for cinemascope, "
            "'16:9' default).  Adjusts TV height to match."
        ),
    )
    args: argparse.Namespace = parser.parse_args()

    if not os.path.isfile(args.movie):
        print(f"ERROR: Movie file not found: {args.movie}",
              file=sys.stderr)
        sys.exit(1)

    # --- Apply aspect ratio ---
    global TV_WIDTH, TV_HEIGHT, ROOM_WIDTH, ROOM_HEIGHT
    if args.aspect:
        parts: list[str] = args.aspect.split(":")
        ratio: float = float(parts[0]) / float(parts[1]) if len(parts) == 2 else float(parts[0])
        TV_HEIGHT = int(TV_WIDTH / ratio)
        # Ensure even height for ffmpeg.
        TV_HEIGHT = TV_HEIGHT & ~1
    ROOM_WIDTH = TV_WIDTH + BORDER_PX * 2
    ROOM_HEIGHT = TV_HEIGHT + BORDER_PX * 2
    print(f"  TV: {TV_WIDTH}x{TV_HEIGHT} "
          f"(aspect {TV_WIDTH/TV_HEIGHT:.2f}:1)")

    # --- Initialize pygame ---
    pygame.init()
    screen: pygame.Surface = pygame.display.set_mode(
        (ROOM_WIDTH, ROOM_HEIGHT),
    )
    pygame.display.set_caption(
        f"GlowUp Screen Test — {args.mode} mode"
    )

    # --- Start movie decoder ---
    decoder: MovieDecoder = MovieDecoder(
        args.movie, width=TV_WIDTH, height=TV_HEIGHT,
        start_time=args.start,
    )
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
            screen.fill(ROOM_BG)
            pygame.display.flip()
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
        # Dark room background.
        screen.fill(ROOM_BG)

        # Render the ambient glow (blurred color zones behind the TV).
        if isinstance(edge_colors, list):
            render_glow_border(
                screen, edge_colors, processed_bri,
                dominant_sat, args.mode,
                dominant_hue=dominant_hue,
                brightness=min(1.0, brightness * args.sensitivity),
            )

        # Composite the movie frame on top (the "TV").
        frame_array: np.ndarray = np.frombuffer(
            frame_bytes, dtype=np.uint8,
        ).reshape(TV_HEIGHT, TV_WIDTH, 3)
        tv_surface: pygame.Surface = pygame.surfarray.make_surface(
            frame_array.swapaxes(0, 1),
        )
        screen.blit(tv_surface, (BORDER_PX, BORDER_PX))

        pygame.display.flip()
        clock.tick(MOVIE_FPS)

    # --- Cleanup ---
    decoder.stop()
    pygame.quit()
    print("Done.")


if __name__ == "__main__":
    main()
