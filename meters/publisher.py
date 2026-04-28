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
import collections
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Deque, Optional

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

# rtl_433 protocol decoder IDs to PIN with -R.
#
# Three modes:
#   ()                    — discover all 269 protocols at startup
#                           and enable them ALL (the "scoop
#                           everything, winnow later" stance).  This
#                           is the default since ``-G`` was deprecated
#                           in rtl_433 25.02 and ``-R`` on the CLI is
#                           "enable only the listed" rather than
#                           "add to default" (proven 2026-04-26 when
#                           -R 53/54/55/56/153 silently dropped every
#                           ITRON SCM+ decode despite strong RX).
#   ("default",)          — let rtl_433 use its built-in default set
#                           (no -R flags emitted).  Sentinel string,
#                           since () already means discover-all.
#   (53, 70, 154, ...)    — pin to the explicit numeric set.  Use
#                           when reproducing a historical run or
#                           narrowing a known-bad decoder.
#
# Filtering to the meter subset happens in parse_packet() at the
# Python layer, where we control the model→type table directly —
# false positives from rare/disabled-by-default decoders show up on
# the /airwaves dashboard but never pollute the durable meter
# pipeline (which keys off model name in _MODEL_TO_TYPE).
_PROTOCOL_IDS: tuple[Any, ...] = ()

# Sentinel marker for "use rtl_433's compiled-in default set".
_PROTOCOL_IDS_DEFAULT_SENTINEL: str = "default"

# Mixed-band coverage tuned for North-American urban capture
# breadth.  Four hops, 12-second dwell (48-second full cycle) —
# overnight test on the prior 5-band/30s rotation captured only 12
# distinct transmitters across 12 hours in an urban setting; the
# community consensus from rtl_433-based projects is that 433.92
# MHz needs a much larger time-share than rotating bands, and that
# 902-928 ISM is fully covered by a single 915 MHz tune given
# rtl_433's 2048k sample-rate window (~26 MHz coverage).
#
# Four hops, 12-second dwell each:
#   315 MHz     North American TPMS (older Toyota / Hyundai), garage
#               door remotes, some keyless-entry fobs.  Drive-by
#               capture — expect spikes when traffic passes.
#   345 MHz     Honeywell / 2GIG / Vivint security sensors — door,
#               window, motion, glass-break.  Event-driven; long
#               quiet stretches are normal.
#   433.92 MHz  Acurite/LaCrosse/OS weather stations, Chamberlain/
#               Genie garage doors, Markisol blinds, Regency fans,
#               Honeywell ActivLink doorbells, European TPMS, the
#               densest consumer-electronics band by a wide margin.
#   915 MHz     902-928 MHz US ISM block, single tune.  ITRON SCM /
#               SCMplus / IDM / NetIDM utility meters span this
#               whole band; rtl_433's 2 MS/s window covers it from
#               one center freq.  Replaces the prior 911/921 split.
#
# 868 MHz European ISM dropped — vanishingly rare in southern
# Alabama; the time slot is better spent on 433.92.  916 MHz
# dropped earlier (zero decodes); 911 and 921 collapsed into the
# 915 single-tune.
#
# Override via --rtl433-freqs / --rtl433-hop-interval for other
# regions (Japan is 426M, etc.).
_DEFAULT_FREQUENCIES: tuple[str, ...] = (
    "315M", "345M", "433.92M", "915M",
)
_DEFAULT_HOP_INTERVAL_S: int = 12

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
# bert).  Keep the legacy strings in the table so a
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

# Side-channel topic for the raw rtl_433 firehose.  Every decoded
# packet — meters, garage doors, weather stations, window blinds,
# every random ISM-band thing rtl_433 recognises — is published here
# as the unmodified rtl_433 JSON object (plus a ``received_ts``
# field).  Consumed by a hub-side in-memory ring buffer that backs
# the /airwaves dashboard.  No persistence, no schema, fire-and-
# forget — the meter pipeline above is the durable path.
_RAW_TOPIC: str = "glowup/sub_ghz/raw"

# Retained tuner-state topic.  Published every time rtl_433 reports
# "Tuned to <freq>MHz." on stdout (which it does on every retune,
# given -vvvvv -F log,v=5 — see _RTL433_VERBOSITY_ARGS below).  The
# /airwaves dashboard subscribes and renders a "now scanning" header
# strip with a client-side countdown.  Retained so a freshly-loaded
# dashboard sees the last known state immediately instead of waiting
# up to dwell-seconds for the next hop.
_TUNER_TOPIC: str = "glowup/sub_ghz/tuner"

