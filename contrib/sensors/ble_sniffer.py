#!/usr/bin/env python3
"""GlowUp BLE sniffer — advertisement capture with registry-driven policy.

Listens on a raw HCI socket (default hci0) via ``aioblescan`` and
publishes two kinds of MQTT messages:

    glowup/ble/seen/<mac>        retained snapshot (dashboard state)
    glowup/ble/events/<mac>      non-retained event log entries

Every MAC is processed through a per-device state machine that emits
only information-bearing events:

    first_seen       MAC was not previously tracked this session
    payload_changed  mfr_data / tx_power / services hash changed
    moved            smoothed RSSI crossed an exposure-band boundary
    heartbeat        periodic "still here" emitted on heartbeat_interval
    gone             no advert heard within gone_after seconds

Raw RSSI is smoothed (EMA) before band detection so natural receiver
jitter (±3-10 dB) never fires a ``moved`` event.

A registry (``/etc/glowup/ble_registry.json`` by default) maps known
MACs to a label, category, and policy. Policies tell the state
machine which event types to suppress:

    heartbeat_only   Bowflex, Pura, HomePods at rest — chatter devices
                     where only "here / gone" matters.
    sensor_event     ONVIS, weather stations — emit payload_changed
                     immediately; moved is suppressed (stationary).
    track            Keys, deliberately tracked phones — emit every
                     movement event.
    ignore           Noisy neighbor devices — emit nothing.
    default          Unknown devices not in the registry — emit
                     first_seen (stranger!) + payload_changed + gone;
                     heartbeats suppressed to control firehose.

The registry is the BLE analogue of LIFX's ``device_registry.json``.
Empty registry is valid (all devices get ``default`` policy).

Runtime dep: aioblescan, paho-mqtt (both in the ernie venv).

Deploy:
    /opt/ernie/ble_sniffer.py            (this file)
    /etc/glowup/ble_registry.json        (registry, optional)
    /etc/systemd/system/ble-sniffer.service

Restart with SIGHUP is not supported — stop/start via systemctl. The
state machine's per-MAC state is intentionally in-memory: on restart
every known MAC re-emits ``first_seen`` once, which is the correct
behaviour for stranger-detection semantics.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "2.0"

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aioblescan as aiobs
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HCI device index — hci0 on ernie.
_HCI_DEFAULT: int = 0

# MQTT defaults — publish to the local broker; ernie's mosquitto
# bridges glowup/ble/# out to the hub.
_BROKER_DEFAULT: str = "127.0.0.1"
_PORT_DEFAULT: int = 1883
_CLIENT_ID: str = "ernie-ble-sniffer"

# Topic roots. See module docstring for semantics.
_TOPIC_SEEN: str = "glowup/ble/seen"       # retained current state
_TOPIC_EVENT: str = "glowup/ble/events"    # non-retained event log

# Registry on disk. Missing file is non-fatal — everything runs under
# the default policy.
_REGISTRY_DEFAULT: str = "/etc/glowup/ble_registry.json"

# EMA smoothing factor for RSSI. alpha=0.1 means the EMA responds to
# sustained RSSI change over roughly 10 samples but ignores single-
# packet outliers. A chatty device advertising at 100 ms therefore
# smooths over about 1 second.
_RSSI_EMA_ALPHA: float = 0.1

# Exposure bands on smoothed RSSI (dBm). These boundaries come from
# empirical observation of a suburban 3-bedroom house: very-close is
# "same room as the receiver within ~2 m", close is "same room or
# adjacent through one wall", mid is "across the house", far is
# "neighbor or outside". Tune per site.
_BAND_VERY_CLOSE: int = -55
_BAND_CLOSE: int = -70
_BAND_MID: int = -85

# Hysteresis applied to band transitions. The EMA must clear a boundary
# by this many dBm before a band change is accepted, preventing "moved"
# storms when RSSI straddles a threshold (e.g. device at exactly -70 dBm
# toggling close↔mid at 6 Hz and writing retained MQTT every crossing).
_BAND_HYSTERESIS_DB: float = 3.0

# Periodic heartbeat interval. A stationary tracked device emits one
# ``heartbeat`` event per this period so the dashboard can show "last
# seen N seconds ago" without waiting for the next physical change.
_HEARTBEAT_INTERVAL_S: float = 300.0

# Absence threshold. If no advert is heard from a MAC for this many
# seconds, a ``gone`` event is emitted and the MAC's seen-retained is
# overwritten with a "gone" marker so the dashboard grays it out.
_GONE_AFTER_S: float = 120.0

# Absence sweep cadence — how often the background task scans for
# gone MACs. Has to be shorter than _GONE_AFTER_S to keep timing
# tight enough for a useful "left the house" signal, but long enough
# that we're not burning CPU on empty sweeps.
_GONE_SWEEP_S: float = 10.0

# IEEE OUI database path (Debian/Ubuntu ``ieee-data`` package).
# Falls back to empty-table (no vendor lookup) if missing; install
# with ``sudo apt install ieee-data``.
_OUI_FILE_DEFAULT: str = "/usr/share/ieee-data/oui.txt"

# Apple BLE manufacturer-data subtype byte → human label. Source:
# community-reverse-engineered docs
# (https://adamcatley.com/AirPods.html and many others). The bytes
# 0x4C 0x00 at the start of mfr_data indicate Apple (company ID).
_APPLE_SUBTYPES: dict[int, str] = {
    0x01: "Apple iBeacon-pre",
    0x02: "Apple iBeacon",
    0x03: "AirPrint",
    0x05: "AirDrop",
    0x06: "HomeKit",
    0x07: "AirPods / Proximity Pairing",
    0x08: "Hey Siri",
    0x09: "AirPlay",
    0x0A: "AirPlay",
    0x0B: "Magic Switch",
    0x0C: "Handoff",
    0x0D: "Wi-Fi Settings",
    0x0E: "Instant Hotspot",
    0x0F: "Nearby Action",
    0x10: "iPhone / iPad (Nearby Info)",
    0x11: "FindMy",
    0x12: "FindMy Network",
    0x16: "Proximity Pairing",
}

# Well-known company IDs — the first two bytes (little-endian) of
# manufacturer-specific data. Full list: Bluetooth SIG Assigned
# Numbers; we only pin the ones we actually see often.
_COMPANY_IDS: dict[int, str] = {
    0x004C: "Apple",
    0x00E0: "Google",
    0x0075: "Samsung",
    0x0006: "Microsoft",
    0x0059: "Nordic Semiconductor",
    0x0499: "Ruuvi Innovations",
    0x038F: "Xiaomi",
}

# Valid policies a registry entry may request.
_POLICY_HEARTBEAT_ONLY: str = "heartbeat_only"
_POLICY_SENSOR_EVENT: str = "sensor_event"
_POLICY_TRACK: str = "track"
_POLICY_IGNORE: str = "ignore"
_POLICY_DEFAULT: str = "default"
_ALL_POLICIES: frozenset[str] = frozenset({
    _POLICY_HEARTBEAT_ONLY, _POLICY_SENSOR_EVENT,
    _POLICY_TRACK, _POLICY_IGNORE, _POLICY_DEFAULT,
})


logger: logging.Logger = logging.getLogger("glowup.ble_sniffer")


# ---------------------------------------------------------------------------
# Enrichment — OUI vendor lookup + Apple/Google mfr-data decoders
# ---------------------------------------------------------------------------

def _load_oui_table(path: str) -> dict[str, str]:
    """Parse the IEEE oui.txt into a {'84BA20': 'Vendor Name'} dict.

    Wireshark / IEEE format has one vendor per block:

        84-BA-20   (hex)      Apple, Inc.
        84BA20     (base 16)  Apple, Inc.

    We key on the second form (hex-no-separator uppercase) because it
    matches the OUI we extract from an observed MAC.
    """
    table: dict[str, str] = {}
    if not os.path.exists(path):
        logger.info("OUI file not found at %s — vendor lookup disabled", path)
        return table
    # Format of the ieee-data oui.txt vendor line:
    #   "84BA20     (base 16)\t\tApple, Inc."
    # The "(base 16)" marker has internal whitespace so a simple
    # split-3 won't work. Anchor on the literal marker substring.
    marker: str = "(base 16)"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                idx: int = line.find(marker)
                if idx < 0:
                    continue
                oui_hex: str = line[:idx].strip().upper()
                if len(oui_hex) != 6:
                    continue
                vendor: str = line[idx + len(marker):].strip()
                if vendor:
                    table[oui_hex] = vendor
    except OSError as exc:
        logger.warning("OUI parse failed: %s", exc)
    logger.info("OUI table: %d vendors loaded", len(table))
    return table


def _oui_vendor(mac: str, table: dict[str, str]) -> Optional[str]:
    """Return IEEE-registered vendor for this MAC, or None if absent.

    Called on every MAC unconditionally — the OUI database itself is
    the authority on whether the address is public. A hit means
    public; a miss means either random (rotating) or an unregistered
    manufacturer.
    """
    if not mac:
        return None
    try:
        oui: str = mac.replace(":", "")[:6].upper()
    except AttributeError:
        return None
    return table.get(oui)


def _mac_address_kind(mac: str, vendor: Optional[str]) -> str:
    """Classify the BLE address: public / random-static / resolvable / etc.

    Public is authoritatively signalled by a hit in the IEEE OUI
    table (``vendor`` is non-None). For addresses with no OUI match
    we apply the Bluetooth Core Spec top-2-bits heuristic for
    random-address sub-kind:

        11 → static random (fixed for device lifetime)
        01 → resolvable private (rotates every ~15 min)
        00 → non-resolvable private (rotates, no IRK linkage)

    BLE does not repurpose the IEEE U/L bit, so an unregistered OUI
    with a clear top-2 bits pattern can legitimately be an unknown
    public-OUI-not-yet-in-our-DB device; we mark it "public-unknown"
    to distinguish from clearly-random patterns.
    """
    if vendor is not None:
        return "public"
    try:
        first_byte: int = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return "unknown"
    top2: int = (first_byte >> 6) & 0x03
    if top2 == 0b11:
        return "random-static"
    if top2 == 0b01:
        return "random-resolvable"
    if top2 == 0b00:
        return "public-unknown"
    return "random-reserved"


def _decode_mfr(mfr_hex: Optional[str]) -> tuple[
    Optional[str], Optional[str],
]:
    """Decode the company ID + Apple-subtype label from manufacturer data.

    Args:
        mfr_hex: hex string of the raw manufacturer-specific data
                 payload (company-id prefix + vendor payload).

    Returns:
        (company_label, subtype_label). Either element may be None.
        company_label is the Bluetooth SIG company name when known;
        subtype_label is the Apple-specific subtype when the company
        is Apple and we recognise the type byte.
    """
    if not mfr_hex or len(mfr_hex) < 4:
        return (None, None)
    try:
        # Company ID is little-endian: first two hex-bytes are LSB/MSB.
        company_id: int = int(mfr_hex[2:4] + mfr_hex[0:2], 16)
    except ValueError:
        return (None, None)
    company_label: Optional[str] = _COMPANY_IDS.get(company_id)
    subtype_label: Optional[str] = None
    if company_id == 0x004C and len(mfr_hex) >= 6:
        try:
            type_byte: int = int(mfr_hex[4:6], 16)
            subtype_label = _APPLE_SUBTYPES.get(type_byte)
        except ValueError:
            subtype_label = None
    return (company_label, subtype_label)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MacState:
    """Per-MAC state tracked across adverts for event-vs-seen decisions."""

    mac: str
    first_seen_ts: float
    last_heard_ts: float
    last_heartbeat_ts: float = 0.0
    name_cached: Optional[str] = None
    rssi_ema: float = 0.0
    last_band: str = "none"
    last_payload_hash: str = ""
    label: str = ""
    category: str = "unknown"
    policy: str = _POLICY_DEFAULT
    published_any: bool = False
    last_mfr_data: Optional[str] = None
    last_services: list = field(default_factory=list)
    last_tx_power: Optional[int] = None
    # Enrichment — populated once and cached for the MAC lifetime.
    # vendor and mac_kind are derived from the MAC itself so they
    # don't change; company and apple_subtype can update when the
    # advertised manufacturer data changes (hence refreshed on every
    # payload_changed).
    vendor: Optional[str] = None
    mac_kind: str = "unknown"
    company: Optional[str] = None
    apple_subtype: Optional[str] = None


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------

def _load_registry(path: str) -> dict[str, dict[str, Any]]:
    """Return MAC→metadata dict. Missing / malformed file → {}.

    Validates that each entry has a usable policy. Unknown policies
    are logged and the entry falls back to ``default``.
    """
    if not os.path.exists(path):
        logger.info("registry not found at %s — all devices default", path)
        return {}
    try:
        with open(path, "r") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("registry load failed (%s): %s — using empty", path, exc)
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for mac, meta in raw.items():
        # Underscore-prefixed keys are reserved for comments / docs
        # in the JSON file (JSON has no native comment syntax); they
        # are never treated as device entries.
        if mac.startswith("_"):
            continue
        if not isinstance(meta, dict):
            logger.warning("registry: skipping non-dict entry for %s", mac)
            continue
        policy: str = str(meta.get("handling", _POLICY_DEFAULT))
        if policy not in _ALL_POLICIES:
            logger.warning(
                "registry: %s has unknown policy %r — using default",
                mac, policy,
            )
            policy = _POLICY_DEFAULT
        normalized[mac.upper()] = {
            "label": str(meta.get("label", "")),
            "category": str(meta.get("category", "unknown")),
            "location": str(meta.get("location", "")),
            "handling": policy,
        }
    logger.info("registry: loaded %d entries from %s", len(normalized), path)
    return normalized


# ---------------------------------------------------------------------------
# Advertisement parsing
# ---------------------------------------------------------------------------

def _safe_mfr_hex(raw: Any) -> Optional[str]:
    """Convert aioblescan mfr-data payload to a hex string, or None.

    aioblescan payload shape varies across versions and advertisement
    sub-types: bytes, bytearray, list[int], or list[objects with .val].
    Any decode failure returns None rather than raising.
    """
    if raw is None:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).hex() if raw else None
        if not raw:
            return None
        first = raw[0]
        if isinstance(first, int):
            return bytes(raw).hex()
        if hasattr(first, "val"):
            vals = [b.val for b in raw]
            if all(isinstance(v, int) and 0 <= v < 256 for v in vals):
                return bytes(vals).hex()
    except (TypeError, AttributeError, ValueError):
        pass
    return None


def _extract(ev: "aiobs.HCI_Event") -> Optional[dict[str, Any]]:
    """Pull the useful fields out of an aioblescan advertising event."""
    mac_obj = ev.retrieve("peer")
    if not mac_obj:
        return None
    mac: str = mac_obj[0].val.upper()

    rssi_obj = ev.retrieve("rssi")
    rssi: int = rssi_obj[0].val if rssi_obj else 0

    name_obj = ev.retrieve("Complete Name") or ev.retrieve("Shortened Name")
    name_val = name_obj[0].val if name_obj else None
    if isinstance(name_val, (bytes, bytearray)):
        name_val = name_val.decode("utf-8", errors="replace")

    mfr_obj = ev.retrieve("Manufacturer Specific Data")
    mfr_hex: Optional[str] = None
    if mfr_obj:
        mfr_hex = _safe_mfr_hex(mfr_obj[0].payload)

    tx_obj = ev.retrieve("Tx Power")
    tx_power: Optional[int] = tx_obj[0].val if tx_obj else None

    uuid16 = ev.retrieve("Complete uuids 16")
    services: list = []
    if uuid16:
        for u in uuid16:
            v = u.val
            if isinstance(v, (bytes, bytearray)):
                services.append(bytes(v).hex())
            else:
                services.append(v)

    return {
        "mac": mac,
        "rssi": rssi,
        "name": name_val,
        "mfr_data": mfr_hex,
        "tx_power": tx_power,
        "services": services,
    }


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def _band_for(rssi_ema: float) -> str:
    """Map smoothed RSSI to an exposure band label (no hysteresis)."""
    if rssi_ema >= _BAND_VERY_CLOSE:
        return "very-close"
    if rssi_ema >= _BAND_CLOSE:
        return "close"
    if rssi_ema >= _BAND_MID:
        return "mid"
    return "far"


_BAND_RANK: dict[str, int] = {"far": 0, "mid": 1, "close": 2, "very-close": 3}
_BAND_LOWER_EDGE: dict[str, float] = {
    "mid": _BAND_MID, "close": _BAND_CLOSE, "very-close": _BAND_VERY_CLOSE,
}
_BAND_BY_RANK: list[str] = ["far", "mid", "close", "very-close"]


def _next_band(rssi_ema: float, current_band: str) -> str:
    """Band for rssi_ema with hysteresis applied against current_band.

    A band change is only accepted when the EMA has cleared the boundary
    by _BAND_HYSTERESIS_DB dBm.  Devices hovering at a threshold freeze
    in their current band rather than oscillating.
    """
    if current_band not in _BAND_RANK:
        return _band_for(rssi_ema)
    candidate = _band_for(rssi_ema)
    if candidate == current_band:
        return current_band
    H = _BAND_HYSTERESIS_DB
    cur_rank = _BAND_RANK[current_band]
    cand_rank = _BAND_RANK[candidate]
    if cand_rank > cur_rank:
        # Moving closer: must clear the lower edge of the next band up by H.
        edge = _BAND_LOWER_EDGE[_BAND_BY_RANK[cur_rank + 1]]
        return candidate if rssi_ema >= edge + H else current_band
    else:
        # Moving farther: must drop below the lower edge of current band by H.
        edge = _BAND_LOWER_EDGE[current_band]
        return candidate if rssi_ema < edge - H else current_band


def _payload_hash(mfr_data: Optional[str],
                  tx_power: Optional[int],
                  services: list) -> str:
    """Stable hash of the content-bearing fields of an advert."""
    h = hashlib.sha1()
    h.update((mfr_data or "").encode("utf-8"))
    h.update(str(tx_power).encode("utf-8"))
    h.update(",".join(sorted(str(s) for s in services)).encode("utf-8"))
    return h.hexdigest()


def _snapshot(state: MacState) -> dict[str, Any]:
    """Serializable snapshot of MacState for MQTT payloads."""
    return {
        "mac": state.mac,
        "name": state.name_cached,
        "label": state.label,
        "category": state.category,
        "policy": state.policy,
        "first_seen_ts": state.first_seen_ts,
        "last_heard_ts": state.last_heard_ts,
        "rssi_ema": round(state.rssi_ema, 1),
        "band": state.last_band,
        "mfr_data": state.last_mfr_data,
        "tx_power": state.last_tx_power,
        "services": state.last_services,
        "vendor": state.vendor,
        "mac_kind": state.mac_kind,
        "company": state.company,
        "apple_subtype": state.apple_subtype,
    }


class Sniffer:
    """Owner of the MAC state table + MQTT client + event policy."""

    def __init__(
        self, registry: dict[str, dict[str, Any]],
        oui_table: dict[str, str],
        mqtt_client: "mqtt.Client",
    ) -> None:
        self._registry: dict[str, dict[str, Any]] = registry
        self._oui: dict[str, str] = oui_table
        self._client: "mqtt.Client" = mqtt_client
        self._state: dict[str, MacState] = {}

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _allow(self, policy: str, event_type: str) -> bool:
        """Policy filter — return True if an event of this type fires."""
        if policy == _POLICY_IGNORE:
            return False
        if policy == _POLICY_HEARTBEAT_ONLY:
            return event_type in {"heartbeat", "gone"}
        if policy == _POLICY_SENSOR_EVENT:
            return event_type in {
                "first_seen", "payload_changed", "heartbeat", "gone",
            }
        if policy == _POLICY_TRACK:
            return True
        # default (unknown devices): no heartbeats — quiet for stranger
        # detection, but emit on genuine events.
        return event_type in {
            "first_seen", "payload_changed", "moved", "gone",
        }

    def _publish_event(self, state: MacState, event_type: str) -> None:
        """Emit a non-retained event record."""
        if not self._allow(state.policy, event_type):
            return
        mac_flat: str = state.mac.replace(":", "")
        topic: str = f"{_TOPIC_EVENT}/{mac_flat}"
        payload: dict[str, Any] = {
            "ts": time.time(),
            "event": event_type,
            **_snapshot(state),
        }
        try:
            self._client.publish(
                topic, json.dumps(payload, default=str),
                qos=0, retain=False,
            )
        except Exception as exc:
            logger.warning("event publish failed for %s: %s", state.mac, exc)

    def _publish_seen(self, state: MacState,
                      gone: bool = False) -> None:
        """Emit / clear the retained seen snapshot."""
        if state.policy == _POLICY_IGNORE:
            return
        mac_flat: str = state.mac.replace(":", "")
        topic: str = f"{_TOPIC_SEEN}/{mac_flat}"
        if gone:
            payload: dict[str, Any] = {
                "mac": state.mac, "gone": True,
                "last_heard_ts": state.last_heard_ts,
            }
        else:
            payload = {"ts": time.time(), **_snapshot(state)}
        try:
            self._client.publish(
                topic, json.dumps(payload, default=str),
                qos=0, retain=True,
            )
        except Exception as exc:
            logger.warning("seen publish failed for %s: %s", state.mac, exc)

    # ------------------------------------------------------------------
    # Advertisement ingress
    # ------------------------------------------------------------------

    def on_advert(self, data: bytes) -> None:
        """Decode one HCI event and advance the state machine."""
        ev = aiobs.HCI_Event()
        try:
            ev.decode(data)
        except Exception:
            return
        try:
            rec: Optional[dict[str, Any]] = _extract(ev)
        except Exception as exc:
            logger.warning("extract failed: %s", exc)
            return
        if not rec:
            return
        self._ingest(rec)

    def _ingest(self, rec: dict[str, Any]) -> None:
        mac: str = rec["mac"]
        now: float = time.time()
        rssi: int = rec["rssi"]
        state: Optional[MacState] = self._state.get(mac)
        if state is None:
            state = self._new_state(mac, now, rssi)
            self._state[mac] = state
            # First-heard: cache whatever we have, compute band,
            # record payload hash, then emit first_seen.
            self._update_content(state, rec)
            state.rssi_ema = float(rssi)
            state.last_band = _band_for(state.rssi_ema)
            self._publish_event(state, "first_seen")
            state.last_heartbeat_ts = now
            state.published_any = True
            self._publish_seen(state)
            return

        # Existing MAC: update name cache (opportunistic — scan
        # responses often carry the name in a separate packet).
        name_new: Optional[Any] = rec.get("name")
        if isinstance(name_new, str) and name_new and not state.name_cached:
            state.name_cached = name_new

        state.last_heard_ts = now

        # Smooth RSSI before any band comparison.
        state.rssi_ema = (
            _RSSI_EMA_ALPHA * rssi
            + (1 - _RSSI_EMA_ALPHA) * state.rssi_ema
        )

        payload_changed: bool = self._update_content(state, rec)
        new_band: str = _next_band(state.rssi_ema, state.last_band)
        band_changed: bool = (new_band != state.last_band)
        if band_changed:
            state.last_band = new_band

        emitted: bool = False
        if payload_changed:
            self._publish_event(state, "payload_changed")
            emitted = True
        if band_changed:
            self._publish_event(state, "moved")
            emitted = True

        # Heartbeat: independent of band/payload, periodic "still here".
        if (now - state.last_heartbeat_ts) >= _HEARTBEAT_INTERVAL_S:
            self._publish_event(state, "heartbeat")
            state.last_heartbeat_ts = now
            emitted = True

        if emitted or not state.published_any:
            state.published_any = True
            self._publish_seen(state)

    def _new_state(self, mac: str, now: float, rssi: int) -> MacState:
        meta: dict[str, Any] = self._registry.get(mac, {})
        return MacState(
            mac=mac,
            first_seen_ts=now,
            last_heard_ts=now,
            label=meta.get("label", ""),
            category=meta.get("category", "unknown"),
            policy=meta.get("handling", _POLICY_DEFAULT),
            rssi_ema=float(rssi),
            # One-time MAC-derived enrichment. These fields never
            # change for a given MAC so we compute once at creation.
            vendor=(vendor_hit := _oui_vendor(mac, self._oui)),
            mac_kind=_mac_address_kind(mac, vendor_hit),
        )

    def _update_content(
        self, state: MacState, rec: dict[str, Any],
    ) -> bool:
        """Return True if content-bearing fields changed."""
        mfr: Optional[str] = rec.get("mfr_data")
        tx: Optional[int] = rec.get("tx_power")
        svcs: list = rec.get("services", [])
        new_hash: str = _payload_hash(mfr, tx, svcs)
        changed: bool = (new_hash != state.last_payload_hash)
        if changed:
            state.last_payload_hash = new_hash
            state.last_mfr_data = mfr
            state.last_tx_power = tx
            state.last_services = svcs
            # Re-decode company ID + Apple subtype whenever payload
            # changes — these are payload-dependent, unlike the
            # MAC-derived vendor/kind which are fixed for the device.
            company, subtype = _decode_mfr(mfr)
            state.company = company
            state.apple_subtype = subtype
        return changed

    # ------------------------------------------------------------------
    # Absence sweep
    # ------------------------------------------------------------------

    async def gone_sweep(self) -> None:
        """Periodically emit ``gone`` events for silent MACs."""
        while True:
            await asyncio.sleep(_GONE_SWEEP_S)
            now: float = time.time()
            for mac, state in list(self._state.items()):
                if (now - state.last_heard_ts) < _GONE_AFTER_S:
                    continue
                # Emit gone once, then drop the MAC — next advert will
                # re-fire first_seen, which is the correct semantics for
                # a device that left and came back.
                self._publish_event(state, "gone")
                self._publish_seen(state, gone=True)
                del self._state[mac]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> int:
    registry: dict[str, dict[str, Any]] = _load_registry(args.registry)
    oui_table: dict[str, str] = _load_oui_table(args.oui_file)

    client: "mqtt.Client" = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=_CLIENT_ID,
    )
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()
    logger.info("connected to %s:%d", args.broker, args.port)

    sniffer: Sniffer = Sniffer(registry, oui_table, client)

    try:
        sock = aiobs.create_bt_socket(args.hci)
    except OSError as e:
        logger.error("cannot open HCI socket (need CAP_NET_RAW?): %s", e)
        return 1

    loop = asyncio.get_running_loop()
    fac = loop._create_connection_transport(
        sock, aiobs.BLEScanRequester, None, None,
    )
    conn, btctrl = await fac
    btctrl.process = sniffer.on_advert
    # aioblescan 0.2.14 made send_scan_request / stop_scan_request
    # coroutines; they must be awaited or the scan never starts on
    # the controller.
    await btctrl.send_scan_request()
    logger.info("scanning on hci%d", args.hci)

    sweep_task: asyncio.Task = asyncio.create_task(sniffer.gone_sweep())
    stop: asyncio.Event = asyncio.Event()

    def _on_signal() -> None:
        stop.set()

    loop.add_signal_handler(signal.SIGTERM, _on_signal)
    loop.add_signal_handler(signal.SIGINT, _on_signal)

    try:
        await stop.wait()
    finally:
        sweep_task.cancel()
        try:
            await btctrl.stop_scan_request()
        except Exception:
            pass
        conn.close()
        client.loop_stop()
        client.disconnect()
        logger.info("clean shutdown")
    return 0


def main() -> int:
    """CLI entry point."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "",
    )
    parser.add_argument("--broker", default=_BROKER_DEFAULT)
    parser.add_argument("--port", type=int, default=_PORT_DEFAULT)
    parser.add_argument("--hci", type=int, default=_HCI_DEFAULT)
    parser.add_argument("--registry", default=_REGISTRY_DEFAULT)
    parser.add_argument("--oui-file", default=_OUI_FILE_DEFAULT,
                        help="IEEE OUI DB (from 'ieee-data' package)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
