#!/usr/bin/env python3
"""One-time interactive Vivint 2FA setup — run on the Pi via SSH.

Authenticates with Vivint's cloud, handles the 2FA challenge, saves
a refresh token so the daemon can connect without future 2FA prompts.

Usage (on the Pi)::

    cd ~/glowup
    python3 vivint_setup.py

The script reads username/password from server.json's ``vivint`` section,
prompts for the 2FA code, then writes the refresh token to
``~/.vivint_token``.  The VivintAdapter loads this token on startup.

If the token file already exists, the script offers to re-authenticate
(useful if the token has expired after a long downtime).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Server config file — expected in the same directory as this script.
CONFIG_FILE: str = "server.json"

# Token file — stored alongside server.json in /etc/glowup/ so it works
# regardless of which user runs the service (root via systemd, pi via CLI).
TOKEN_FILE: Path = Path("/etc/glowup/.vivint_token")

# File permissions: owner read/write only.
TOKEN_FILE_MODE: int = stat.S_IRUSR | stat.S_IWUSR

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    """Run the interactive Vivint setup flow.

    Returns:
        Exit code (0 = success, 1 = error).
    """
    # Lazy import so the script fails fast with a clear message if
    # vivintpy is not installed.
    try:
        from vivintpy.account import Account
        from vivintpy.exceptions import VivintSkyApiMfaRequiredError
    except ImportError:
        print("ERROR: vivintpy is not installed.")
        print("Install with: pip install vivintpy")
        return 1

    # Load config — check /etc/glowup/ first (Pi deployment), then
    # fall back to the script's own directory (dev machine).
    config_path: Path = Path("/etc/glowup") / CONFIG_FILE
    if not config_path.exists():
        config_path = Path(__file__).parent / CONFIG_FILE
    if not config_path.exists():
        print(f"ERROR: {config_path} not found")
        return 1

    with open(config_path, "r") as f:
        config: dict = json.load(f)

    vivint_config: dict = config.get("vivint", {})
    username: str = vivint_config.get("username", "")
    password: str = vivint_config.get("password", "")

    if not username or not password:
        print("ERROR: vivint.username and vivint.password must be set in server.json")
        print("Add your Vivint email and password to the 'vivint' section.")
        return 1

    # Check for existing token.
    if TOKEN_FILE.exists():
        print(f"Existing token found at {TOKEN_FILE}")
        answer: str = input("Re-authenticate? (y/N): ").strip().lower()
        if answer != "y":
            print("Keeping existing token.")
            return 0

    print(f"\nAuthenticating as {username}...")
    print("Vivint will send a 2FA code to your phone/email.\n")

    account: Account = Account(username=username, password=password)

    try:
        await account.connect(load_devices=True, subscribe_for_realtime_updates=False)
        # No 2FA required — unusual but possible.
        print("Connected without 2FA challenge.")
    except VivintSkyApiMfaRequiredError:
        print("2FA code required.")
        code: str = input("Enter the 2FA code: ").strip()
        if not code:
            print("ERROR: No code entered.")
            return 1
        try:
            await account.verify_mfa(code)
        except Exception as exc:
            print(f"ERROR: 2FA verification failed: {exc}")
            return 1
        print("2FA verified.")
    except Exception as exc:
        print(f"ERROR: Authentication failed: {exc}")
        return 1

    # Extract the refresh token from vivintpy's internal token dict.
    # After auth, api.tokens is a dict containing OAuth tokens including
    # "refresh_token".  api.refresh_token is a method (for refreshing),
    # not a getter.
    refresh_token: str = ""
    if hasattr(account, "api") and hasattr(account.api, "tokens"):
        tokens: dict = account.api.tokens
        if isinstance(tokens, dict):
            refresh_token = tokens.get("refresh_token", "")
            print(f"Token dict keys: {list(tokens.keys())}")

    if not refresh_token:
        print("WARNING: Could not extract refresh token.")
        print("The adapter will need username/password on every restart.")
        return 1

    # Write token file with restricted permissions.
    TOKEN_FILE.write_text(refresh_token)
    os.chmod(TOKEN_FILE, TOKEN_FILE_MODE)
    print(f"\nRefresh token saved to {TOKEN_FILE} (mode 0600)")

    # Show discovered locks for verification.
    print("\nDiscovered locks:")
    lock_count: int = 0
    for system in account.systems:
        for alarm_panel in system.alarm_panels:
            for device in alarm_panel.devices:
                # Import DoorLock here since we already confirmed vivintpy works.
                from vivintpy.devices.door_lock import DoorLock
                if isinstance(device, DoorLock):
                    lock_count += 1
                    state: str = "LOCKED" if device.is_locked else "UNLOCKED"
                    battery: str = f"{device.battery_level}%" if device.battery_level is not None else "N/A"
                    print(f"  - {device.name}: {state} (battery: {battery})")

    if lock_count == 0:
        print("  (none found)")
    else:
        print(f"\n{lock_count} lock(s) found.")

    # Clean disconnect.
    try:
        await account.disconnect()
    except Exception:
        pass

    print("\nSetup complete. The GlowUp server will use the saved token.")
    print("If the token expires (long downtime), re-run this script.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