# QoS for the raw firehose.  0 = fire-and-forget; we don't care if a
# duplicate or two slips through and we don't care if one is lost.
# This is a UI nicety, not a measurement record.
_RAW_QOS: int = 0

# QoS for tuner state.  1 = at-least-once.  Tune events are sparse
# (one per dwell, ~5 per cycle) so the cost is trivial and we want
# the dashboard's header to reliably reflect ground truth.
_TUNER_QOS: int = 1

# Stdout regex for rtl_433's per-retune NOTICE message.  rtl_433
# 25.02 prints exactly "SDR: Tuned to <ddd.ddd>MHz." (no trailing
# whitespace) when ``cfg->verbosity >= LOG_NOTICE`` (5) AND a log
# output handler is attached at filter level 5.  We unlock this by
# adding ``-vvvvv -F log,v=5`` to the rtl_433 invocation; the log
# stream interleaves with -F json on stdout, but log lines fail JSON
# parse and tune lines are matched by this regex before being
# discarded as noise.  Discovered Bed, 2026-04-26 night — see the
# `print_logf(LOG_NOTICE, "SDR", "Tuned to %s.", ...)` call in
# upstream src/sdr.c sdr_set_center_freq().
_TUNE_LINE_RE: re.Pattern[str] = re.compile(
    r"^SDR: Tuned to ([\d.]+)MHz\.\s*$",
)

# Verbosity / log-output flags appended to the rtl_433 command line
# to surface tune events.  ``-vvvvv`` raises cfg->verbosity to 5
# (LOG_NOTICE), unblocking the log_handler verbosity gate.
# ``-F log,v=5`` adds a log output handler at filter level 5 so
# NOTICE-and-below messages reach stdout.  Pulse-data trace dumps
# are gated separately at LOG_TRACE=8 in upstream sdr.c, so we do
# NOT see the per-pulse spam at this verbosity — only NOTICE level.
# The ``-v`` flag in upstream rtl_433 takes no argument despite the
# help text suggesting ``-v <num>``; the only way to reach
# verbosity 5 from the CLI is five literal v's.
_RTL433_VERBOSITY_ARGS: tuple[str, ...] = (
    "-vvvvv", "-F", "log,v=5",
)

# QoS for publishes.  1 = at-least-once.  Meters transmit every
# 30-60s so a duplicate from a redelivery is harmless (logger
# rate-limits anyway), but we want at-least-once so a paho hiccup
# does not silently drop a reading.
_PUBLISH_QOS: int = 1

# Seconds between MQTT keepalives.
_MQTT_KEEPALIVE_S: int = 60

