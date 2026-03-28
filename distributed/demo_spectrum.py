"""Distributed spectrum demo — end-to-end audio analysis pipeline.

Orchestrates all three nodes from a single command:

1. **Pi** (orchestrator): Assigns AudioExtractor work to Judy
2. **Judy** (compute): Receives raw PCM via UDP, runs FFT, publishes
   frequency bands via MQTT back to the Pi broker
3. **Conway** (sensor + display): Captures mic audio, streams PCM to
   Judy via UDP, subscribes to Judy's MQTT output, renders a live
   spectrum analyzer in the terminal

Usage::

    python3 -m distributed.demo_spectrum

Press Ctrl+C to stop.  The script cleans up the work assignment on exit.

Requires:
    - GlowUp server running on Pi with orchestrator enabled
    - Worker agent running on Judy with AudioExtractor capability
    - Auth token in ~/.glowup_token
    - ffmpeg installed (for mic capture)
    - paho-mqtt installed (for MQTT subscription)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from .audio_sensor import AudioSensor
from .spectrum_display import SpectrumDisplay
from network_config import net

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pi server (from centralized network config).
PI_HOST: str = net.broker
PI_PORT: int = 8420
MQTT_BROKER: str = net.broker
MQTT_PORT: int = 1883

# Judy compute node (from centralized network config).
JUDY_IP: str = net.server
JUDY_UDP_PORT: int = 9420

# Signal naming.
SOURCE_NAME: str = "conway"

# Token file.
TOKEN_FILE: str = os.path.expanduser("~/.glowup_token")

# Audio config.
SAMPLE_RATE: int = 44100
BANDS: int = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_token() -> str:
    """Load auth token from ~/.glowup_token.

    Returns:
        The auth token string.

    Raises:
        SystemExit: If the token file is missing.
    """
    path: Path = Path(TOKEN_FILE)
    if not path.exists():
        print(f"Error: {TOKEN_FILE} not found.", file=sys.stderr)
        print("Create it with your server's auth_token value.",
              file=sys.stderr)
        sys.exit(1)
    return path.read_text().strip()


def _api_call(method: str, path: str, token: str,
              body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Make an authenticated API call to the Pi server.

    Args:
        method: HTTP method (GET, POST).
        path:   API path (e.g. "/api/fleet").
        token:  Auth token.
        body:   Optional JSON body for POST.

    Returns:
        Parsed JSON response.

    Raises:
        SystemExit: On connection failure.
    """
    url: str = f"http://{PI_HOST}:{PI_PORT}{path}"
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req: Request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        print(f"Error: Cannot reach Pi server at {url}: {exc}",
              file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the distributed spectrum demo."""
    print("╔══════════════════════════════════════════════╗")
    print("║   GlowUp Distributed Spectrum Demo          ║")
    print("║   Conway → Judy → Pi → Terminal Display     ║")
    print("╚══════════════════════════════════════════════╝")
    print()

    # Load auth token.
    token: str = _load_token()
    print(f"  Token loaded from {TOKEN_FILE}")

    # Check fleet — is Judy online?
    print("  Checking fleet status...", end="", flush=True)
    fleet: dict[str, Any] = _api_call("GET", "/api/fleet", token)
    if not fleet.get("enabled"):
        print("\n  Error: Distributed compute not enabled on Pi.",
              file=sys.stderr)
        sys.exit(1)

    judy_online: bool = False
    for node in fleet.get("nodes", []):
        if node["node_id"] == "judy" and node["online"]:
            judy_online = True
            break

    if not judy_online:
        print("\n  Error: Judy is not online.", file=sys.stderr)
        sys.exit(1)
    print(" Judy online ✓")

    # Issue work assignment.
    print("  Assigning AudioExtractor to Judy...", end="", flush=True)
    assignment: dict[str, Any] = _api_call("POST", "/api/assign", token, {
        "node_id": "judy",
        "operator": "AudioExtractor",
        "config": {
            "source_name": SOURCE_NAME,
            "sample_rate": SAMPLE_RATE,
            "bands": BANDS,
        },
        "inputs": [{
            "signal_name": f"{SOURCE_NAME}:audio:pcm_raw",
            "transport": "udp",
            "udp_port": JUDY_UDP_PORT,
        }],
        "outputs": [{
            "signal_name": f"{SOURCE_NAME}:audio:bands",
            "transport": "mqtt",
        }],
    })

    assignment_id: str = assignment.get("assignment_id", "")
    if not assignment.get("assigned"):
        print(f"\n  Error: Assignment failed: {assignment}",
              file=sys.stderr)
        sys.exit(1)
    print(f" {assignment_id} ✓")

    # Start audio sensor.
    print(f"  Starting mic capture → Judy ({JUDY_IP}:{JUDY_UDP_PORT})...",
          end="", flush=True)
    sensor: AudioSensor = AudioSensor(
        target_ip=JUDY_IP,
        target_port=JUDY_UDP_PORT,
        sample_rate=SAMPLE_RATE,
        signal_name=f"{SOURCE_NAME}:audio:pcm_raw",
    )
    sensor.start()
    print(" streaming ✓")

    # Give Judy a moment to start processing.
    print("  Waiting for Judy to process first frames...", end="", flush=True)
    time.sleep(2)
    print(" ready ✓")
    print()
    print("  Launching spectrum display — press Ctrl+C to stop")
    time.sleep(1)

    # Start spectrum display (takes over the terminal).
    display: SpectrumDisplay = SpectrumDisplay(
        broker=MQTT_BROKER,
        port=MQTT_PORT,
        source_name=SOURCE_NAME,
    )

    def _shutdown(signum: int, frame: object) -> None:
        """Clean shutdown: stop display, sensor, cancel assignment."""
        display.stop()
        sensor.stop()

        # Cancel the work assignment on Judy.
        print("\n  Cancelling assignment on Judy...", end="", flush=True)
        try:
            _api_call(
                "POST",
                f"/api/assign/judy/cancel/{assignment_id}",
                token,
            )
            print(" done")
        except SystemExit:
            print(" failed (server unreachable)")

        print("  Demo stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    display.start()


if __name__ == "__main__":
    main()
