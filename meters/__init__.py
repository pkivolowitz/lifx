"""Meter capture — sub-GHz utility meter telemetry via SDR.

Captures broadcasts from neighborhood electric (ITRON ERT — SCM, SCM+,
IDM, NetIDM) and water (Neptune R900) meters using rtl_433 on a host
with an RTL-SDR dongle (today: ernie, 10.0.0.153).  Each parsed
reading is published cross-host to the hub's MQTT broker on the
``glowup/meters/<meter_id>`` topic where the corresponding
:class:`infrastructure.meter_logger.MeterLogger` persists it to
PostgreSQL.

Civic motivation: the operator suspects the local water utility
(MAWSS, Mobile, AL) is over-billing irrigation usage by an order of
magnitude.  Capturing the actual radio transmissions the meter is
sending — independent of the utility's reading — provides
third-party-independent evidence to compare against the bill.

Module layout::

    meters/
        __init__.py             — this file
        publisher.py            — rtl_433 → MQTT bridge (runs on ernie)
        glowup-meters.service   — systemd unit for the publisher
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"
