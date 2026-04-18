"""Allow ``python3 -m sdr`` to run the SDR service."""

__version__: str = "1.0.0"

from sdr.service import main

main()
