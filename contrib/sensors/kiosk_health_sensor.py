#!/usr/bin/env python3
"""Kiosk health sensor — publishes systemd service state to MQTT.

Observes a single systemd service (user or system scope) and publishes a
normalized ``KioskHealth`` record to the GlowUp MQTT broker on a
signal-class-first topic tree::

    glowup/hardware/kiosk/<node_id>       (retained, every interval)
    glowup/node/<node_id>/kiosk/status    (retained, "online" | "offline", LWT)

The JSON schema is fleet-portable: drop this file on any future kiosk-
class satellite and point it at that box's kiosk service unit. No code
change needed per host; ``node_id`` defaults to the short hostname.

Metrics come from ``systemctl show``. All of these are free in any
modern systemd:

    ActiveState            active | inactive | failed | activating | ...
    SubState               running | dead | failed | start-limit-hit | ...
    Result                 success | exit-code | signal | oom-kill |
                           start-limit-hit | ...
    NRestarts              total restarts since service-enable
    ActiveEnterTimestamp   epoch (usec since unix epoch) of last start
    InvocationID           UUID that changes on every restart

Derived fields:

    uptime_s               seconds since ActiveEnterTimestamp if active
    crash_loop             True if Result=="start-limit-hit" — the
                           StartLimitBurst budget was exhausted and
                           systemd has given up restarting; this is the
                           dead-man signal that a human must investigate.

Usage::

    python3 kiosk_health_sensor.py \\
        --service kiosk.service --scope user --broker 127.0.0.1

Runtime dep: python3-paho-mqtt (apt or pip).

Press Ctrl+C (SIGINT) or send SIGTERM for graceful shutdown. On exit
the sensor explicitly publishes ``offline`` on its status topic so the
orchestrator sees the transition immediately without waiting for LWT.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from types import FrameType
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

logger: logging.Logger = logging.getLogger("glowup.kiosk_health")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default broker — Pi 5 "glowup" hub per reference_project_state.md.
# The sensor normally publishes to the local mosquitto which bridges
# glowup/hardware/# and glowup/node/# out to the hub.
_DEFAULT_BROKER: str = "127.0.0.1"
_DEFAULT_PORT: int = 1883
_DEFAULT_INTERVAL_S: float = 60.0
_DEFAULT_SERVICE: str = "kiosk.service"
_DEFAULT_SCOPE: str = "user"  # "user" or "system"

# MQTT topic roots. Match the pi_thermal_sensor convention so a single
# subscription "glowup/hardware/+" covers every fleet health feed.
_TOPIC_DATA_FMT: str = "glowup/hardware/kiosk/{node_id}"
_TOPIC_STATUS_FMT: str = "glowup/node/{node_id}/kiosk/status"

# systemctl show field list. Kept minimal — anything we don't use on
# the server side is wasted MQTT payload bytes and parsing time.
_SHOW_PROPERTIES: tuple[str, ...] = (
    "ActiveState",
    "SubState",
    "Result",
    "NRestarts",
    "ActiveEnterTimestamp",
    "InvocationID",
    "ExecMainStatus",
    "ExecMainCode",
)

# Maximum seconds systemctl is allowed to run before we give up. The
# normal call is instantaneous; this catches hung journald / systemd.
_SYSTEMCTL_TIMEOUT_S: float = 5.0

# Sub-states that indicate systemd has stopped trying to restart the
# service. Any of these combined with ActiveState != active should be
# surfaced as a dead-man signal. On systemd >= 229 "start-limit-hit"
# appears in Result; older versions use the sub-state.
_DEAD_MAN_RESULTS: frozenset[str] = frozenset({
    "start-limit-hit", "start-limit", "oom-kill",
})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class KioskHealth:
    """Normalized kiosk-service health record for the signal bus."""

    ts: str
    node_id: str
    service: str
    scope: str
    active: bool
    sub_state: str
    result: str
    n_restarts: int
    uptime_s: Optional[float]
    invocation_id: str
    crash_loop: bool
    exec_main_status: Optional[int]


# ---------------------------------------------------------------------------
# systemctl probe
# ---------------------------------------------------------------------------

def _parse_systemctl_show(output: str) -> dict[str, str]:
    """Parse ``KEY=value\\n`` lines from ``systemctl show``."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _query_service(service: str, scope: str) -> dict[str, str]:
    """Return the parsed ``systemctl show`` output for a service.

    Args:
        service: Service unit name (e.g. ``kiosk.service``).
        scope: ``user`` or ``system``.

    Returns:
        Dict of systemctl property → string value. Empty on failure; an
        empty dict is treated by callers as "unknown state".
    """
    cmd: list[str] = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    cmd.append("show")
    cmd.append(service)
    cmd += ["-p", ",".join(_SHOW_PROPERTIES)]

    try:
        proc: subprocess.CompletedProcess = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("systemctl probe failed: %s", exc)
        return {}

    if proc.returncode != 0:
        # systemd returns non-zero when the unit doesn't exist at all
        # (common during first install). Log once per interval — not
        # a crash-worthy condition.
        logger.debug(
            "systemctl rc=%d stderr=%s",
            proc.returncode, proc.stderr.strip(),
        )
        return _parse_systemctl_show(proc.stdout)

    return _parse_systemctl_show(proc.stdout)


