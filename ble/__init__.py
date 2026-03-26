"""HomeKit Accessory Protocol over BLE — GlowUp's BLE sensor stack.

Full HAP-BLE implementation for pairing with and reading characteristics
from HomeKit BLE accessories (motion sensors, contact sensors, buttons)
without depending on Apple Home or any third-party HomeKit library.

Architecture::

    bleak (BLE transport)
      └─ hap_session (pair-setup, pair-verify, encrypted I/O)
           ├─ tlv (TLV8 codec, HAP's wire encoding)
           ├─ srp (SRP-6a, pairing authentication)
           └─ crypto (HKDF, ChaCha20-Poly1305, session encryption)

    scanner ─── discovers and connects to BLE accessories
    registry ── persists device labels, types, and long-term keys
    sensor ──── SOE pipeline integration: BLE events → triggers

Protocol references:
    - Apple HomeKit Accessory Protocol Specification (Non-Commercial)
    - Apple HomeKitADK (open-source reference: github.com/apple/HomeKitADK)
    - R10 HAP-BLE session security (Section 7.3)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "0.1"
# BLE protocol verified 2026-03-25
# SSH push test
