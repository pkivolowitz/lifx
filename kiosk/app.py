"""GlowUp Kiosk — native pygame dashboard application.

Fullscreen dashboard for Pi clocks. Polls the GlowUp API and
external weather services, renders tiles in a grid layout.
Supports day and night color themes.

Usage::

    python -m kiosk --api http://10.0.0.214:8420
    python -m kiosk --night          # force night mode
    python -m kiosk --windowed       # debug in a window

Requires: pygame, requests.
"""

__version__: str = "1.0"

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Callable

import pygame

from kiosk.data import DataPoller
from kiosk.theme import DAY, NIGHT, Theme
from kiosk import tiles

logger: logging.Logger = logging.getLogger("glowup.kiosk")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Target frame rate — 10 FPS is plenty for a dashboard.
FPS: int = 10

# Night mode hours (inclusive).  22:00–06:00.
NIGHT_START: int = 22
NIGHT_END: int = 6

# Tile padding in pixels.
TILE_PAD: int = 8

# Clock tile height as fraction of screen height.
CLOCK_HEIGHT_FRAC: float = 0.20

# ---------------------------------------------------------------------------
# Tile registry — order matters for grid layout
# ---------------------------------------------------------------------------

# Each entry: (name, draw_function).
# The grid fills left-to-right, top-to-bottom.
TILE_REGISTRY: list[tuple[str, Callable]] = [
    ("locks", tiles.draw_locks),
    ("security", tiles.draw_security),
    ("health", tiles.draw_health),
    ("weather", tiles.draw_weather),
    ("aqi", tiles.draw_aqi),
    ("soil", tiles.draw_soil),
    ("alerts", tiles.draw_alerts),
    ("moon", tiles.draw_moon),
]


# ---------------------------------------------------------------------------
# Grid layout
# ---------------------------------------------------------------------------

def _compute_grid(
    screen_w: int, screen_h: int, clock_h: int,
    n_tiles: int, cols: int = 3,
) -> list[pygame.Rect]:
    """Compute tile rectangles in a grid below the clock.

    Args:
        screen_w: Screen width in pixels.
        screen_h: Screen height in pixels.
        clock_h:  Height reserved for the clock at top.
        n_tiles:  Number of tiles to lay out.
        cols:     Number of grid columns.

    Returns:
        List of Rects, one per tile.
    """
    avail_h: int = screen_h - clock_h
    rows: int = max(1, (n_tiles + cols - 1) // cols)
    tile_w: int = (screen_w - TILE_PAD * (cols + 1)) // cols
    # 50% of natural height — snug fit around content.
    tile_h: int = int((avail_h - TILE_PAD * (rows + 1)) / rows * 0.50)

    rects: list[pygame.Rect] = []
    for i in range(n_tiles):
        col: int = i % cols
        row: int = i // cols
        x: int = TILE_PAD + col * (tile_w + TILE_PAD)
        y: int = clock_h + TILE_PAD + row * (tile_h + TILE_PAD)
        rects.append(pygame.Rect(x, y, tile_w, tile_h))

    return rects


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse arguments and run the kiosk."""
    parser = argparse.ArgumentParser(
        description="GlowUp Kiosk — native Pi dashboard",
    )
    parser.add_argument(
        "--api", type=str, default="http://10.0.0.214:8420",
        help="GlowUp server URL",
    )
    parser.add_argument(
        "--night", action="store_true",
        help="Force night mode",
    )
    parser.add_argument(
        "--day", action="store_true",
        help="Force day mode",
    )
    parser.add_argument(
        "--windowed", action="store_true",
        help="Run in a window (for debugging)",
    )
    parser.add_argument(
        "--width", type=int, default=0,
        help="Window width (windowed mode only)",
    )
    parser.add_argument(
        "--height", type=int, default=0,
        help="Window height (windowed mode only)",
    )
    parser.add_argument(
        "--cols", type=int, default=3,
        help="Grid columns (default 3)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Hide the mouse cursor.
    os.environ["SDL_VIDEO_CURSOR"] = "0"
    os.environ["WLR_NO_HARDWARE_CURSORS"] = "1"

    # Initialize pygame.
    pygame.init()
    pygame.mouse.set_visible(False)

    # Create an invisible cursor (1x1 transparent pixel).
    try:
        invisible = pygame.cursors.Cursor(
            (1, 1), (0, 0),
            pygame.Surface((1, 1), pygame.SRCALPHA),
        )
        pygame.mouse.set_cursor(invisible)
    except Exception:
        pass

    if args.windowed:
        w: int = args.width or 480
        h: int = args.height or 800
        screen = pygame.display.set_mode((w, h))
        pygame.display.set_caption("GlowUp Kiosk")
    else:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)

    screen_w, screen_h = screen.get_size()
    logger.info("Screen: %dx%d", screen_w, screen_h)

    # Top margin — push everything down.
    TOP_MARGIN: int = 20

    # Clock area at the top.
    clock_h: int = int(screen_h * CLOCK_HEIGHT_FRAC)
    clock_rect = pygame.Rect(0, TOP_MARGIN, screen_w, clock_h)

    # Tile grid — offset by top margin.
    tile_rects: list[pygame.Rect] = _compute_grid(
        screen_w, screen_h, clock_h + TOP_MARGIN,
        len(TILE_REGISTRY), args.cols,
    )

    # Start data poller.
    poller = DataPoller(api_base=args.api)
    poller.start()

    # Main loop.
    clock = pygame.time.Clock()
    running: bool = True

    while running:
        # Event handling.
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False

        # Determine theme.
        if args.night:
            theme = NIGHT
        elif args.day:
            theme = DAY
        else:
            hour: int = datetime.now().hour
            if hour >= NIGHT_START or hour < NIGHT_END:
                theme = NIGHT
            else:
                theme = DAY

        # Clear screen.
        screen.fill(theme.bg)

        # Draw clock.
        tiles.draw_clock(screen, clock_rect, poller, theme)

        # Draw tiles.
        for i, (name, draw_fn) in enumerate(TILE_REGISTRY):
            if i < len(tile_rects):
                try:
                    draw_fn(screen, tile_rects[i], poller, theme)
                except Exception as exc:
                    logger.debug("Tile '%s' render error: %s", name, exc)

        # Flip.
        pygame.display.flip()
        clock.tick(FPS)

    # Cleanup.
    poller.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