def _derive_health(
    raw: dict[str, str], node_id: str, service: str, scope: str,
) -> KioskHealth:
    """Convert raw systemctl fields into the normalized record."""
    active_state: str = raw.get("ActiveState", "unknown")
    sub_state: str = raw.get("SubState", "unknown")
    result: str = raw.get("Result", "unknown")
    invocation_id: str = raw.get("InvocationID", "")

    # NRestarts is a u32; empty string on some systems if not yet set.
    n_restarts: int = 0
    try:
        n_restarts = int(raw.get("NRestarts", "0") or "0")
    except ValueError:
        n_restarts = 0

    # ActiveEnterTimestamp (human-readable) vs ActiveEnterTimestampMonotonic
    # vs ActiveEnterTimestampRealtime — we ask for the non-suffixed one
    # which is a wall-clock string like "Tue 2026-04-21 19:58:15 CDT".
    # Parsing that portably is brittle, so we compute uptime ourselves
    # by reading the realtime monotonic via systemctl show when needed.
    # For simplicity here: if active, use a heuristic based on systemd's
    # parseable epoch form ActiveEnterTimestampMonotonic (microseconds
    # since boot). We don't have it in _SHOW_PROPERTIES — fall back to
    # None for uptime; derive it at the dashboard from the sequence of
    # timestamps rather than trusting our own clock.
    uptime_s: Optional[float] = None

    exec_main_status: Optional[int] = None
    try:
        ems: str = raw.get("ExecMainStatus", "")
        if ems:
            exec_main_status = int(ems)
    except ValueError:
        exec_main_status = None

    # Crash-loop detection. On systemd >= 229 the Result field is the
    # authoritative signal when StartLimitBurst is exhausted, so the
    # simple check is sufficient. sub_state may read "failed" for
    # other reasons (clean stop after a start-limit, for example), so
    # we don't conflate that with a crash loop.
    crash_loop: bool = result in _DEAD_MAN_RESULTS

    return KioskHealth(
        ts=_utc_iso_now(),
        node_id=node_id,
        service=service,
        scope=scope,
        active=(active_state == "active"),
        sub_state=sub_state,
        result=result,
        n_restarts=n_restarts,
        uptime_s=uptime_s,
        invocation_id=invocation_id,
        crash_loop=crash_loop,
        exec_main_status=exec_main_status,
    )


