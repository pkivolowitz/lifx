"""Zigbee smart-plug emitter — drives plugs via the broker-2 zigbee_service HTTP API.

Plugs are binary devices (ON/OFF).  This emitter mirrors the shape of
:class:`emitters.lifx.LifxEmitter`: two creation paths (config-based
and programmatic), SOE lifecycle (``on_configure``, ``on_open``,
``on_emit``, ``on_close``), and engine-facing ``power_on`` /
``power_off`` methods.  Group-level power fan-out can therefore call
either emitter through the same signature with no branch on device
class.

The transport is HTTP: the hub POSTs to broker-2's ``zigbee_service``
at ``http://{broker}:{port}/devices/{name}/state`` with body
``{"state": "ON"|"OFF"}``.  The service waits for the device to echo
the new state before responding 200; on echo timeout it returns 504.
See ``docs/29-zigbee-service.md`` for the full service contract.

Two creation paths:

**Config-based** (via EmitterManager / ``create_emitter()``)::

    emitter = create_emitter("zigbee_plug", "LRTV", {
        "broker": "<broker-2 host>",
        "http_port": 8422,
        "device_name": "LRTV",
    })

**Programmatic** (via factory classmethod)::

    emitter = ZigbeePlugEmitter.from_plug(
        name="LRTV", broker_host="<broker-2 host>",
    )
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "0.1"

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from emitters import Emitter, EmitterCapabilities

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default broker-2 HTTP port — matches GLZ_HTTP_PORT in
# zigbee_service/service.py.  Keep these two in lockstep or the hub
# will silently miss the plug service on a deployment that has
# overridden the default.
_DEFAULT_BROKER_PORT: int = 8422

# Default HTTP timeout (seconds).  Must exceed the service-side
# CMD_ECHO_TIMEOUT plus network overhead; otherwise a correctly-
# executing command looks like a client-side failure and we retry
# redundantly.  The service's echo-wait is currently 6 s.
_DEFAULT_TIMEOUT_SEC: float = 8.0

# Maximum meaningful update rate.  Each command round-trips through
# Z2M to the device over an 802.15.4 mesh and waits for an echo;
# flooding the pipeline congests the mesh for other devices.
_MAX_RATE_HZ: float = 1.0

# Frame type accepted by on_emit() — see EmitterCapabilities.
_FRAME_TYPE_BINARY: str = "binary"

# ON/OFF state strings — the zigbee_service API contract.
_STATE_ON: str = "ON"
_STATE_OFF: str = "OFF"

# JSON content type header for POST body.
_CONTENT_TYPE_JSON: str = "application/json"

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.emitters.zigbee_plug")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PlugCommandError(RuntimeError):
    """Raised when a plug command cannot be completed.

    Covers HTTP failures, echo timeouts, service-side 5xx, and JSON
    parse errors.  The original cause is always chained via
    ``__cause__`` so callers can diagnose without grepping strings.
    """


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

class ZigbeePlugEmitter(Emitter):
    """Emitter that drives a single Zigbee smart plug via zigbee_service HTTP.

    Plugs have no HSBK semantics — effects that produce HSBK frames
    should not be routed here.  :meth:`on_emit` accepts only ``bool``
    frames.  The :meth:`power_on` / :meth:`power_off` interface mirrors
    :class:`emitters.lifx.LifxEmitter` so group-level power fan-out
    treats both device classes uniformly.

    HTTP is stateless; there is no persistent connection to manage.
    Each command opens and closes its own TCP connection via
    :mod:`urllib.request`.  Thread safety at this layer is therefore
    limited to the last-known-state cache, which is written only after
    a successful command round-trip.
    """

    emitter_type: str = "zigbee_plug"
    description: str = "Zigbee smart plug via broker-2 zigbee_service HTTP API"

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize from a name and configuration dict.

        Args:
            name:   Instance name (typically the plug's Z2M friendly name).
            config: Instance configuration dict.  Recognized keys:

                    * ``"broker"`` — broker-2 hostname or IP (required
                      for config-based creation).
                    * ``"http_port"`` — HTTP port (default 8422).
                    * ``"device_name"`` — Z2M friendly name.  Defaults
                      to *name* when omitted, matching the common case
                      where both come from the same labeling authority.
                    * ``"timeout_sec"`` — HTTP timeout (default 8.0).
        """
        super().__init__(name, config)

        # Z2M friendly name that identifies the plug on broker-2.
        self._device_name: str = str(config.get("device_name") or name)

        # Transport config.  Validated in on_configure so from_plug()
        # can inject it without going through the config dict.
        self._broker_host: str = str(config.get("broker", ""))
        self._http_port: int = int(
            config.get("http_port", _DEFAULT_BROKER_PORT))
        self._timeout_sec: float = float(
            config.get("timeout_sec", _DEFAULT_TIMEOUT_SEC))

        # Last-known state cache.  ``None`` until the first successful
        # command or state query.  Used to skip redundant POSTs when
        # the caller repeats a command (idempotent operations rule).
        # On a failed command the cache is NOT updated, so the next
        # call naturally retries.
        self._last_state: Optional[str] = None

    @classmethod
    def from_plug(cls, name: str, broker_host: str,
                  device_name: Optional[str] = None,
                  http_port: int = _DEFAULT_BROKER_PORT,
                  ) -> "ZigbeePlugEmitter":
        """Create a ZigbeePlugEmitter programmatically.

        Mirrors :meth:`emitters.lifx.LifxEmitter.from_device` — a
        future PlugManager will use this to materialize emitters from
        its registry without going through config dicts.

        Args:
            name:        Instance name (typically the friendly name).
            broker_host: broker-2 hostname or IP.
            device_name: Z2M friendly name.  Defaults to *name*.
            http_port:   HTTP port (default 8422).

        Returns:
            A :class:`ZigbeePlugEmitter` ready for use after
            :meth:`on_configure`.
        """
        config: dict[str, Any] = {
            "broker": broker_host,
            "http_port": http_port,
            "device_name": device_name or name,
        }
        return cls(name, config)

    # --- SOE lifecycle (called by EmitterManager) --------------------------

    def on_configure(self, config: dict[str, Any]) -> None:
        """Validate that a broker host was provided.

        The connection itself is stateless HTTP — there is no socket
        to open at configure time.  We only check that we have
        somewhere to send.

        Args:
            config: Full server configuration dict (unused; accepted
                    for ABC conformance).

        Raises:
            ValueError: If no broker host was configured.
        """
        if not self._broker_host:
            raise ValueError(
                f"ZigbeePlugEmitter '{self.name}' requires 'broker' in "
                f"config"
            )

    def on_open(self) -> None:
        """No-op — HTTP is stateless, no connection to establish."""

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Dispatch a binary frame to the plug.

        Accepts ``bool`` frames (True → ON, False → OFF).  Other frame
        types are logged and treated as a failure — plugs have no
        HSBK semantics and effects driving HSBK frames should not be
        routed to a plug emitter.

        Args:
            frame:    ``bool`` — desired plug state.
            metadata: Per-frame context dict (see :class:`Emitter`).
                      Unused by this emitter but accepted for ABC
                      conformance.

        Returns:
            ``True`` on successful command, ``False`` on failure.
        """
        if not isinstance(frame, bool):
            logger.warning(
                "ZigbeePlugEmitter '%s' received unsupported frame type: "
                "%s (plugs accept only bool)",
                self.name, type(frame).__name__,
            )
            return False
        try:
            self.set_power(on=frame)
            return True
        except PlugCommandError as exc:
            logger.warning(
                "ZigbeePlugEmitter '%s' set_power failed: %s",
                self.name, exc,
            )
            return False

    def on_close(self) -> None:
        """No-op — no persistent resources to release."""

    def capabilities(self) -> EmitterCapabilities:
        """Declare plug capabilities — binary frames at ≤ 1 Hz.

        Returns:
            An :class:`EmitterCapabilities` for a binary sink.
        """
        return EmitterCapabilities(
            accepted_frame_types=[_FRAME_TYPE_BINARY],
            max_rate_hz=_MAX_RATE_HZ,
            extra={"device_name": self._device_name},
        )

    # --- Engine-facing properties (mirrors LifxEmitter) --------------------

    @property
    def emitter_id(self) -> str:
        """Unique identifier — the plug's Z2M friendly name."""
        return self._device_name

    @property
    def label(self) -> str:
        """Human-readable label — the friendly name for plugs."""
        return self._device_name

    @property
    def product_name(self) -> str:
        """Product description.  Plugs do not self-identify here."""
        return "Zigbee smart plug"

    @property
    def last_state(self) -> Optional[str]:
        """Most recent confirmed state (``"ON"``, ``"OFF"``, or ``None``).

        ``None`` means no command has succeeded and :meth:`query_state`
        has not been called in this process.  Callers that need live
        state should call :meth:`query_state` explicitly rather than
        trusting the cache.
        """
        return self._last_state

    # --- Engine-facing control (mirrors LifxEmitter.power_on/off) ----------

    def power_on(self, duration_ms: int = 0) -> None:
        """Turn the plug on.

        Args:
            duration_ms: Ignored — plugs have no transition.  Accepted
                         for signature compatibility with
                         :meth:`LifxEmitter.power_on` so group power
                         fan-out can call both uniformly.

        Raises:
            PlugCommandError: On any HTTP, JSON, or echo-timeout
                              failure.
        """
        del duration_ms  # accepted for signature compat, not used
        self.set_power(on=True)

    def power_off(self, duration_ms: int = 0) -> None:
        """Turn the plug off.

        Args:
            duration_ms: Ignored — see :meth:`power_on`.

        Raises:
            PlugCommandError: As :meth:`power_on`.
        """
        del duration_ms
        self.set_power(on=False)

    def set_power(self, on: bool) -> None:
        """Command the plug to the requested state.

        Idempotent: if the cached last-known state already matches the
        request, the HTTP POST is skipped.  A fresh process (no cached
        state) always issues the command — the cost is one round-trip
        and the benefit is correctness after a restart.

        On any failure the cache is NOT updated, so the next call
        naturally retries.

        Args:
            on: ``True`` for ON, ``False`` for OFF.

        Raises:
            PlugCommandError: On any HTTP, JSON, or echo-timeout
                              failure.  The underlying cause is
                              chained for diagnostics.
        """
        desired: str = _STATE_ON if on else _STATE_OFF
        if self._last_state == desired:
            logger.debug(
                "ZigbeePlugEmitter '%s' already in state %s — skipping",
                self.name, desired,
            )
            return
        response: dict[str, Any] = self._post_state(desired)
        # Trust the echoed current_state over our request — the device
        # is authoritative and may report something else if the relay
        # was overridden locally between the command and the echo.
        current: Any = response.get("current_state")
        self._last_state = current if isinstance(current, str) else desired

    def close(self) -> None:
        """Compatibility wrapper around :meth:`on_close`.

        Provided for symmetry with :meth:`LifxEmitter.close` — code
        that calls ``emitter.close()`` directly works on both classes.
        """
        self.on_close()

    # --- Plug-specific (not part of Emitter ABC) --------------------------

    def query_state(self) -> dict[str, Any]:
        """Fetch the current state of the plug from broker-2.

        Updates the last-known-state cache if the response carries a
        string ``state`` field.

        Returns:
            The ``/devices/{name}`` JSON body — typically includes
            ``state``, ``power_w``, ``online``, ``age_sec``.

        Raises:
            PlugCommandError: On any HTTP or JSON failure.
        """
        url: str = self._device_url()
        try:
            with urllib.request.urlopen(
                    url, timeout=self._timeout_sec) as resp:
                raw: bytes = resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PlugCommandError(
                f"HTTP query to {url} failed: {exc}") from exc

        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlugCommandError(
                f"Non-JSON response from {url}: {exc}") from exc

        state: Any = data.get("state")
        if isinstance(state, str):
            self._last_state = state
        return data

    def get_info(self) -> dict[str, Any]:
        """Return plug status information for API responses.

        Returns:
            JSON-serializable dict with identity, transport config,
            and last-known state.  Does not issue an HTTP call —
            callers that need live state should invoke
            :meth:`query_state` first.
        """
        return {
            "id": self.emitter_id,
            "label": self.label,
            "device_name": self._device_name,
            "broker": self._broker_host,
            "http_port": self._http_port,
            "last_state": self._last_state,
        }

    # --- Private helpers ---------------------------------------------------

    def _device_url(self) -> str:
        """Return the base ``/devices/{name}`` URL for this plug."""
        return (
            f"http://{self._broker_host}:{self._http_port}"
            f"/devices/{self._device_name}"
        )

    def _state_url(self) -> str:
        """Return the POST ``/devices/{name}/state`` URL for this plug."""
        return f"{self._device_url()}/state"

    def _post_state(self, desired: str) -> dict[str, Any]:
        """POST the desired state to broker-2 and parse the response.

        Args:
            desired: ``"ON"`` or ``"OFF"``.

        Returns:
            The JSON response body from the service — includes
            ``echoed``, ``current_state``, and ``power_w`` on success.

        Raises:
            PlugCommandError: On any HTTP, JSON, service-side 5xx, or
                              echo-timeout failure.
        """
        url: str = self._state_url()
        payload: bytes = json.dumps({"state": desired}).encode("utf-8")
        req: urllib.request.Request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": _CONTENT_TYPE_JSON},
        )

        try:
            with urllib.request.urlopen(
                    req, timeout=self._timeout_sec) as resp:
                raw: bytes = resp.read()
        except urllib.error.HTTPError as exc:
            # Non-2xx response.  504 means the device did not echo in
            # time; the command MAY still have been sent on-air.  Other
            # codes (400, 404, 500) are hard failures.
            body: str = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except OSError as read_exc:
                logger.debug(
                    "Could not read error body from %s: %s", url, read_exc)
            raise PlugCommandError(
                f"HTTP {exc.code} from {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PlugCommandError(
                f"HTTP POST to {url} failed: {exc}") from exc

        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlugCommandError(
                f"Non-JSON response from {url}: {exc}") from exc

        if not data.get("echoed"):
            raise PlugCommandError(
                f"Plug '{self._device_name}' did not echo {desired}: "
                f"{data}"
            )
        return data
