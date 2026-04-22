"""Device-type taxonomy for glowup-zigbee-service.

Shared between the service (broker-2) and every client of the service
(hub dashboard, hub scheduler, voice coordinator on Daedalus).  Kept
in its own module so clients can import the taxonomy without pulling
the service's runtime deps (paho-mqtt, sqlite3).

The taxonomy classifies every paired Zigbee device into exactly one
string-tagged type by fingerprinting the accumulated Z2M payload.
Hub-side consumers filter by ``type`` rather than re-implementing
inference — if this file gains a type, every consumer picks it up by
re-import, with no chance of fingerprint drift between call sites.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.0"

from typing import Any

TYPE_PLUG: str = "plug"         # switchable relay, metering optional
TYPE_SOIL: str = "soil"         # soil-moisture sensor
TYPE_CONTACT: str = "contact"   # magnet / door-window sensor
TYPE_MOTION: str = "motion"     # PIR / occupancy sensor
TYPE_BUTTON: str = "button"     # scene controller / button remote
TYPE_UNKNOWN: str = "unknown"   # fallback — insufficient fingerprint yet

KNOWN_TYPES: frozenset[str] = frozenset({
    TYPE_PLUG, TYPE_SOIL, TYPE_CONTACT, TYPE_MOTION, TYPE_BUTTON, TYPE_UNKNOWN,
})


def infer_device_type(raw: dict[str, Any]) -> str:
    """Classify a Zigbee device by its accumulated Z2M payload.

    *raw* is the full merged payload snapshot the service holds on
    ``DeviceState.raw`` — it accumulates across messages, so once a
    device has ever reported a distinguishing field the classification
    sticks even if later heartbeats arrive with sparser payloads.

    Inference is specific-before-general: sensor fingerprints are
    checked before plug (which matches on a generic ``state`` key)
    to avoid mis-classifying a sensor that happens to include ``state``.
    """
    # Sensor fingerprints first — these are unambiguous.
    if "soil_moisture" in raw:
        return TYPE_SOIL
    if "contact" in raw:
        return TYPE_CONTACT
    if "occupancy" in raw or "motion" in raw:
        return TYPE_MOTION
    if "action" in raw:
        return TYPE_BUTTON
    # Switchable relay (ThirdReality Gen3 plugs report power+voltage+state;
    # bare relays without metering still report state).  Metering fields
    # are deliberately NOT required — a pure on/off plug is still a plug.
    if "state" in raw:
        return TYPE_PLUG
    return TYPE_UNKNOWN
