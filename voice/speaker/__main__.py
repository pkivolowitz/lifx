"""Allow ``python -m voice.speaker`` to launch the daemon."""

__version__: str = "1.0.0"

from voice.speaker.daemon import main

main()
