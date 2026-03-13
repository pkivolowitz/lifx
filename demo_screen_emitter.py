#!/usr/bin/env python3
"""Demo: render a LIFX effect to the terminal via ScreenEmitter.

This was impossible before the emitter abstraction. The engine was
hardcoded to send UDP packets to LIFX devices. Now it renders frames
to any Emitter — including a terminal.

Usage:
    python3 demo_screen_emitter.py [effect_name] [zones]

    python3 demo_screen_emitter.py aurora 80
    python3 demo_screen_emitter.py cylon 50
    python3 demo_screen_emitter.py spin 60

Press Ctrl+C to stop.
"""

import signal
import sys
import threading

from emitters.screen import ScreenEmitter
from engine import Controller

DEFAULT_EFFECT: str = "aurora"
DEFAULT_ZONES: int = 80
DEFAULT_FPS: int = 20


def main() -> None:
    effect_name: str = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EFFECT
    zones: int = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_ZONES

    em = ScreenEmitter(zone_count=zones, label=f"Terminal ({zones} zones)")
    em.power_on()

    ctrl = Controller([em], fps=DEFAULT_FPS)
    ctrl.play(effect_name)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()

    ctrl.stop(fade_ms=0)
    em.close()


if __name__ == "__main__":
    main()
