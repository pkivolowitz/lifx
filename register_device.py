#!/usr/bin/env python3
"""Quick device registration helper — API client.

Registers LIFX devices via the GlowUp server API.  The server owns the
registry file at ``/etc/glowup/device_registry.json``; this script is a
thin HTTP client that talks to the server's ``/api/registry`` endpoints.

Usage::

    python3 register_device.py <ip> "Label Name"   # register with label
    python3 register_device.py <ip>                 # prompts for label
    python3 register_device.py --list               # show registry
    python3 register_device.py --push-labels        # write all labels to bulbs
    python3 register_device.py --clear-label <ip>   # blank the firmware label
    python3 register_device.py --remove <mac-or-label>  # unregister a device
    python3 register_device.py --help               # this message

Designed for rapid use during a bulk identification session.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from network_config import net

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default GlowUp server address.
SERVER_HOST: str = net.server

#: Default GlowUp server port.
SERVER_PORT: int = 8420

#: Path to the bearer-token file.
TOKEN_PATH: Path = Path.home() / ".glowup_token"

#: HTTP timeout for API requests (seconds).
API_TIMEOUT: float = 10.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _read_token() -> str:
    """Read the bearer token from ``~/.glowup_token``.

    Returns:
        The token string, stripped of whitespace.

    Raises:
        SystemExit: If the token file does not exist.
    """
    if not TOKEN_PATH.exists():
        print(
            f"ERROR: Token file not found: {TOKEN_PATH}\n"
            "Create it with: echo '<token>' > ~/.glowup_token",
            file=sys.stderr,
        )
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


def _server_url(path: str) -> str:
    """Build a full server URL for the given API path.

    Args:
        path: API path (e.g. ``/api/registry``).
    """
    return f"http://{SERVER_HOST}:{SERVER_PORT}{path}"


def _api_get(path: str) -> dict[str, Any]:
    """Authenticated GET request to the server.

    Args:
        path: API path.

    Returns:
        Parsed JSON response dict.
    """
    token: str = _read_token()
    req: urllib.request.Request = urllib.request.Request(
        _server_url(path),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(f"ERROR: Cannot reach server: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def _api_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Authenticated POST request with JSON body.

    Args:
        path: API path.
        body: JSON-serializable request body.

    Returns:
        Parsed JSON response dict.
    """
    token: str = _read_token()
    data: bytes = json.dumps(body).encode("utf-8")
    req: urllib.request.Request = urllib.request.Request(
        _server_url(path),
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err_body: dict = json.loads(exc.read())
            print(f"ERROR: {err_body.get('error', exc)}", file=sys.stderr)
        except Exception:
            print(f"ERROR: HTTP {exc.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"ERROR: Cannot reach server: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def _api_delete(path: str) -> dict[str, Any]:
    """Authenticated DELETE request.

    Args:
        path: API path.

    Returns:
        Parsed JSON response dict.
    """
    token: str = _read_token()
    req: urllib.request.Request = urllib.request.Request(
        _server_url(path),
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err_body: dict = json.loads(exc.read())
            print(f"ERROR: {err_body.get('error', exc)}", file=sys.stderr)
        except Exception:
            print(f"ERROR: HTTP {exc.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"ERROR: Cannot reach server: {exc.reason}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list() -> None:
    """List all registered devices with live status."""
    data: dict[str, Any] = _api_get("/api/registry")
    devices: list[dict] = data.get("devices", [])

    if not devices:
        print("(empty registry)")
        return

    print(
        f"\n{'MAC Address':19}  {'Label':24}  {'IP Address':15}  "
        f"{'Status':8}  {'Notes'}"
    )
    print("=" * 80)
    for d in devices:
        mac: str = d.get("mac", "?")
        label: str = d.get("label", "?")
        ip: str = d.get("ip", "") or "-"
        status: str = "online" if d.get("online") else "offline"
        notes: str = d.get("notes", "")
        print(f"{mac:19}  {label:24}  {ip:15}  {status:8}  {notes}")

    print(f"\n{len(devices)} device(s) registered.")


def cmd_add(ip: str, label: str) -> None:
    """Register a device by IP address.

    The server resolves the IP to a MAC via its ARP table, registers
    the device, and writes the label to the bulb firmware.

    Args:
        ip:    Device IP address.
        label: User-defined label.
    """
    result: dict[str, Any] = _api_post("/api/registry/device", {
        "ip": ip,
        "label": label,
    })

    mac: str = result.get("mac", "?")
    fw: bool = result.get("firmware_written", False)

    print(f"Registered: {mac} → {label}")
    if fw:
        print(f"Label written to bulb firmware: {label}")
    else:
        print("WARNING: Could not write label to bulb (timeout or offline)")


def cmd_remove(identifier: str) -> None:
    """Remove a device by MAC address or label.

    Args:
        identifier: MAC address or label.
    """
    # URL-encode colons for MAC addresses in path.
    encoded: str = urllib.request.quote(identifier, safe="")
    result: dict[str, Any] = _api_delete(
        f"/api/registry/device/{encoded}"
    )
    print(f"Removed: {result.get('removed', identifier)}")


def cmd_clear_label(ip: str) -> None:
    """Clear (blank) the firmware label on a bulb via the server API.

    Sends an empty label to ``POST /api/registry/push-label`` which
    writes a null label to the device firmware.  The server handles
    device communication and registry consistency.

    Args:
        ip: Device IP address.
    """
    result: dict[str, Any] = _api_post("/api/registry/push-label", {
        "ip": ip,
        "label": "",
    })
    fw: bool = result.get("firmware_written", False)
    if fw:
        print(f"Label cleared on {ip}")
    else:
        print(f"WARNING: No ack from {ip} — bulb may be offline")


def cmd_push_labels() -> None:
    """Write all registry labels to bulb firmware."""
    data: dict[str, Any] = _api_post("/api/registry/push-labels", {})
    results: list[dict] = data.get("results", [])

    if not results:
        print("(empty registry — nothing to push)")
        return

    ok: int = 0
    failed: int = 0
    offline: int = 0

    for r in results:
        status: str = r.get("status", "?")
        label: str = r.get("label", "?")
        mac: str = r.get("mac", "?")
        ip: str = r.get("ip", "")

        if status == "ok":
            print(f"  OK       {mac}  {label} → {ip}")
            ok += 1
        elif status == "offline":
            print(f"  OFFLINE  {mac}  {label}")
            offline += 1
        else:
            print(f"  FAILED   {mac}  {label} → {ip}  ({status})")
            failed += 1

    print(f"\nPushed: {ok}  Failed: {failed}  Offline: {offline}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point — parse args and dispatch."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg1: str = sys.argv[1]

    if arg1 in ("--help", "-h"):
        print(__doc__)
        sys.exit(0)
    elif arg1 == "--list":
        cmd_list()
    elif arg1 == "--push-labels":
        cmd_push_labels()
    elif arg1 == "--remove":
        if len(sys.argv) < 3:
            print("Usage: register_device.py --remove <mac-or-label>",
                  file=sys.stderr)
            sys.exit(1)
        cmd_remove(sys.argv[2])
    elif arg1 == "--clear-label":
        if len(sys.argv) < 3:
            print("Usage: register_device.py --clear-label <ip>",
                  file=sys.stderr)
            sys.exit(1)
        cmd_clear_label(sys.argv[2])
    else:
        # register_device.py <ip> [label]
        ip: str = arg1
        if len(sys.argv) >= 3:
            label: str = sys.argv[2]
        else:
            label = input(f"Label for {ip}: ").strip()
            if not label:
                print("ERROR: Label cannot be empty", file=sys.stderr)
                sys.exit(1)
        cmd_add(ip, label)


if __name__ == "__main__":
    main()
