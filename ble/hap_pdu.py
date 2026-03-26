"""HAP-BLE PDU framing — request and response construction/parsing.

Every HAP-BLE operation (characteristic read, write, subscribe) is
encoded as a HAP PDU written to and read from GATT characteristics.

Request PDU (Section 7.3.4.1)::

    ┌─────────┬────────┬─────┬─────┬────────┬──────┐
    │ Control │ Opcode │ TID │ IID │ Length │ Body │
    │ 1 byte  │ 1 byte │ 1B  │ 2B  │ 2 LE   │ var  │
    └─────────┴────────┴─────┴─────┴────────┴──────┘

Response PDU (Section 7.3.4.2)::

    ┌─────────┬─────┬────────┬────────┬──────┐
    │ Control │ TID │ Status │ Length │ Body │
    │ 1 byte  │ 1B  │ 2 LE   │ 2 LE  │ var  │
    └─────────┴─────┴────────┴────────┴──────┘

Body is a TLV8 payload (characteristic value, error detail, etc.).

Large PDUs that exceed the BLE MTU are fragmented at the GATT layer.
The Control byte's continuation bit distinguishes fragments.

Transaction IDs (TIDs) are 1 byte, cycling 0–255.  The controller
assigns TIDs; the accessory echoes them in responses.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import struct
from dataclasses import dataclass
from typing import Optional

from .hap_constants import (
    OPCODE_CHAR_READ,
    OPCODE_CHAR_SUBSCRIBE,
    OPCODE_CHAR_UNSUBSCRIBE,
    OPCODE_CHAR_WRITE,
    OPCODE_SERVICE_SIGNATURE_READ,
    PDU_FRAGMENT_CONTINUATION,
    PDU_FRAGMENT_FIRST,
    PDU_TYPE_REQUEST,
    PDU_TYPE_RESPONSE,
    STATUS_DESCRIPTIONS,
    STATUS_SUCCESS,
)

logger: logging.Logger = logging.getLogger("glowup.ble.hap_pdu")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Request header size: control(1) + opcode(1) + TID(1) + IID(2) = 5 bytes.
# Length field (2 bytes) is only present when there is a body.
REQUEST_HEADER_LEN: int = 5

# Response header size: control(1) + TID(1) + status(2) = 4 bytes.
# Length field (2 bytes) is only present when there is a body.
RESPONSE_HEADER_LEN: int = 4

# TID wraps at 256 (1 byte).
TID_MAX: int = 256

# HAP-BLE TLV types within PDU bodies (distinct from pairing TLV types).
# These identify characteristic value, additional authorization data, etc.
HAP_PARAM_VALUE: int = 0x01               # Characteristic value.
HAP_PARAM_ADDITIONAL_AUTH: int = 0x02      # Additional authorization data.
HAP_PARAM_ORIGIN: int = 0x03              # Origin (local vs remote).
HAP_PARAM_CHAR_TYPE: int = 0x04           # Characteristic type UUID.
HAP_PARAM_CHAR_INSTANCE_ID: int = 0x05    # Characteristic instance ID.
HAP_PARAM_SERVICE_TYPE: int = 0x06        # Service type UUID.
HAP_PARAM_SERVICE_INSTANCE_ID: int = 0x07  # Service instance ID.
HAP_PARAM_TTL: int = 0x08                 # Time-to-live.
HAP_PARAM_RETURN_RESPONSE: int = 0x09     # Request a response.
HAP_PARAM_HAP_CHAR_PROPERTIES: int = 0x0A  # HAP characteristic properties.
HAP_PARAM_GATT_USER_DESC: int = 0x0B      # GATT user description.
HAP_PARAM_GATT_FORMAT: int = 0x0C         # GATT presentation format.
HAP_PARAM_GATT_VALID_RANGE: int = 0x0D    # GATT valid range.
HAP_PARAM_HAP_STEP_VALUE: int = 0x0E      # HAP step value.


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HapRequest:
    """A HAP-BLE request PDU ready to write to a GATT characteristic.

    Attributes:
        opcode: Operation (read, write, subscribe, etc.).
        tid: Transaction ID (0–255).
        iid: Instance ID of the target characteristic.
        body: Optional TLV8-encoded body (for writes).
    """

    opcode: int
    tid: int
    iid: int
    body: Optional[bytes] = None

    def serialize(self) -> bytes:
        """Serialize to wire bytes.

        Returns:
            Complete PDU bytes ready for GATT write.
        """
        control: int = PDU_TYPE_REQUEST | PDU_FRAGMENT_FIRST
        header: bytes = struct.pack(
            "<BBBH",
            control,
            self.opcode,
            self.tid,
            self.iid,
        )

        if self.body:
            length_field: bytes = struct.pack("<H", len(self.body))
            return header + length_field + self.body
        return header


@dataclass
class HapResponse:
    """A parsed HAP-BLE response PDU.

    Attributes:
        tid: Transaction ID echoed from the request.
        status: Status code (0 = success).
        body: Optional TLV8-encoded body (characteristic value, etc.).
    """

    tid: int
    status: int
    body: Optional[bytes] = None

    @property
    def ok(self) -> bool:
        """True if the status indicates success."""
        return self.status == STATUS_SUCCESS

    @property
    def status_description(self) -> str:
        """Human-readable status string."""
        return STATUS_DESCRIPTIONS.get(self.status, f"Unknown (0x{self.status:04X})")


# ---------------------------------------------------------------------------
# Transaction ID allocator
# ---------------------------------------------------------------------------

class TidAllocator:
    """Allocates monotonically increasing transaction IDs (0–255, wrapping).

    Thread-safety is not needed — HAP-BLE is single-threaded by
    design (one outstanding request at a time per session).
    """

    def __init__(self) -> None:
        self._next: int = 0

    def allocate(self) -> int:
        """Return the next TID and advance the counter."""
        tid: int = self._next
        self._next = (self._next + 1) % TID_MAX
        return tid

    def reset(self) -> None:
        """Reset the counter (e.g., on new connection)."""
        self._next = 0


# ---------------------------------------------------------------------------
# PDU construction helpers
# ---------------------------------------------------------------------------

def build_read_request(tid: int, iid: int) -> bytes:
    """Build a characteristic read request PDU.

    Args:
        tid: Transaction ID.
        iid: Characteristic instance ID.

    Returns:
        Serialized PDU bytes.
    """
    return HapRequest(
        opcode=OPCODE_CHAR_READ,
        tid=tid,
        iid=iid,
    ).serialize()


def build_write_request(tid: int, iid: int, body: bytes) -> bytes:
    """Build a characteristic write request PDU.

    Args:
        tid: Transaction ID.
        iid: Characteristic instance ID.
        body: TLV8-encoded value to write.

    Returns:
        Serialized PDU bytes.
    """
    return HapRequest(
        opcode=OPCODE_CHAR_WRITE,
        tid=tid,
        iid=iid,
        body=body,
    ).serialize()


def build_subscribe_request(tid: int, iid: int) -> bytes:
    """Build a characteristic event subscription request PDU.

    Args:
        tid: Transaction ID.
        iid: Characteristic instance ID to subscribe to.

    Returns:
        Serialized PDU bytes.
    """
    return HapRequest(
        opcode=OPCODE_CHAR_SUBSCRIBE,
        tid=tid,
        iid=iid,
    ).serialize()


def build_unsubscribe_request(tid: int, iid: int) -> bytes:
    """Build a characteristic event unsubscription request PDU.

    Args:
        tid: Transaction ID.
        iid: Characteristic instance ID.

    Returns:
        Serialized PDU bytes.
    """
    return HapRequest(
        opcode=OPCODE_CHAR_UNSUBSCRIBE,
        tid=tid,
        iid=iid,
    ).serialize()


def build_service_signature_read(tid: int, iid: int) -> bytes:
    """Build a service signature read request PDU.

    Used during accessory discovery to enumerate characteristics
    and their properties within a service.

    Args:
        tid: Transaction ID.
        iid: Service instance ID.

    Returns:
        Serialized PDU bytes.
    """
    return HapRequest(
        opcode=OPCODE_SERVICE_SIGNATURE_READ,
        tid=tid,
        iid=iid,
    ).serialize()


# ---------------------------------------------------------------------------
# PDU parsing
# ---------------------------------------------------------------------------

def parse_response(data: bytes) -> HapResponse:
    """Parse a HAP-BLE response PDU from raw GATT notification bytes.

    Handles both bodyless responses (4 bytes) and responses with a
    TLV8 body (6+ bytes including the length field).

    Args:
        data: Raw bytes received from GATT notification/read.

    Returns:
        Parsed :class:`HapResponse`.

    Raises:
        ValueError: If the data is too short or structurally invalid.
    """
    if len(data) < RESPONSE_HEADER_LEN:
        raise ValueError(
            f"HAP response too short: expected >= {RESPONSE_HEADER_LEN} "
            f"bytes, got {len(data)}"
        )

    control: int = data[0]
    tid: int = data[1]
    status: int = struct.unpack_from("<H", data, 2)[0]

    body: Optional[bytes] = None

    # If there are more bytes after the header, parse the length + body.
    if len(data) > RESPONSE_HEADER_LEN:
        if len(data) < RESPONSE_HEADER_LEN + 2:
            raise ValueError(
                "HAP response has trailing bytes but not enough for "
                "a length field"
            )
        body_len: int = struct.unpack_from("<H", data, RESPONSE_HEADER_LEN)[0]
        body_start: int = RESPONSE_HEADER_LEN + 2
        body_end: int = body_start + body_len

        if body_end > len(data):
            raise ValueError(
                f"HAP response body truncated: declared {body_len} bytes, "
                f"only {len(data) - body_start} available"
            )
        body = data[body_start:body_end]

    if status != STATUS_SUCCESS:
        desc: str = STATUS_DESCRIPTIONS.get(
            status, f"0x{status:04X}"
        )
        logger.warning("HAP response TID=%d status: %s", tid, desc)

    return HapResponse(tid=tid, status=status, body=body)


def is_continuation(data: bytes) -> bool:
    """Check if a PDU fragment is a continuation (not the first fragment).

    Args:
        data: Raw GATT notification bytes.

    Returns:
        True if the continuation bit is set in the control byte.
    """
    if not data:
        return False
    return bool(data[0] & PDU_FRAGMENT_CONTINUATION)


def reassemble_fragments(fragments: list[bytes]) -> bytes:
    """Reassemble fragmented PDU data from multiple GATT notifications.

    The first fragment contains the full header.  Continuation
    fragments contain only body data (the control byte is stripped).

    Args:
        fragments: Ordered list of raw GATT notification bytes.
            The first element must be the first fragment.

    Returns:
        Reassembled PDU bytes (header + complete body).
    """
    if not fragments:
        return b""

    # First fragment is the header + start of body.
    result: bytearray = bytearray(fragments[0])

    # Continuations carry only body bytes (after the control byte).
    for frag in fragments[1:]:
        if len(frag) > 1:
            # Skip the control byte of continuation fragments.
            result.extend(frag[1:])

    return bytes(result)
