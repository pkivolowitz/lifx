"""HTTP client for glowup-zigbee-service.

The service owns the Zigbee radio and exposes a small JSON HTTP API
on broker-2:8422 (see ``service.py``).  This module is the canonical
client for that API — every hub subsystem that needs to read or
command Zigbee devices routes through ``ZigbeeControlClient`` rather
than hand-rolling urllib calls.

Why a shared client:

- **Positive handoff.**  Plug commands aren't fire-and-forget; the
  service blocks waiting for the device to echo its new state.  The
  client surfaces ``echoed`` on every ``set_state`` call so callers
  can treat a missing echo as a hard failure instead of assuming
  success (per Perry's "military device" rule — every stage confirms
  receipt).
- **One place to widen the interface.**  When phase-3 group
  integration adds more endpoints, one module grows — not four.
- **Stdlib only.**  urllib + json keeps voice/coordinator (runs on
  Daedalus) and the hub both able to import this without pulling
  paho-mqtt or sqlite3.

Usage::

    from zigbee_service.client import ZigbeeControlClient
    client = ZigbeeControlClient("http://10.0.0.123:8422")

    result = client.set_state("MBTV", "ON")
    if result.ok and result.echoed:
        ...                         # device acknowledged
    elif result.ok and not result.echoed:
        ...                         # service accepted but device silent
    else:
        logger.warning("%s", result.error)

    ok, devices = client.list_devices(type_filter="plug")
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default HTTP timeout (seconds).  Plugs actuate in <500 ms over Zigbee,
# and the service waits up to CMD_ECHO_TIMEOUT_SEC (5 s) for the echoed
# state.  Client timeout is intentionally one second looser than the
# server-side wait so we see the service's structured 504 rather than
# racing it to a client-side TimeoutError.
DEFAULT_TIMEOUT_S: float = 6.0

# Valid power states the service accepts on POST /devices/{name}/state.
VALID_STATES: frozenset[str] = frozenset({"ON", "OFF"})

logger: logging.Logger = logging.getLogger("glowup.zigbee_client")


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    """Outcome of a ``set_state`` call.

    ``ok`` and ``echoed`` are orthogonal: the service may accept the
    command and publish it to Z2M (``ok=True``) while the device fails
    to acknowledge within the echo timeout (``echoed=False``) — this
    is the "command sent, state unknown" state every realistic Zigbee
    deployment eventually hits.
    """
    ok: bool
    echoed: bool
    state: Optional[str]      # echoed state if device acknowledged
    power_w: Optional[float]  # echoed power reading if available
    error: Optional[str]      # human-readable error, None on success


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ZigbeeControlClient:
    """Stdlib HTTP client for glowup-zigbee-service on broker-2.

    Thread-safe: every call opens its own urllib request, no shared
    mutable state beyond the immutable config captured at __init__.
    """

    def __init__(
        self,
        base_url: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """Configure the client.

        Args:
            base_url:   Root URL of the service (e.g.
                        ``http://10.0.0.123:8422``).  A trailing slash
                        is tolerated.
            timeout_s:  Per-request HTTP timeout in seconds.
        """
        if not base_url:
            raise ValueError("base_url must be a non-empty URL")
        self._base: str = base_url.rstrip("/")
        self._timeout: float = timeout_s

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_state(self, name: str, state: str) -> CommandResult:
        """Set a plug's power state to ON or OFF.

        Surfaces the service's ``echoed`` field — callers should treat
        ``echoed=False`` as a command-sent-but-unconfirmed outcome,
        not a success.  The service itself blocks up to its echo
        timeout before returning, so a False from here means the
        device genuinely did not acknowledge.
        """
        if not name:
            return CommandResult(
                ok=False, echoed=False, state=None, power_w=None,
                error="device name must be non-empty",
            )
        # Validate at the system boundary per coding standards — the
        # service rejects invalid states with 400, but failing locally
        # skips a round trip and gives a clearer error.
        normalized: str = state.upper() if isinstance(state, str) else ""
        if normalized not in VALID_STATES:
            return CommandResult(
                ok=False, echoed=False, state=None, power_w=None,
                error=f"state must be one of {sorted(VALID_STATES)}",
            )

        path: str = f"/devices/{urllib.parse.quote(name, safe='')}/state"
        ok, payload = self._request("POST", path, {"state": normalized})
        if not ok:
            # payload is a string error in the not-ok branch.
            return CommandResult(
                ok=False, echoed=False, state=None, power_w=None,
                error=str(payload),
            )
        # Service shape on success: {device, desired, echoed, current_state, power_w}
        # Service shape on 504:     {device, desired, echoed=False, error}
        if not isinstance(payload, dict):
            return CommandResult(
                ok=False, echoed=False, state=None, power_w=None,
                error=f"unexpected response shape: {type(payload).__name__}",
            )
        echoed: bool = bool(payload.get("echoed", False))
        cur_state: Optional[str] = payload.get("current_state")
        power_raw: Any = payload.get("power_w")
        power_w: Optional[float] = None
        if isinstance(power_raw, (int, float)):
            power_w = float(power_raw)
        err: Optional[str] = None
        if not echoed:
            # Surface the service's own error string if present.
            err = str(payload.get("error", "device did not acknowledge"))
        return CommandResult(
            ok=True, echoed=echoed, state=cur_state, power_w=power_w,
            error=err,
        )

    def get_device(self, name: str) -> tuple[bool, Any]:
        """Fetch a single device's full current state.

        Returns ``(True, device_dict)`` on success, ``(False, error_str)``
        on failure (including 404 for unknown devices).
        """
        path: str = f"/devices/{urllib.parse.quote(name, safe='')}"
        return self._request("GET", path)

    def list_devices(
        self, type_filter: Optional[str] = None,
    ) -> tuple[bool, Any]:
        """List every tracked device, optionally filtered by type.

        ``type_filter`` accepts any TYPE_* string from device_types.
        An unknown filter string returns an empty list from the
        service rather than an error, so rolling upgrades are safe.

        Returns ``(True, [device_dict, ...])`` on success; the outer
        envelope (``{"devices": [...]}``) is unwrapped here so callers
        get a plain list.
        """
        path: str = "/devices"
        if type_filter is not None:
            path = f"{path}?type={urllib.parse.quote(type_filter, safe='')}"
        ok, payload = self._request("GET", path)
        if not ok:
            return (False, payload)
        if not isinstance(payload, dict) or "devices" not in payload:
            return (False, f"unexpected response shape: {payload!r}")
        return (True, payload["devices"])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None,
    ) -> tuple[bool, Any]:
        """Execute one HTTP request and parse the JSON response.

        Returns (True, parsed) on 2xx; (False, "error string") on any
        transport or HTTP failure.  Every exception is logged at DEBUG
        and translated to a short error — corrupt input must not
        crash the caller, per coding standards.
        """
        url: str = f"{self._base}{path}"
        data: Optional[bytes] = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw: bytes = resp.read()
        except urllib.error.HTTPError as exc:
            # Service 4xx/5xx returns structured JSON — try to surface it.
            # Close the body explicitly so the interpreter doesn't emit a
            # ResourceWarning from __del__ cleanup (Python 3.12+).
            try:
                body_bytes: bytes = exc.read() or b""
            except OSError:
                body_bytes = b""
            finally:
                exc.close()
            try:
                err_payload = json.loads(body_bytes or b"{}")
                detail: str = err_payload.get("error", f"HTTP {exc.code}")
            except ValueError:
                detail = f"HTTP {exc.code}"
            logger.debug("%s %s → %s: %s", method, url, exc.code, detail)
            return (False, detail)
        except urllib.error.URLError as exc:
            logger.debug("%s %s → URLError: %s", method, url, exc.reason)
            return (False, f"unreachable: {exc.reason}")
        except TimeoutError as exc:
            logger.debug("%s %s → timeout: %s", method, url, exc)
            return (False, "request timed out")
        except Exception as exc:  # defensive — never let transport crash caller
            logger.warning("%s %s → unexpected error: %s", method, url, exc)
            return (False, f"{type(exc).__name__}: {exc}")

        if not raw:
            return (True, {})
        try:
            return (True, json.loads(raw))
        except ValueError as exc:
            logger.debug("%s %s → non-JSON body: %s", method, url, exc)
            return (False, f"non-JSON response: {exc}")
