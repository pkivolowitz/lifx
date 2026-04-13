"""GlowUp Kiosk — native pygame dashboard application.

Fullscreen dashboard for Pi clocks. Polls the GlowUp API and
external weather services, renders tiles in a grid layout.
Supports day and night color themes.

Usage::

    python -m kiosk --api http://localhost:8420
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

# ---------------------------------------------------------------------------
# Layout modes
# ---------------------------------------------------------------------------

MODE_DESKTOP: str = "desktop"
MODE_WALLCLOCK: str = "wallclock"

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

# Wallclock mode: fewer tiles, viewed from across the room.
WALLCLOCK_REGISTRY: list[tuple[str, Callable]] = [
    ("weather", tiles.draw_weather),
    ("alerts", tiles.draw_alerts),
    ("locks", tiles.draw_locks),
    ("security", tiles.draw_security),
]

# Night-stack registry — wallclock at night dumps the 2x2 grid and
# uses a vertical stack of full-width single-line rows.  Order is the
# render order top-to-bottom.  Security is split into alarm + doors
# so each phrase gets its own row.
NIGHT_STACK_REGISTRY: list[tuple[str, Callable]] = [
    ("temp", tiles.night_row_temp),
    ("locks", tiles.night_row_locks),
    ("doors", tiles.night_row_doors),
    ("alarm", tiles.night_row_alarm),
    ("alerts", tiles.night_row_alerts),
]

# Night layout: clock takes a smaller fraction so the 5 rows below
# get more room each.  0.22 leaves ~78% of screen height for rows.
NIGHT_CLOCK_FRAC: float = 0.22

MODE_CONFIG: dict[str, dict] = {
    MODE_DESKTOP: {
        "registry": TILE_REGISTRY,
        "cols": 3,
        "clock_frac": 0.20,
        "tile_fill": 0.50,
    },
    MODE_WALLCLOCK: {
        "registry": WALLCLOCK_REGISTRY,
        "cols": 2,
        "clock_frac": 0.40,
        "tile_fill": 0.95,
    },
}


# ---------------------------------------------------------------------------
# Grid layout
# ---------------------------------------------------------------------------

def _compute_night_stack(
    screen_w: int, screen_h: int, top_offset: int, n_rows: int,
) -> list[pygame.Rect]:
    """Compute full-width row rects for the night stacked layout.

    Args:
        screen_w:   Screen width in pixels.
        screen_h:   Screen height in pixels.
        top_offset: Pixels reserved at the top (clock + margin).
        n_rows:     Number of rows to lay out.

    Returns:
        List of Rects, one per row, vertically stacked below the clock,
        each spanning the full screen width minus a small horizontal pad.
    """
    avail_h: int = screen_h - top_offset
    row_h: int = max(
        1, (avail_h - TILE_PAD * (n_rows + 1)) // n_rows,
    )
    rects: list[pygame.Rect] = []
    for i in range(n_rows):
        y: int = top_offset + TILE_PAD + i * (row_h + TILE_PAD)
        rects.append(
            pygame.Rect(
                TILE_PAD, y,
                screen_w - 2 * TILE_PAD, row_h,
            )
        )
    return rects


def _compute_grid(
    screen_w: int, screen_h: int, clock_h: int,
    n_tiles: int, cols: int = 3, fill_frac: float = 0.50,
) -> list[pygame.Rect]:
    """Compute tile rectangles in a grid below the clock.

    Args:
        screen_w:  Screen width in pixels.
        screen_h:  Screen height in pixels.
        clock_h:   Height reserved for the clock at top.
        n_tiles:   Number of tiles to lay out.
        cols:      Number of grid columns.
        fill_frac: Fraction of available row height each tile occupies.

    Returns:
        List of Rects, one per tile.
    """
    avail_h: int = screen_h - clock_h
    rows: int = max(1, (n_tiles + cols - 1) // cols)
    tile_w: int = (screen_w - TILE_PAD * (cols + 1)) // cols
    tile_h: int = int((avail_h - TILE_PAD * (rows + 1)) / rows * fill_frac)

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
        "--api", type=str, default="http://localhost:8420",
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
        "--cols", type=int, default=0,
        help="Grid columns (0 = use mode default)",
    )
    parser.add_argument(
        "--rotate", type=int, default=0, choices=[0, 90, 180, 270],
        help="Rotate output clockwise (matches wlr-randr --transform)",
    )
    parser.add_argument(
        "--mode", type=str, default=MODE_DESKTOP,
        choices=[MODE_DESKTOP, MODE_WALLCLOCK],
        help="Layout: desktop (3 cols, all tiles) or wallclock (2 cols, 4 tiles, big)",
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

    phys_w, phys_h = screen.get_size()
    if args.rotate in (90, 270):
        screen_w, screen_h = phys_h, phys_w
    else:
        screen_w, screen_h = phys_w, phys_h

    if args.rotate == 0:
        canvas = screen
    else:
        canvas = pygame.Surface((screen_w, screen_h))

    logger.info(
        "Screen: %dx%d physical, %dx%d logical, rotate=%d",
        phys_w, phys_h, screen_w, screen_h, args.rotate,
    )

    # Resolve layout mode.
    mode_cfg = MODE_CONFIG[args.mode]
    registry: list[tuple[str, Callable]] = mode_cfg["registry"]
    cols: int = args.cols if args.cols > 0 else mode_cfg["cols"]
    clock_frac: float = mode_cfg["clock_frac"]
    logger.info(
        "Mode: %s — %d tiles, %d cols, clock_frac=%.2f",
        args.mode, len(registry), cols, clock_frac,
    )

    # Top margin — push everything down.
    TOP_MARGIN: int = 20

    # Clock area at the top. Measured from the actual font metrics so
    # there is no dead space between date and first tile row — the old
    # code reserved screen_h * clock_frac which was ~2x the real need.
    target_h: int = int(screen_h * clock_frac)
    clock_h: int = tiles.measure_clock_height(screen_w, target_h)
    clock_rect = pygame.Rect(0, TOP_MARGIN, screen_w, clock_h)
    logger.info("Clock: target_h=%d measured=%d", target_h, clock_h)

    # Tile grid — offset by top margin.
    tile_rects: list[pygame.Rect] = _compute_grid(
        screen_w, screen_h, clock_h + TOP_MARGIN,
        len(registry), cols, mode_cfg["tile_fill"],
    )

    # Night stack layout — pre-computed once.  Wallclock at night
    # switches to this layout dynamically when the theme flips to NIGHT.
    # Other modes (desktop) keep the grid even at night.
    night_target_h: int = int(screen_h * NIGHT_CLOCK_FRAC)
    night_clock_h: int = tiles.measure_clock_height(
        screen_w, night_target_h,
    )
    night_clock_rect = pygame.Rect(0, TOP_MARGIN, screen_w, night_clock_h)
    night_rects: list[pygame.Rect] = _compute_night_stack(
        screen_w, screen_h, night_clock_h + TOP_MARGIN,
        len(NIGHT_STACK_REGISTRY),
    )
    night_layout_active: bool = (args.mode == MODE_WALLCLOCK)
    if night_layout_active:
        logger.info(
            "Night stack: clock_h=%d, %d rows, row_h~%d",
            night_clock_h, len(NIGHT_STACK_REGISTRY),
            night_rects[0].h if night_rects else 0,
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
        # Night mode is driven by the server's CombineOperator via
        # hints.night_mode (time:is_night AND NOT main_bedroom lights).
        # Fall back to the local hour check only when the server is
        # unreachable (hints absent) or the bus signal hasn't populated
        # yet — so the kiosk still behaves sanely in a cold start.
        if args.night:
            theme = NIGHT
        elif args.day:
            theme = DAY
        else:
            hints = poller.get("hints") or {}
            if "night_mode" in hints:
                theme = NIGHT if hints["night_mode"] else DAY
            else:
                hour: int = datetime.now().hour
                if hour >= NIGHT_START or hour < NIGHT_END:
                    theme = NIGHT
                else:
                    theme = DAY

        # Pick layout: night stack (when wallclock + NIGHT theme) or
        # the configured grid otherwise.  Night stack uses the
        # NIGHT_STACK_REGISTRY with split alarm/doors rows.
        use_night_stack: bool = night_layout_active and theme is NIGHT
        if use_night_stack:
            active_clock_rect = night_clock_rect
            active_registry = NIGHT_STACK_REGISTRY
            active_rects = night_rects
        else:
            active_clock_rect = clock_rect
            active_registry = registry
            active_rects = tile_rects

        # Clear logical canvas.
        canvas.fill(theme.bg)

        # Draw clock.
        tiles.draw_clock(canvas, active_clock_rect, poller, theme)

        # Draw tiles.
        for i, (name, draw_fn) in enumerate(active_registry):
            if i < len(active_rects):
                try:
                    draw_fn(canvas, active_rects[i], poller, theme)
                except Exception as exc:
                    logger.debug("Tile '%s' render error: %s", name, exc)

        # Rotate canvas onto physical screen if needed.
        if args.rotate != 0:
            screen.blit(pygame.transform.rotate(canvas, -args.rotate), (0, 0))

        # Flip.
        pygame.display.flip()
        clock.tick(FPS)

    # Cleanup.
    poller.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