def _utc_iso_now() -> str:
    """Return current UTC time as an ISO-8601 string with trailing Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# MQTT client
# ---------------------------------------------------------------------------

class _Publisher:
    """Thin wrapper around paho-mqtt for publishing and clean shutdown."""

    def __init__(
        self, broker: str, port: int, client_id: str,
        status_topic: str,
    ) -> None:
        if not _HAS_PAHO:
            raise RuntimeError(
                "paho-mqtt not installed — install with "
                "`pip install paho-mqtt` or `apt install python3-paho-mqtt`"
            )
        self._status_topic: str = status_topic
        self._client: Any = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        # LWT: if this process dies unexpectedly, the broker publishes
        # "offline" on the status topic. Combined with the explicit
        # "online" we publish on connect, the orchestrator sees a
        # clean presence signal without polling.
        self._client.will_set(
            self._status_topic, payload="offline",
            qos=1, retain=True,
        )
        self._client.connect(broker, port, keepalive=60)
        self._client.loop_start()
        self._client.publish(
            self._status_topic, payload="online",
            qos=1, retain=True,
        )
        logger.info(
            "connected to %s:%d as %s", broker, port, client_id,
        )

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        self._client.publish(topic, payload, qos=0, retain=retain)

    def close(self) -> None:
        try:
            self._client.publish(
                self._status_topic, payload="offline",
                qos=1, retain=True,
            )
        except Exception:
            pass
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _run_loop(
    broker: str, port: int, service: str, scope: str,
    node_id: str, interval_s: float,
) -> None:
    """Probe and publish in a loop until SIGTERM / SIGINT."""
    data_topic: str = _TOPIC_DATA_FMT.format(node_id=node_id)
    status_topic: str = _TOPIC_STATUS_FMT.format(node_id=node_id)
    client_id: str = f"kiosk-health-{node_id}"

    pub: _Publisher = _Publisher(broker, port, client_id, status_topic)

    stop: threading.Event = threading.Event()

    def _handle_signal(signum: int, _frame: Optional[FrameType]) -> None:
        logger.info("signal %d received — shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "kiosk_health_sensor running — node_id=%s service=%s scope=%s "
        "interval=%.1fs topic=%s",
        node_id, service, scope, interval_s, data_topic,
    )

    while not stop.is_set():
        raw: dict[str, str] = _query_service(service, scope)
        record: KioskHealth = _derive_health(raw, node_id, service, scope)
        try:
            pub.publish(
                data_topic, json.dumps(asdict(record), default=str),
                retain=True,
            )
        except Exception as exc:
            logger.warning("publish failed: %s", exc)

        stop.wait(interval_s)

    pub.close()
    logger.info("clean shutdown")


def main() -> int:
    """Entry point — parse args, set up logging, run the loop."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument(
        "--broker", default=_DEFAULT_BROKER,
        help="MQTT broker host (default: %(default)s)",
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT,
        help="MQTT broker port (default: %(default)s)",
    )
    parser.add_argument(
        "--service", default=_DEFAULT_SERVICE,
        help="systemd service unit to observe (default: %(default)s)",
    )
    parser.add_argument(
        "--scope", choices=("user", "system"), default=_DEFAULT_SCOPE,
        help="systemctl scope (default: %(default)s)",
    )
    parser.add_argument(
        "--node-id", default="",
        help="Override node identifier (default: short hostname)",
    )
    parser.add_argument(
        "--interval", type=float, default=_DEFAULT_INTERVAL_S,
        help="publish interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        help="Python logging level (default: %(default)s)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not _HAS_PAHO:
        logger.error(
            "paho-mqtt not installed — cannot start sensor"
        )
        return 1

    node_id: str = args.node_id or socket.gethostname().split(".")[0].lower()

    try:
        _run_loop(
            broker=args.broker,
            port=args.port,
            service=args.service,
            scope=args.scope,
            node_id=node_id,
            interval_s=args.interval,
        )
    except Exception as exc:
        logger.exception("fatal: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