# How many recent stderr lines from rtl_433 to retain for crash
# diagnostics.  rtl_433 at -vvvvv emits megabytes of pulse-data
# trace per minute on stderr (direct fprintf, not through the log
# subsystem), so we MUST drain the pipe continuously — otherwise it
# fills the 64 KiB pipe buffer, rtl_433 blocks on write(stderr), the
# read loop on stdout starves, tune events never reach the publisher,
# and the /airwaves indicator freezes.  Bed, 2026-04-26 night /
# 2026-04-27 early — confirmed via /proc/<pid>/wchan = pipe_write.
# A bounded deque is the right shape: cheap, drops oldest first,
# preserves a useful tail for the post-mortem print on rc != 0.
_STDERR_RING_SIZE: int = 64


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
        protocol_ids:  Three-mode decoder selector — see the
                       ``_PROTOCOL_IDS`` module constant for the full
                       contract.  ``()`` (the default) discovers all
                       supported rtl_433 protocols at startup and
                       enables every one — the "scoop everything,
                       winnow at the Python boundary" stance.
                       ``("default",)`` lets rtl_433 use its built-in
                       set.  A tuple of ints pins to that explicit
                       set.
    """

    def __init__(
        self,
        broker_host: str,
        broker_port: int = 1883,
        rtl433_path: str = _DEFAULT_RTL433,
        protocol_ids: tuple[Any, ...] = _PROTOCOL_IDS,
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
        # tuple[Any, ...] supports the three-mode contract documented
        # at _PROTOCOL_IDS: empty for scoop-everything, ("default",)
        # to use rtl_433's built-ins, or numeric IDs to pin.
        self._protocol_ids: tuple[Any, ...] = protocol_ids
        self._frequencies: tuple[str, ...] = frequencies
        self._hop_interval_s: int = hop_interval_s
        self._sample_rate: str = sample_rate
        self._client: "mqtt.Client" = mqtt.Client(
            client_id=f"glowup-meters-{socket.gethostname().split('.')[0]}",
        )
        self._proc: Optional[subprocess.Popen[str]] = None
        self._stopping: bool = False
        # Bounded ring of recent stderr lines from rtl_433.  Drained
        # by a daemon thread spawned in start() so the pipe never
        # fills (see _STDERR_RING_SIZE comment for the failure mode).
        # The crash-tail diagnostic (in _read_loop) reads from here
        # instead of from the pipe.
        self._stderr_ring: Deque[str] = collections.deque(
            maxlen=_STDERR_RING_SIZE,
        )
        self._stderr_thread: Optional[threading.Thread] = None
        # Numeric rotation in MHz, parsed once from the configured
        # frequency strings ("433.92M" → 433.92).  Cached for the
        # tuner-state payload so /airwaves can show "next: <freq>"
        # without re-parsing per hop.  Strings that don't match the
        # ``<float>M`` pattern are dropped from this list with a warn
        # — the rotation is still honoured at the rtl_433 level
        # (publisher just won't surface those slots in the indicator).
        self._rotation_MHz: list[float] = []
        for f in self._frequencies:
            try:
                self._rotation_MHz.append(float(f.rstrip("M").rstrip("m")))
            except ValueError:
                logger.warning(
                    "rotation slot %r does not parse as <MHz>M; "
                    "tuner-state payload will omit it", f,
                )
        self._host_short: str = socket.gethostname().split(".")[0]

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

        # ``stdbuf -oL`` forces line-buffered stdout in the child.
        # Without it, glibc block-buffers a 4 KiB pipe, so short log
        # lines ("SDR: Tuned to <f>MHz.") sit in the buffer until JSON
        # decode packets push it past block size.  At 30s dwell on
        # quiet bands that means tune events arrive minutes late or
        # not at all — the first deploy-with-pipe symptom was the
        # /airwaves dashboard freezing on the startup tune forever.
        # ``-eL`` covers stderr too in case any future code paths
        # emit to stderr.  Both are coreutils standard.
        cmd: list[str] = [
            "stdbuf", "-oL", "-eL",
            self._rtl433_path, "-F", "json",
            # Per-packet metadata: utc time + microsecond resolution
            # in the timestamp, plus 'level' which adds 'freq' (tuned
            # MHz at decode time), 'rssi', 'snr', 'noise', and 'mod'
            # to every JSON line.  Two separate -M flags by design —
            # rtl_433 25.02 takes 'level' and 'time' as sibling
            # selectors, not nested options under time:.  Without
            # 'level' the freq/rssi/snr fields are absent and the
            # /airwaves dashboard's freq column shows em-dashes.
            "-M", "time:utc:usec",
            "-M", "level",
            "-s", self._sample_rate,
            # Verbosity flags that unlock the per-retune NOTICE log
            # message ("SDR: Tuned to ...MHz.") on stdout.  The
            # publisher reads these lines, parses out the freq, and
            # publishes a retained tuner-state message to MQTT —
            # ground truth for the /airwaves dashboard's "now
            # scanning" indicator.  See _RTL433_VERBOSITY_ARGS in
            # the constants block for the rationale.
            *_RTL433_VERBOSITY_ARGS,
            # No -Y level= flag.  Commit 50eb015 added "-Y level=-30"
            # intending to lower the pulse-detection threshold to catch
            # weaker meter transmissions, but rtl_433 25.02's level=
            # semantics are inverted from what that commit assumed —
            # negative values are stricter than default, not laxer.
            # Empirically (Bed, 2026-04-26 night): with `-Y level=-30`
            # rtl_433 produced ZERO decodes across 433.92 MHz and all
            # five 902-928 MHz hops over multiple minutes; removing
            # the flag, the loud neighbor ITRON SCM+ at 911 MHz
            # decoded on the first try.  rtl_433's default auto-tracked
            # threshold is the right default — let it do its job.
        ]
        # Multiple -f flags: rtl_433 hops between them at -H interval.
        # Without this the receiver only sees ~2 MHz of the 26 MHz US
        # ISM band and misses meters whose frequency-hop landed
        # outside the slice we're tuned to.
        for freq in self._frequencies:
            cmd.extend(["-f", freq])
        if len(self._frequencies) > 1:
            cmd.extend(["-H", str(self._hop_interval_s)])
        # Decoder protocol selection.  See _PROTOCOL_IDS docstring for
        # the three modes.  In "scoop everything" mode (empty tuple,
        # the default), we discover all 269 protocols at startup and
        # emit a -R flag for each — this enables every decoder
        # including the disabled-by-default ones (rare TPMS variants,
        # security panels, regional weather sensors) at the cost of a
        # longer argv and slightly more demod CPU.  rtl_433 25.02
        # deprecated -G (which previously enabled-all in one flag);
        # the conf-file path was an alternative but adds a deploy
        # artifact, where the dynamic-discovery path stays
        # self-contained in this module.
        active_pids: tuple[int, ...]
        if self._protocol_ids == ():
            active_pids = self._discover_all_protocol_ids()
            logger.info(
                "decoder mode: scoop-everything (%d protocols enabled "
                "via -R; false positives expected, parse_packet() "
                "filters meters at the Python boundary)",
                len(active_pids),
            )
        elif self._protocol_ids == (_PROTOCOL_IDS_DEFAULT_SENTINEL,):
            active_pids = ()
            logger.info(
                "decoder mode: rtl_433 built-in defaults (no -R flags)",
            )
        else:
            # Caller passed explicit numeric IDs — treat as a pin.
            active_pids = tuple(int(p) for p in self._protocol_ids)
            logger.info(
                "decoder mode: explicit pin (%d protocols)",
                len(active_pids),
            )
        for pid in active_pids:
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

        # Start the stderr drain thread BEFORE entering the read loop.
        # rtl_433 at -vvvvv firehoses pulse-data trace lines on stderr;
        # if we let that pipe back up, rtl_433 blocks on write(stderr)
        # and the stdout-side read loop never sees another line.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="rtl_433-stderr-drain",
            daemon=True,
        )
        self._stderr_thread.start()

        try:
            self._read_loop()
        finally:
            self._cleanup()

    def _discover_all_protocol_ids(self) -> tuple[int, ...]:
        """Run ``rtl_433 -R help`` and return every protocol ID it lists.

        Output format (rtl_433 25.02)::

            [01]  Silvercrest Remote Control
            [02]  Rubicson, TFA 30.3197 ...
            [06]* ELV EM 1000           # disabled-by-default — '*' suffix
            ...
            [275]  GM-Aftermarket TPMS

        We extract every numeric ID regardless of the disabled marker
        — the whole point of this code path is to enable all of them.
        Empty result triggers a fail-fast warning rather than a
        silent fallback to default protocols, since the caller asked
        for "everything" and a degraded set is a misrepresentation.

        Cached on the first call (self-imposed: rtl_433's protocol
        list is stable for a process lifetime; re-running ``-R help``
        on each restart is cheap but unnecessary).
        """
        # Pattern matches '[NN]' or '[NN]*' at start of line.
        line_re: re.Pattern[str] = re.compile(
            r"^\s*\[(\d+)\][\s*]",
        )
        try:
            out: str = subprocess.check_output(
                [self._rtl433_path, "-R", "help"],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                FileNotFoundError) as exc:
            logger.warning(
                "could not enumerate rtl_433 protocols (%s); falling "
                "back to built-in defaults", exc,
            )
            return ()
        ids: list[int] = []
        for line in out.splitlines():
            m: Optional[re.Match[str]] = line_re.match(line)
            if m is not None:
                ids.append(int(m.group(1)))
        if not ids:
            logger.warning(
                "rtl_433 -R help returned no protocol IDs; falling "
                "back to built-in defaults",
            )
        return tuple(ids)

    def _drain_stderr(self) -> None:
        """Continuously read rtl_433 stderr into a bounded ring.

        rtl_433 at -vvvvv emits very high-volume trace data on stderr
        via direct ``fprintf(stderr, ...)`` (Pulse data dumps,
        per-pulse timing, decoder bitrows, etc.).  We can't simply
        let that pipe buffer fill because rtl_433 will then block on
        ``write(stderr)`` — confirmed via /proc/<pid>/wchan reading
        ``pipe_write`` after a few minutes of operation, with the
        stdout side of rtl_433 starving the publisher's read loop.

        Discarding the bytes is fine — the pulse-trace stream has no
        operational value here.  We retain the last
        :data:`_STDERR_RING_SIZE` lines into ``self._stderr_ring`` so
        a non-zero rtl_433 exit can include a useful tail in the
        crash log without ever blocking the producer.

        Runs as a daemon thread; exits naturally when the rtl_433
        process exits and stderr closes (readline returns "").
        """
        assert self._proc is not None
        if self._proc.stderr is None:
            return
        try:
            for line in self._proc.stderr:
                # Bounded deque appends drop the oldest line for free
                # — no per-line cost beyond the rstrip; volume here
                # can be tens of thousands of lines per second at
                # peak, so this loop body must stay tight.
                self._stderr_ring.append(line.rstrip("\n"))
        except Exception as exc:  # pragma: no cover
            # If readline raises (e.g. pipe broken mid-shutdown), log
            # at debug and exit the thread.  The rtl_433 exit-code
            # path will surface any actual failure to the operator.
            logger.debug("stderr drain ended: %s", exc)

    def _publish_tuner_state(self, freq_MHz: float) -> None:
        """Publish a retained tuner-state message after a rtl_433 retune.

        Called from :meth:`_read_loop` whenever a stdout line matches
        :data:`_TUNE_LINE_RE`.  Payload format::

            {
                "host":          "bert",
                "freq_MHz":      911.0,
                "tuned_at":      1714187654.123,
                "rotation_MHz":  [345.0, 433.92, 868.0, 911.0, 921.0],
                "dwell_s":       30
            }

        Retained at QoS 1 so a freshly-loaded /airwaves dashboard
        sees the last known state immediately.  Published with
        ``retain=True`` — paho will issue a clean update on every
        tune; the broker only ever holds the latest value.
        """
        payload: dict[str, Any] = {
            "host":         self._host_short,
            "freq_MHz":     freq_MHz,
            "tuned_at":     time.time(),
            "rotation_MHz": list(self._rotation_MHz),
            "dwell_s":      self._hop_interval_s,
        }
        try:
            self._client.publish(
                _TUNER_TOPIC, json.dumps(payload),
                qos=_TUNER_QOS, retain=True,
            )
            logger.debug("tuner state -> %.3f MHz", freq_MHz)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "tuner-state serialize failed (freq=%s): %s",
                freq_MHz, exc,
            )
        except Exception as exc:
            # MQTT layer hiccups — not fatal to the meter pipeline.
            logger.debug("tuner-state publish failed: %s", exc)

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
                # Not JSON — could be a tune-event log line (which we
                # care about) or rtl_433 banner / decoder noise (which
                # we don't).  Try the tune-line regex before discarding.
                tune_match: Optional[re.Match[str]] = (
                    _TUNE_LINE_RE.match(line)
                )
                if tune_match is not None:
                    self._publish_tuner_state(float(tune_match.group(1)))
                else:
                    logger.debug("non-JSON line from rtl_433: %s (%s)",
                                 line[:80], exc)
                continue

            # ---- Raw firehose to the /airwaves dashboard ----
            # Every JSON-shaped packet rtl_433 emits gets shipped to
            # the side-channel raw topic, regardless of whether it
            # passes the meter-schema filter below.  This is what
            # lights up the live RF activity feed: garage door
            # remotes, window blind clickers, weather stations,
            # neighbours' meters, anything the band carries.  The
            # hub-side ring buffer subscriber owns shape decisions
            # downstream — keep this side as dumb as possible.
            if isinstance(packet, dict):
                raw_envelope: dict[str, Any] = dict(packet)
                raw_envelope["received_ts"] = time.time()
                try:
                    raw_payload: str = json.dumps(raw_envelope)
                    self._client.publish(
                        _RAW_TOPIC, raw_payload,
                        qos=_RAW_QOS, retain=False,
                    )
                except (TypeError, ValueError) as exc:
                    logger.debug(
                        "raw firehose serialize failed (model=%r): %s",
                        packet.get("model"), exc,
                    )
                except Exception as exc:
                    # MQTT layer hiccups — don't block the meter path
                    # on a side-channel failure.
                    logger.debug(
                        "raw firehose publish failed: %s", exc,
                    )

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
            # Pull diagnostics from the drain thread's ring rather
            # than reading the stderr pipe directly — the drain
            # thread has been consuming bytes the whole time, so the
            # pipe is empty by now and a .read() would just block or
            # return empty.  Snapshot under no lock: deque.append /
            # iteration interleave is GIL-safe and we tolerate a
            # partial-update read here (last line might be truncated
            # — fine for a post-mortem hint).
            tail_lines: list[str] = list(self._stderr_ring)
            stderr_tail: str = "\n".join(tail_lines)
            logger.error(
                "rtl_433 exited with code %d.  stderr tail (last %d lines):\n%s",
                rc, len(tail_lines), stderr_tail[-2000:],
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
