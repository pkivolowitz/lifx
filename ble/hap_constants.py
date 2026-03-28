"""HAP-BLE constants — TLV types, opcodes, status codes, UUIDs.

Central reference for every magic number in the HomeKit Accessory
Protocol over BLE.  Each constant includes the HAP spec section or
table it originates from.

Sections reference the HAP Non-Commercial Specification R17 unless
otherwise noted.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

# ---------------------------------------------------------------------------
# TLV types — Pair Setup / Pair Verify (Table 4-4)
# ---------------------------------------------------------------------------

# Pairing method being performed.
TLV_METHOD: int = 0x00

# Current step identifier (1–6 for pair-setup, 1–4 for pair-verify).
TLV_STATE: int = 0x06

# Error code if the step failed.
TLV_ERROR: int = 0x07

# SRP public key (pair-setup) or Curve25519 public key (pair-verify).
TLV_PUBLIC_KEY: int = 0x03

# SRP salt (pair-setup M2 only, 16 bytes).
TLV_SALT: int = 0x02

# SRP proof from client (M1) or server (M2).
TLV_PROOF: int = 0x04

# Encrypted data + auth tag (pair-setup M5/M6, pair-verify M2/M3).
TLV_ENCRYPTED_DATA: int = 0x05

# Pairing identifier (controller or accessory, up to 36 bytes).
TLV_IDENTIFIER: int = 0x01

# Ed25519 signature (64 bytes).
TLV_SIGNATURE: int = 0x0A

# Ed25519 long-term public key (32 bytes).
TLV_CERTIFICATE: int = 0x09  # Spec calls it "Certificate" but it's LTPK.

# Pairing type flags (e.g., transient pair-setup).
TLV_FLAGS: int = 0x13

# Separator — zero-length TLV used between logical records.
TLV_SEPARATOR: int = 0xFF

# ---------------------------------------------------------------------------
# Pairing methods (TLV_METHOD values, Table 4-5)
# ---------------------------------------------------------------------------

# Standard pair-setup using the 8-digit setup code and SRP-6a.
METHOD_PAIR_SETUP: int = 0

# Reserved for MFi pair-setup (not used here).
METHOD_PAIR_SETUP_WITH_AUTH: int = 1

# Session establishment using persisted long-term keys.
METHOD_PAIR_VERIFY: int = 2

# Add a new controller pairing (admin only).
METHOD_ADD_PAIRING: int = 3

# Remove an existing controller pairing.
METHOD_REMOVE_PAIRING: int = 4

# List all controller pairings.
METHOD_LIST_PAIRINGS: int = 5

# ---------------------------------------------------------------------------
# Pairing error codes (TLV_ERROR values, Table 4-6)
# ---------------------------------------------------------------------------

# Reserved / no error.
ERR_UNKNOWN: int = 0x01

# Authentication failed (wrong setup code, bad proof).
ERR_AUTHENTICATION: int = 0x02

# Client retried too many times — accessory locked out.
ERR_BACKOFF: int = 0x03

# Too many paired controllers.
ERR_MAX_PEERS: int = 0x04

# Max authentication attempts reached.
ERR_MAX_TRIES: int = 0x05

# Accessory is not accepting pairing requests.
ERR_UNAVAILABLE: int = 0x06

# Generic busy — retry later.
ERR_BUSY: int = 0x07

# Human-readable error descriptions for logging.
ERROR_DESCRIPTIONS: dict[int, str] = {
    ERR_UNKNOWN: "Unknown error",
    ERR_AUTHENTICATION: "Authentication failed (wrong setup code?)",
    ERR_BACKOFF: "Too many attempts — accessory locked, wait and retry",
    ERR_MAX_PEERS: "Maximum number of paired controllers reached",
    ERR_MAX_TRIES: "Maximum authentication attempts exhausted",
    ERR_UNAVAILABLE: "Accessory not accepting pairing requests",
    ERR_BUSY: "Accessory busy — retry later",
}

# ---------------------------------------------------------------------------
# HAP-BLE PDU opcodes (Table 7-8)
#
# CRITICAL: verified against homekit_python HapBleOpCodes and live
# ONVIS SMS2 testing 2026-03-25.  The original values in this file
# were wrong (0x00 was CHAR_READ, 0x01 was CHAR_WRITE).  The correct
# mapping — confirmed by successful pair-setup — is below.
# ---------------------------------------------------------------------------

# Characteristic signature read (returns characteristic metadata).
OPCODE_CHAR_SIGNATURE_READ: int = 0x01

# Write a characteristic value.
OPCODE_CHAR_WRITE: int = 0x02

# Read a characteristic value.
OPCODE_CHAR_READ: int = 0x03

# Timed write (write with TTL).
OPCODE_CHAR_TIMED_WRITE: int = 0x04

# Execute write (commit a timed write).
OPCODE_CHAR_EXEC_WRITE: int = 0x05

# Service signature read (discovery of service characteristics).
OPCODE_SERVICE_SIGNATURE_READ: int = 0x06

# Subscribe to characteristic notifications (events).
OPCODE_CHAR_SUBSCRIBE: int = 0x07

# Unsubscribe from notifications.
OPCODE_CHAR_UNSUBSCRIBE: int = 0x08

# ---------------------------------------------------------------------------
# HAP-BLE PDU status codes (Table 7-9)
# ---------------------------------------------------------------------------

STATUS_SUCCESS: int = 0x0000
STATUS_UNSUPPORTED_PDU: int = 0x0001
STATUS_MAX_PROCEDURES: int = 0x0002
STATUS_INSUFFICIENT_AUTH: int = 0x0003
STATUS_INVALID_INSTANCE_ID: int = 0x0004
STATUS_INSUFFICIENT_AUTHORIZATION: int = 0x0005
STATUS_INVALID_REQUEST: int = 0x0006

STATUS_DESCRIPTIONS: dict[int, str] = {
    STATUS_SUCCESS: "Success",
    STATUS_UNSUPPORTED_PDU: "Unsupported PDU",
    STATUS_MAX_PROCEDURES: "Max procedures exceeded",
    STATUS_INSUFFICIENT_AUTH: "Insufficient authentication",
    STATUS_INVALID_INSTANCE_ID: "Invalid instance ID",
    STATUS_INSUFFICIENT_AUTHORIZATION: "Insufficient authorization",
    STATUS_INVALID_REQUEST: "Invalid request",
}

# ---------------------------------------------------------------------------
# HAP-BLE PDU control field (Section 7.3.3)
# ---------------------------------------------------------------------------

# Bit 0: request (0) or response (1).
PDU_TYPE_REQUEST: int = 0b00000000
PDU_TYPE_RESPONSE: int = 0b00000010

# Bits 1-2: fragmentation.
PDU_FRAGMENT_FIRST: int = 0b00000000   # First (or only) fragment.
PDU_FRAGMENT_CONTINUATION: int = 0b10000000  # Continuation fragment.

# ---------------------------------------------------------------------------
# HomeKit service and characteristic UUIDs (HAP spec Appendix)
#
# HAP uses "short" UUIDs in the Apple BT base:
#     XXXXXXXX-0000-1000-8000-0026BB765291
# Only the first 4 bytes vary.
# ---------------------------------------------------------------------------

# Apple Bluetooth base UUID template.
_HAP_BASE: str = "00000000-0000-1000-8000-0026BB765291"


def hap_uuid(short: int) -> str:
    """Expand a short HAP UUID to full 128-bit string form.

    Args:
        short: The 32-bit short UUID (e.g., 0x43 for Pairing).

    Returns:
        Full UUID string like ``"00000043-0000-1000-8000-0026BB765291"``.
    """
    return f"{short:08X}-0000-1000-8000-0026BB765291"


# --- Services ---

# HAP Pairing Service — always present, used for pair-setup/verify.
SERVICE_PAIRING: str = hap_uuid(0x55)

# HAP Protocol Information Service — version, config number.
SERVICE_PROTOCOL_INFO: str = hap_uuid(0xA2)

# HAP Accessory Information Service — name, manufacturer, model.
SERVICE_ACCESSORY_INFO: str = hap_uuid(0x3E)

# --- Pairing characteristics (within SERVICE_PAIRING) ---

# Pair Setup characteristic — write pair-setup TLV requests.
CHAR_PAIR_SETUP: str = hap_uuid(0x4C)

# Pair Verify characteristic — write pair-verify TLV requests.
CHAR_PAIR_VERIFY: str = hap_uuid(0x4E)

# Pairing Features characteristic — bitmask of supported methods.
CHAR_PAIRING_FEATURES: str = hap_uuid(0x4F)

# Pairings characteristic — add/remove/list pairings.
CHAR_PAIRINGS: str = hap_uuid(0x50)

# --- Sensor characteristics (the ones we actually care about) ---

# Occupancy Detected — boolean, 0 or 1 (our motion trigger).
CHAR_OCCUPANCY_DETECTED: str = hap_uuid(0x71)

# Motion Detected — boolean, 0 or 1.
CHAR_MOTION_DETECTED: str = hap_uuid(0x22)

# Current Temperature — float, Celsius.
CHAR_CURRENT_TEMPERATURE: str = hap_uuid(0x11)

# Current Relative Humidity — float, percentage.
CHAR_CURRENT_HUMIDITY: str = hap_uuid(0x10)

# Status Active — boolean, whether the sensor is operational.
CHAR_STATUS_ACTIVE: str = hap_uuid(0x75)

# Battery Level — uint8, 0–100.
CHAR_BATTERY_LEVEL: str = hap_uuid(0x68)

# Status Low Battery — uint8, 0 = normal, 1 = low.
CHAR_STATUS_LOW_BATTERY: str = hap_uuid(0x79)

# Service Instance ID — required for HAP-BLE PDU addressing.
CHAR_SERVICE_INSTANCE_ID: str = hap_uuid(0xE3)

# --- Accessory Information characteristics ---

CHAR_NAME: str = hap_uuid(0x23)
CHAR_MANUFACTURER: str = hap_uuid(0x20)
CHAR_MODEL: str = hap_uuid(0x21)
CHAR_SERIAL_NUMBER: str = hap_uuid(0x30)
CHAR_FIRMWARE_REVISION: str = hap_uuid(0x52)

# ---------------------------------------------------------------------------
# SRP constants (HAP-specific, Section 5.6)
# ---------------------------------------------------------------------------

# HAP mandates the 3072-bit SRP group from RFC 5054.
# Generator.
SRP_GENERATOR: int = 5

# 3072-bit prime (RFC 5054 Appendix A, 3072-bit group).
SRP_PRIME_3072: int = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF",
    16
)

# Hash function used by HAP-SRP (SHA-512).
SRP_HASH_NAME: str = "sha512"

# SRP salt length in bytes (HAP mandates 16 bytes).
SRP_SALT_LENGTH: int = 16

# ---------------------------------------------------------------------------
# HAP-BLE GATT service UUID
#
# Accessories expose a single GATT service with this UUID.
# All HAP characteristics live under it.
# ---------------------------------------------------------------------------

# The HAP-BLE GATT service UUID (same as SERVICE_PAIRING base).
GATT_HAP_SERVICE: str = SERVICE_PAIRING

# Instance ID for the Pairing Service (always 1 per spec).
IID_PAIRING_SETUP: int = 0x0002
IID_PAIRING_VERIFY: int = 0x0004
