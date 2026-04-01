"""Minimal HTTP server for the GlowUp standalone clock.

Serves the clock HTML and config.json from the local filesystem.
Runs on localhost:8080 — Chromium kiosk points here.

No external dependencies beyond Python stdlib.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import http.server
import os
import sys

# Port for the local clock server.
PORT: int = 8080

# Serve files from the directory containing this script.
SERVE_DIR: str = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    """Start the clock file server."""
    os.chdir(SERVE_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    # Suppress per-request log spam on a headless kiosk.
    handler.log_message = lambda *_args: None
    server = http.server.HTTPServer(("127.0.0.1", PORT), handler)
    print(f"Clock server on http://127.0.0.1:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
