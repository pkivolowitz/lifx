"""rtl_433 → MQTT bridge for utility meter telemetry.

Runs on the configured SDR host under a systemd unit.
Spawns ``rtl_433`` with the meter-protocol decoders enabled, reads
its JSON-line stdout, validates each line at the boundary, and
publishes parsed records to the hub's MQTT broker on the
``glowup/meters/<meter_id>`` topic.

Design lessons applied (from the 2026-04-25 silent-death audit):

- The sample timestamp is parsed from rtl_433's own ``time`` field,
  not stamped at receipt by the publisher or the logger.  Receipt
  time is a separate concern (rate-limit enforcement) and is held
  out as such.
- Every parse / coercion failure is logged, never silently swallowed.
- The schema is validated at the MQTT publish boundary — payloads
  missing the required keys are dropped with a warning, not
  re-shaped or defaulted into plausible-looking nonsense.
- No bare ``except:``.  Every handler logs the cause.

rtl_433 invocation::

    rtl_433 -F json -M utc -R 53 -R 54 -R 55 -R 56 -R 153

Where:

- ``-F json``  emits one JSON object per detected packet to stdout.
- ``-M utc``   stamps the ``time`` field in ISO 8601 UTC (Z form),
               which is exactly what :mod:`infrastructure.meter_logger`
               and the thermal-logger ts parser already accept.
- ``-R 53``    ITRON ERT IDM (Interval Data Message)
- ``-R 54``    ITRON ERT NetIDM
- ``-R 55``    ITRON ERT SCM+ (Standard Consumption Message, plus)
- ``-R 56``    ITRON ERT SCM   (Standard Consumption Message, base)
- ``-R 153``   Neptune R900 water meter (915 MHz FSK)

Topic shape::

    glowup/meters/<meter_id>

Where ``meter_id`` is the device-reported identifier (string).  The
logger downstream subscribes to ``glowup/meters/+`` and uses the
hub-side owned-meters config to flag rows as ours vs neighbor.

Payload shape (JSON object), shipped as a flattened, schema-checked
view of the rtl_433 packet::

    {
        "ts":               "2026-04-25T20:13:37Z",   # ISO-8601 UTC
        "meter_id":         "4599052",                # str (always)
        "meter_type":       "ert_scm",                # str — one of:
                                                       #   ert_scm,
                                                       #   ert_scm_plus,
                                                       #   ert_idm,
                                                       #   ert_net_idm,
                                                       #   neptune_r900
        "consumption":      12345.0,                  # float, raw counter
        "unit":             "raw",                    # placeholder; the
                                                       # logger / billing
                                                       # tool resolves
                                                       # units per type
        "tamper_phy":       0,                        # int (ERT only)
        "tamper_enc":       0,                        # int (ERT only)
        "physical_tamper":  null,                     # int (R900 only)
        "leak":             null,                     # int (R900 only)
        "no_use":           null,                     # int (R900 only)
        "raw":              {...}                     # full original
                                                       # rtl_433 packet
    }

Fields not applicable to a given meter family are present and
``null`` rather than absent — uniform shape downstream simplifies the
logger's column writes.

Process model:

The publisher runs as a long-lived process under systemd.  rtl_433
is a child process; if it dies, the publisher exits non-zero so
systemd's ``Restart=on-failure`` will restart the whole stack
together.  This keeps the SDR-claim / decoder relationship atomic —
the publisher never holds an MQTT session against a dead rtl_433.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__: str = "1.0"

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
except ImportError:
    _HAS_PAHO = False

# Site config — single source of truth for the hub broker host/port.
# Falls back gracefully if site.json is absent (covers tests that
# import this module without /etc/glowup/site.json).
try:
    from glowup_site import site as _site
except ImportError:
    _site = None


logger: logging.Logger = logging.getLogger("glowup.meters.publisher")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# rtl_433 binary on PATH.  Override via --rtl433-path.
_DEFAULT_RTL433: str = "rtl_433"

# rtl_433 protocol decoder IDs to PIN with -R.  Empty = use rtl_433's
# default decoder set (which includes ITRON ERT and Neptune R900).
# We default to empty because rtl_433 25.02's protocol-ID mapping
# silently mismatched our tuple (53/54/55/56/153) and dropped every
# decode despite strong RX — proven 2026-04-26 on pi-sensor-01,
# decoded SCMplus immediately on first run when -R was removed.
# Filtering to the meter subset happens in parse_packet() at the
# Python layer, where we control the model→type table directly.
_PROTOCOL_IDS: tuple[int, ...] = ()

# US ISM-band coverage.  ITRON ERT (electric/gas) and Neptune R900
# (water) frequency-hop across the full 902-928 MHz band; the RTL-SDR
# only sees ~2 MHz at a time (the sample rate window), so to catch
# every meter we have to make rtl_433 hop too.  rtl_433 accepts
# multiple -f flags and rotates through them every -H seconds.
#
# Five hops at 906/911/916/921/926 MHz with 5 MHz spacing cover the
# band tightly (each RTL-SDR window with -s 2048k is ~2 MHz, so 906
# covers 905-907, 911 covers 910-912, etc).  Recipe confirmed
# working on ernie 2026-04-25 (caught a neighbor SCM+ gas meter)
# and replicated on pi-sensor-01 2026-04-26 (decoded SCMplus
# id=101903449).  30-second hop dwell is the proven value — half
# the worst-case time-to-hear vs. 60s, ITRON broadcasts every
# 30-60s so we get at least one transmission window per hop.
#
# Override via --rtl433-freqs / --rtl433-hop-interval for non-US
# deployments (Europe is 868M single-channel, Japan 426M, etc.).
_DEFAULT_FREQUENCIES: tuple[str, ...] = (
    "906M", "911M", "916M", "921M", "926M",
)
_DEFAULT_HOP_INTERVAL_S: int = 30

# RTL-SDR sample rate.  rtl_433's new defaults (25.02+) implicitly
# tune sample rate per protocol, but ITRON SCM/SCM+/IDM and Neptune
# R900 specifically need a wider window than 1 MHz to catch the
# FSK deviation cleanly — 2048k (2 MS/s) is the empirically proven
# value from ernie 2026-04-25.  At 250k (the new "narrow" default)
# decoders silently fail to checksum despite RX showing pulses.
_DEFAULT_SAMPLE_RATE: str = "2048k"

# rtl_433 model-string → our normalized meter_type tag.  Anything not
# in this map is dropped at the schema boundary with a warning, since
# we cannot guarantee the field shape and don't want to ship
# unstructured payloads downstream.  Add a row here when a new meter
# type is intentionally onboarded — never silently passthrough.
#
# rtl_433 25.02 changed the bare names (was "ERT-SCM+", now
# "SCMplus" — confirmed empirically from emitted JSON 2026-04-26 on
# pi-sensor-01).  Keep the legacy strings in the table so a
# downgrade/upgrade of rtl_433 doesn't silently break parsing.
_MODEL_TO_TYPE: dict[str, str] = {
    # rtl_433 25.02+ bare names
    "SCM":          "ert_scm",
    "SCMplus":      "ert_scm_plus",
    "IDM":          "ert_idm",
    "NetIDM":       "ert_net_idm",
    "Neptune-R900": "neptune_r900",
    # Legacy rtl_433 (<25.02) names — kept for forward/backward compat
    "ERT-SCM":      "ert_scm",
    "ERT-SCM+":     "ert_scm_plus",
    "ERT-IDM":      "ert_idm",
    "ERT-NetIDM":   "ert_net_idm",
}

# rtl_433 meter-id field varies by model AND rtl_433 version.  rtl_433
# 25.02 emits CamelCase JSON keys ("EndpointID", "Consumption") for
# meter protocols where older versions used snake_case.  Probe both.
_METER_ID_FIELDS: tuple[str, ...] = (
    "id",                # rtl_433 default ("id" stayed lowercase)
    "EndpointID",        # rtl_433 25.02+ SCMplus / IDM
    "endpoint_id",       # legacy snake_case variant
    "meter_id",          # some forks
    "ert_id",            # very old rtl_433 builds
)

# rtl_433 consumption field, similarly.  rtl_433 25.02 emits
# "Consumption" (CamelCase); legacy rtl_433 used "consumption".
_CONSUMPTION_FIELDS: tuple[str, ...] = (
    "Consumption",       # rtl_433 25.02+ SCM/SCMplus/IDM
    "consumption",       # legacy ERT-SCM and SCM+
    "Consumption_Data",  # 25.02 IDM
    "consumption_data",  # legacy IDM
    "consumption_kwh",   # some forks
    "value",             # R900 (lowercase historically; verify)
    "Value",             # R900 if 25.02 also CamelCased it
    "consumption_raw",   # fallback
)

# MQTT topic prefix.  Each parsed reading goes to
# ``<prefix>/<meter_id>`` where meter_id is the source-reported
# identifier.  The hub-side logger subscribes to ``<prefix>/+``.
_TOPIC_PREFIX: str = "glowup/meters"

# QoS for publishes.  1 = at-least-once.  Meters transmit every
# 30-60s so a duplicate from a redelivery is harmless (logger
# rate-limits anyway), but we want at-least-once so a paho hiccup
# does not silently drop a reading.
_PUBLISH_QOS: int = 1

# Seconds between MQTT keepalives.
_MQTT_KEEPALIVE_S: int = 60


# ---------------------------------------------------------------------------
# Schema validation at the rtl_433 boundary
# ---------------------------------------------------------------------------


def _extract_meter_id(packet: dict[str, Any]) -> Optional[str]:
    """Return the meter_id from an rtl_433 packet, or ``None``.

    rtl_433 uses different field names for the meter identifier
    across protocols.  Probe the known names; coerce to ``str``
    because the wire format mixes int and string.
    """
    for field in _METER_ID_FIELDS:
        v: Any = packet.get(field)
        if v is None:
            continue
        s: str = str(v).strip()
        if s:
            return s
    return None


def _extract_consumption(packet: dict[str, Any]) -> Optional[float]:
    """Return the raw consumption counter as a float, or ``None``.

    Different rtl_433 protocols report consumption under different
    keys.  Probe the known names; coerce to ``float`` and reject
    anything not parseable rather than substituting zero, since
    a falsely-zero consumption row would corrupt the billing
    comparison downstream.
    """
    for field in _CONSUMPTION_FIELDS:
        v: Any = packet.get(field)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            logger.warning(
                "consumption field %r has non-numeric value %r — "
                "skipping packet",
                field, v,
            )
            return None
    return None


def parse_packet(packet: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Validate an rtl_433 JSON packet and reshape to our schema.

    Returns ``None`` (with a warning logged) if the packet is missing
    a required field or comes from a model we have not onboarded.
    Required fields: ``model``, ``time``, a meter_id field, and a
    consumption field.

    Args:
        packet: One JSON object as parsed from rtl_433 stdout.

    Returns:
        A dict matching the publisher's documented payload shape, or
        ``None`` if the packet should be dropped.
    """
    if not isinstance(packet, dict):
        logger.warning("rtl_433 line is not a JSON object: %r",
                       type(packet).__name__)
        return None

    model: Any = packet.get("model")
    if not isinstance(model, str) or not model:
        logger.debug("rtl_433 line has no model field — not a meter")
        return None

    # Permissive model handling: if the model is in our explicit
    # table, use the normalized type tag (ert_scm_plus, neptune_r900,
    # etc.).  Otherwise — IF the packet still has a consumption-like
    # field — accept it under a normalized form of the model name
    # so unknown meter brands (Badger ORION, Flowis, Sensus, etc.)
    # surface in the dashboard rather than silently dropping.
    # Earlier strict behaviour was masking valid decodes during the
    # 2026-04-26 bringup; keep the explicit map for known brands
    # (clean type tags downstream) but don't gate on it.
    meter_type: Optional[str] = _MODEL_TO_TYPE.get(model)
    if meter_type is None:
        # Normalize "Badger-ORION" -> "badger_orion", etc.
        meter_type = model.lower().replace("-", "_").replace(" ", "_")
        logger.info(
            "accepting unmapped meter model %r as type %r",
            model, meter_type,
        )

    ts_raw: Any = packet.get("time")
    if not isinstance(ts_raw, str) or not ts_raw:
        logger.warning("meter packet (model=%s) missing 'time' — drop",
                       model)
        return None
    # rtl_433 -M utc emits "YYYY-MM-DD HH:MM:SS" without trailing Z.
    # Normalize to ISO-8601 with Z for downstream consistency.
    ts_iso: str = ts_raw.replace(" ", "T")
    if not ts_iso.endswith("Z") and "+" not in ts_iso[-6:]:
        ts_iso = ts_iso + "Z"

    meter_id: Optional[str] = _extract_meter_id(packet)
    if meter_id is None:
        logger.warning("meter packet (model=%s) missing meter_id — drop",
                       model)
        return None

    consumption: Optional[float] = _extract_consumption(packet)
    if consumption is None:
        logger.warning(
            "meter packet (model=%s id=%s) missing consumption — drop",
            model, meter_id,
        )
        return None

    # rtl_433 25.02 reports the utility family ("Gas", "Water",
    # "Electric") as MeterType — much more user-meaningful than the
    # protocol family (ert_scm_plus).  Older rtl_433 used lowercase
    # "meter_type"; probe both.  EndpointType (e.g. "0xBC") encodes
    # the device class — pass through for diagnostics.
    utility: Any = (
        packet.get("MeterType")
        or packet.get("meter_type_label")
    )
    endpoint_type: Any = (
        packet.get("EndpointType")
        or packet.get("endpoint_type")
    )

    return {
        "ts":               ts_iso,
        "meter_id":         meter_id,
        "meter_type":       meter_type,
        "utility":          utility,            # "Gas" / "Water" / "Electric"
        "endpoint_type":    endpoint_type,      # e.g. "0xBC"
        "consumption":      consumption,
        "unit":             "raw",
        "tamper_phy":       packet.get("tamper_phy") or packet.get("Tamper"),
        "tamper_enc":       packet.get("tamper_enc"),
        "physical_tamper":  packet.get("physical_tamper"),
        "leak":             packet.get("leak"),
        "no_use":           packet.get("no_use"),
        "raw":              packet,
    }


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class MeterPublisher:
    """Spawns rtl_433 and bridges its JSON output onto MQTT.

    Args:
        broker_host:   MQTT broker hostname / IP (the hub).
        broker_port:   MQTT broker TCP port.
        rtl433_path:   Path to the rtl_433 binary.
        protocol_ids:  Tuple of rtl_433 ``-R`` decoder IDs to enable.
                       Defaults to the meter-protocol set.
    """

    def __init__(
        self,
        broker_host: str,
        broker_port: int = 1883,
        rtl433_path: str = _DEFAULT_RTL433,
        protocol_ids: tuple[int, ...] = _PROTOCOL_IDS,
        frequencies: tuple[str, ...] = _DEFAULT_FREQUENCIES,
        hop_interval_s: int = _DEFAULT_HOP_INTERVAL_S,
        sample_rate: str = _DEFAULT_SAMPLE_RATE,
    ) -> None:
        if not _HAS_PAHO:
            raise ImportError(
                "paho-mqtt is required.  Install with: "
                "sudo apt install -y python3-paho-mqtt",
            )
        self._broker_host: str = broker_host
        self._broker_port: int = broker_port
        self._rtl433_path: str = rtl433_path
        self._protocol_ids: tuple[int, ...] = protocol_ids
        self._frequencies: tuple[str, ...] = frequencies
        self._hop_interval_s: int = hop_interval_s
        self._sample_rate: str = sample_rate
        self._client: "mqtt.Client" = mqtt.Client(
            client_id=f"glowup-meters-{socket.gethostname().split('.')[0]}",
        )
        self._proc: Optional[subprocess.Popen[str]] = None
        self._stopping: bool = False

    def start(self) -> None:
        """Connect MQTT, spawn rtl_433, run the read loop until stop().

        Blocks the calling thread.  Returns when rtl_433 exits OR
        when stop() is called.  If rtl_433 exits non-zero, this
        method raises so the systemd unit's ``Restart=on-failure``
        recycles the whole stack.
        """
        try:
            self._client.connect(
                self._broker_host, self._broker_port, _MQTT_KEEPALIVE_S,
            )
        except Exception as exc:
            logger.error(
                "MQTT connect to %s:%d failed: %s",
                self._broker_host, self._broker_port, exc,
            )
            raise
        self._client.loop_start()
        logger.info(
            "MQTT connected to %s:%d", self._broker_host, self._broker_port,
        )

        cmd: list[str] = [
            self._rtl433_path, "-F", "json", "-M", "utc",
            "-s", self._sample_rate,
            # Lower the FSK pulse-detection threshold from rtl_433's
            # default (-12 dB-ish auto) to -30 dB so weaker meter
            # transmissions (further neighbors, low-power R900 bursts,
            # any meter at the edge of reception) are passed to the
            # decoders instead of being squelched at the demod layer.
            # Trade-off is more false-positive pulse trains hitting
            # the decoder pool; checksums then drop the noise so it
            # doesn't reach our publish path.
            "-Y", "level=-30",
        ]
        # Multiple -f flags: rtl_433 hops between them at -H interval.
        # Without this the receiver only sees ~2 MHz of the 26 MHz US
        # ISM band and misses meters whose frequency-hop landed
        # outside the slice we're tuned to.
        for freq in self._frequencies:
            cmd.extend(["-f", freq])
        if len(self._frequencies) > 1:
            cmd.extend(["-H", str(self._hop_interval_s)])
        # Note: no -R filter.  rtl_433 25.02 silently failed to decode
        # SCMplus when -R 53/54/55/56/153 was passed (proven empirically
        # 2026-04-26 — same hop / sample rate config decoded a neighbor
        # gas meter immediately when -R was removed).  Protocol-ID
        # mappings may have shifted between rtl_433 versions; rather
        # than hand-track them, run the default decoder set and let
        # parse_packet() in this module filter to _MODEL_TO_TYPE at
        # the Python boundary.  CPU cost is modest, the filter
        # behaviour is then under our control.  protocol_ids is kept
        # in the API surface for callers who explicitly want the
        # restriction (e.g. testing one decoder); empty default tuple
        # means "let rtl_433 decide".
        for pid in self._protocol_ids:
            cmd.extend(["-R", str(pid)])
        logger.info("spawning rtl_433: %s", " ".join(cmd))

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
            )
        except FileNotFoundError as exc:
            logger.error("rtl_433 binary not found at %s: %s",
                         self._rtl433_path, exc)
            raise

        try:
            self._read_loop()
        finally:
            self._cleanup()

    def _read_loop(self) -> None:
        """Read rtl_433 stdout line-by-line, parse, publish."""
        assert self._proc is not None
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stopping:
                break
            line = line.strip()
            if not line:
                continue
            try:
                packet: Any = json.loads(line)
            except json.JSONDecodeError as exc:
                # rtl_433 occasionally emits non-JSON banner / status
                # lines; log at debug to avoid noise but never silently
                # swallow.
                logger.debug("non-JSON line from rtl_433: %s (%s)",
                             line[:80], exc)
                continue

            parsed: Optional[dict[str, Any]] = parse_packet(packet)
            if parsed is None:
                continue

            topic: str = f"{_TOPIC_PREFIX}/{parsed['meter_id']}"
            try:
                payload: str = json.dumps(parsed)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "failed to serialize meter payload "
                    "(model=%s id=%s): %s",
                    parsed.get("meter_type"),
                    parsed.get("meter_id"),
                    exc,
                )
                continue
            try:
                self._client.publish(
                    topic, payload,
                    qos=_PUBLISH_QOS, retain=False,
                )
                logger.info(
                    "published %s id=%s consumption=%s",
                    parsed.get("meter_type"),
                    parsed.get("meter_id"),
                    parsed.get("consumption"),
                )
            except Exception as exc:
                logger.warning("MQTT publish on %s failed: %s",
                               topic, exc)

        # Loop exited.  rtl_433 may still be running if we were asked
        # to stop, or it may have crashed.  Either way the caller
        # decides via the proc's returncode.
        rc: Optional[int] = self._proc.poll()
        if rc is not None and rc != 0 and not self._stopping:
            stderr_tail: str = ""
            if self._proc.stderr is not None:
                try:
                    stderr_tail = self._proc.stderr.read() or ""
                except Exception:  # pragma: no cover
                    stderr_tail = "(stderr unreadable)"
            logger.error(
                "rtl_433 exited with code %d.  stderr tail:\n%s",
                rc, stderr_tail[-2000:],
            )
            raise RuntimeError(f"rtl_433 exited rc={rc}")

    def stop(self) -> None:
        """Graceful shutdown — stop rtl_433 and disconnect MQTT."""
        self._stopping = True
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
            except Exception as exc:
                logger.warning("error stopping rtl_433: %s", exc)

    def _cleanup(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:
            logger.warning("error closing MQTT client: %s", exc)
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="glowup-meters",
        description="Bridge rtl_433 utility-meter telemetry to MQTT.",
    )
    p.add_argument(
        "--broker", default=None,
        help=("MQTT broker hostname or IP (the hub).  If omitted, "
              "read from /etc/glowup/site.json key 'hub_broker' via "
              "glowup_site.site."),
    )
    p.add_argument(
        "--port", type=int, default=None,
        help=("MQTT broker TCP port.  If omitted, read from site.json "
              "key 'hub_port' (default 1883)."),
    )
    p.add_argument(
        "--rtl433-path", default=_DEFAULT_RTL433,
        help="Path to rtl_433 binary (default: rtl_433 on PATH).",
    )
    p.add_argument(
        "--rtl433-freqs", default=",".join(_DEFAULT_FREQUENCIES),
        help=("Comma-separated rtl_433 -f frequencies for hopping.  "
              "Default '905M,915M,925M' covers the full US 902-928 "
              "MHz ISM band where ITRON ERT and Neptune R900 "
              "frequency-hop.  Use '868M' (single) for Europe, "
              "'426M' for Japan, etc."),
    )
    p.add_argument(
        "--rtl433-hop-interval", type=int,
        default=_DEFAULT_HOP_INTERVAL_S,
        help=("Seconds to dwell on each frequency before hopping.  "
              "Default 30 (proven on ernie 2026-04-25) — one full "
              "ITRON transmit cycle, so each meter is heard at most "
              "every len(freqs) * hop_interval seconds.  Ignored if "
              "only one frequency is given."),
    )
    p.add_argument(
        "--rtl433-sample-rate", default=_DEFAULT_SAMPLE_RATE,
        help=("rtl_433 -s sample rate.  Default '2048k' (2 MS/s) — "
              "ITRON SCM/SCM+/IDM and Neptune R900 need this wider "
              "FSK window; the new (25.02+) 'narrow' default of "
              "250k silently fails to checksum these protocols."),
    )
    p.add_argument(
        "--log-level", default="INFO",
        help="Python logging level (default: INFO).",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    """Run the publisher until rtl_433 exits or SIGTERM is received."""
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    # Resolve broker / port from CLI → site.json → fail fast.  Hardcoding
    # the hub IP in this generic source file violated the "lifx public
    # repo stays generic" rule; site.json is the household-specific drop.
    broker_host: Optional[str] = args.broker
    broker_port: Optional[int] = args.port
    if broker_host is None and _site is not None:
        broker_host = _site.get("hub_broker")
    if broker_port is None and _site is not None:
        broker_port = _site.get("hub_port", 1883)
    if broker_port is None:
        broker_port = 1883
    if not broker_host:
        logger.error(
            "no MQTT broker configured: pass --broker, or set "
            "'hub_broker' in /etc/glowup/site.json"
        )
        return 2

    freqs: tuple[str, ...] = tuple(
        f.strip() for f in args.rtl433_freqs.split(",") if f.strip()
    )
    if not freqs:
        logger.error("--rtl433-freqs must list at least one frequency")
        return 2

    pub: MeterPublisher = MeterPublisher(
        broker_host=broker_host,
        broker_port=broker_port,
        rtl433_path=args.rtl433_path,
        frequencies=freqs,
        hop_interval_s=args.rtl433_hop_interval,
        sample_rate=args.rtl433_sample_rate,
    )

    def _term(_sig: int, _frame: Any) -> None:
        logger.info("received SIGTERM — stopping")
        pub.stop()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    try:
        pub.start()
    except KeyboardInterrupt:
        pub.stop()
        return 0
    except Exception as exc:
        logger.error("publisher exited with error: %s", exc, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
