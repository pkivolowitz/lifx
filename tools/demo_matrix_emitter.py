#!/usr/bin/env python3
"""Terminal matrix preview — render 2D effects as a pixel grid.

Renders effects onto a full-terminal 2D pixel grid inside a Unicode
box-drawing border.  Resolution is determined by terminal size — make
the window bigger or reduce font size for more pixels.

Any effect works: 1D effects produce a raster-scan pattern across rows,
while 2D effects (like ``plasma2d``) fill the grid natively.  2D effects
receive ``width`` and ``height`` parameters matching the pixel grid.

With ``--audio``, captures from the system microphone via ffmpeg and
feeds FFT-analyzed frequency bands into the signal bus.  Audio-reactive
effects (like ``spectrum2d``) read these signals to drive the display.

Uses 24-bit ANSI truecolor.  No curses dependency.

Usage:
    python3 demo_matrix_emitter.py plasma2d
    python3 demo_matrix_emitter.py spectrum2d --audio
    python3 demo_matrix_emitter.py spectrum2d --audio --fps 30
    python3 demo_matrix_emitter.py --list

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.1"

import argparse
import signal
import sys
import threading

from effects import get_effect_names
from emitters.screen_matrix import ScreenMatrixEmitter
from engine import Controller

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default parameters.
DEFAULT_EFFECT: str = "plasma2d"
DEFAULT_AUDIO_EFFECT: str = "spectrum2d"
DEFAULT_FPS: int = 20

# Audio source name used in signal bus naming.
MIC_SOURCE_NAME: str = "mic"

# Number of frequency bands for the audio extractor.
# More bands = smoother spectrum display on wide terminals.
MIC_BAND_COUNT: int = 32


# ---------------------------------------------------------------------------
# Audio setup
# ---------------------------------------------------------------------------

def _start_mic(band_count: int = MIC_BAND_COUNT):
    """Set up microphone capture via the media pipeline.

    Creates a SignalBus, a MicSource, and an AudioExtractor.
    The source captures from the system's default microphone via ffmpeg.

    Args:
        band_count: Number of FFT frequency bands.

    Returns:
        Tuple of ``(signal_bus, mic_source)`` or ``(None, None)`` on error.
    """
    try:
        from media import SignalBus
        from media.source import create_source
    except ImportError as exc:
        print(f"Audio requires the media module: {exc}", file=sys.stderr)
        return None, None

    bus: SignalBus = SignalBus()
    try:
        source = create_source(MIC_SOURCE_NAME, {
            "type": "mic",
            "extractors": {"audio": {"bands": band_count}},
        }, bus)
        source.start()
    except Exception as exc:
        print(f"Failed to start microphone: {exc}", file=sys.stderr)
        return None, None

    return bus, source


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="2D terminal matrix preview for LIFX effects.",
        epilog=(
            "Press Ctrl+C to stop.  Resize terminal for more pixels.\n"
            "Use --audio with spectrum2d for a mic-driven visualizer."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "effect", nargs="?", default=None,
        help=f"Effect name (default: {DEFAULT_EFFECT}, "
             f"or {DEFAULT_AUDIO_EFFECT} with --audio)",
    )
    parser.add_argument(
        "--fps", "-f", type=int, default=DEFAULT_FPS,
        help=f"Target FPS (default: {DEFAULT_FPS})",
    )
    parser.add_argument(
        "--audio", "-a", action="store_true",
        help="Enable microphone capture for audio-reactive effects",
    )
    parser.add_argument(
        "--bands", "-b", type=int, default=MIC_BAND_COUNT,
        help=f"FFT frequency bands (default: {MIC_BAND_COUNT})",
    )
    parser.add_argument(
        "--list", "-l", action="store_true", dest="list_effects",
        help="List available effects and exit",
    )
    return parser.parse_args()


def main() -> int:
    """Entry point for the 2D matrix preview tool."""
    args: argparse.Namespace = _parse_args()

    # List mode.
    if args.list_effects:
        names: list[str] = get_effect_names()
        print(f"Available effects ({len(names)}):")
        for name in names:
            print(f"  {name}")
        return 0

    # Default effect depends on --audio flag.
    effect_name: str = args.effect or (
        DEFAULT_AUDIO_EFFECT if args.audio else DEFAULT_EFFECT
    )

    # Validate effect name.
    available: list[str] = get_effect_names()
    if effect_name not in available:
        print(f"Unknown effect: {effect_name}")
        print("Use --list to see available effects.")
        return 1

    # Set up audio if requested.
    bus = None
    mic_source = None
    if args.audio:
        bus, mic_source = _start_mic(band_count=args.bands)
        if bus is None:
            return 1

    # Create matrix emitter — auto-fits to terminal size.
    em: ScreenMatrixEmitter = ScreenMatrixEmitter(
        effect_name=effect_name,
        fps=args.fps,
    )
    em.power_on()

    # Pass pixel dimensions and signal bus to the effect.
    ctrl: Controller = Controller([em], fps=args.fps)
    ctrl.play(
        effect_name,
        signal_bus=bus,
        width=em.pixel_width,
        height=em.pixel_height,
    )

    # Block until Ctrl+C.
    stop: threading.Event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()

    ctrl.stop(fade_ms=0)
    em.close()

    # Clean up audio.
    if mic_source is not None:
        mic_source.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
