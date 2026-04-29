"""HAP-CoAP-over-Thread bridge for HomeKit accessories.

Talks to HomeKit-Thread accessories (e.g. ONVIS SMS2) via CoAP-over-UDP
on the Thread mesh.  Designed to coexist with the BLE-side HAP code in
``lifx/ble/`` — primitives (TLV, SRP, crypto, HAP constants) are imported
from there; nothing in ``lifx/ble/`` is modified.

See ``docs/40-hap-coap-wire-format.md`` for the wire-format reference.
See ``.claude/plans/hap_thread.md`` for the implementation plan (local).
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "0.1"
