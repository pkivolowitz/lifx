"""HAP-BLE session — pair-setup, pair-verify, and encrypted I/O.

Orchestrates the full HAP-BLE lifecycle from initial pairing through
encrypted characteristic access.  Protocol details verified against
homekit_python source and live ONVIS SMS2 testing (2026-03-25).

Wire format (ONVIS SMS2, confirmed working):

    Write PDU:
        control(0x00) | opcode(1B) | TID(1B) | IID(2B LE)
        | body_len(2B LE) | body_tlv

    Body TLV wraps value in:
        ParamReturnResponse(0x09, b'\\x01') + Value(0x01, payload)

    Read response:
        control(0x02) | TID(1B) | status(1B) | body_len(2B LE) | body_tlv

    Response body TLV contains:
        Value(0x01, response_payload)

    ChaCha20 nonce for pair-setup/verify:
        b'\\x00\\x00\\x00\\x00' + label  (e.g., b'PS-Msg05')
        NOT label + zeros — the constant comes FIRST.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import logging
import struct
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from . import tlv
from .crypto import (
    build_pairing_nonce,
    derive_pair_setup_accessory_sign_key,
    derive_pair_setup_controller_sign_key,
    derive_pair_setup_encrypt_key,
    derive_pair_verify_encrypt_key,
    derive_session_keys,
    encrypt,
    decrypt,
)
from .hap_constants import (
    CHAR_PAIR_SETUP,
    CHAR_PAIR_VERIFY,
    ERROR_DESCRIPTIONS,
    METHOD_PAIR_SETUP,
    OPCODE_CHAR_WRITE,
    TLV_ENCRYPTED_DATA,
    TLV_ERROR,
    TLV_IDENTIFIER,
    TLV_METHOD,
    TLV_PROOF,
    TLV_PUBLIC_KEY,
    TLV_SALT,
    TLV_SIGNATURE,
    TLV_STATE,
)
from .hap_pdu import HAP_PARAM_VALUE, HAP_PARAM_RETURN_RESPONSE
from .srp import SrpClient, to_byte_array

logger: logging.Logger = logging.getLogger("glowup.ble.hap_session")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Controller pairing identifier — identifies GlowUp to the accessory.
CONTROLLER_PAIRING_ID: bytes = b"GlowUp"

# IID descriptor UUID — contains the HAP instance ID for each characteristic.
IID_DESCRIPTOR_UUID: str = "dc46f0fe-81d2-4616-b5d9-6abdd796939a"


# ---------------------------------------------------------------------------
# GATT client protocol
# ---------------------------------------------------------------------------

class GattClient(Protocol):
    """Protocol for BLE GATT read/write operations."""

    async def read_characteristic(self, uuid: str) -> bytes:
        """Read a GATT characteristic value by UUID."""
        ...

    async def write_characteristic(
        self, uuid: str, data: bytes, response: bool = True
    ) -> None:
        """Write data to a GATT characteristic by UUID."""
        ...

    async def start_notify(
        self, uuid: str, callback: Callable[[int, bytearray], None]
    ) -> None:
        """Subscribe to GATT notifications on a characteristic."""
        ...

    async def stop_notify(self, uuid: str) -> None:
        """Unsubscribe from GATT notifications on a characteristic."""
        ...


# ---------------------------------------------------------------------------
# Pairing result
# ---------------------------------------------------------------------------

@dataclass
class PairingKeys:
    """Long-term keys exchanged during pair-setup."""

    controller_ltsk: bytes
    controller_ltpk: bytes
    accessory_ltpk: bytes
    accessory_pairing_id: bytes


# ---------------------------------------------------------------------------
# HAP Session
# ---------------------------------------------------------------------------

class HapSession:
    """High-level HAP-BLE session manager.

    Orchestrates pair-setup, pair-verify, and encrypted characteristic
    access over a BLE GATT connection.
    """

    def __init__(self, gatt: GattClient) -> None:
        self._gatt: GattClient = gatt
        self._tid_counter: int = 0
        self._iid_pair_setup: int = 0
        self._iid_pair_verify: int = 0

    def _next_tid(self) -> int:
        """Allocate the next transaction ID (0–255, wrapping)."""
        tid: int = self._tid_counter
        self._tid_counter = (self._tid_counter + 1) % 256
        return tid

    # ------------------------------------------------------------------
    # IID Discovery
    # ------------------------------------------------------------------

    async def discover_iids(self) -> None:
        """Discover HAP characteristic instance IDs from GATT descriptors.

        Reads the IID descriptor (dc46f0fe-...) on the Pair Setup and
        Pair Verify characteristics.  Falls back to GATT handle if
        descriptor read fails.
        """
        client = getattr(self._gatt, "_client", None)
        if client is None:
            logger.warning("No bleak client — cannot discover IIDs")
            return

        for service in client.services:
            for char in service.characteristics:
                uuid_upper: str = char.uuid.upper()
                if uuid_upper == CHAR_PAIR_SETUP.upper():
                    self._iid_pair_setup = await self._read_iid(
                        client, char
                    )
                elif uuid_upper == CHAR_PAIR_VERIFY.upper():
                    self._iid_pair_verify = await self._read_iid(
                        client, char
                    )

        logger.info(
            "HAP IIDs: pair_setup=0x%04X, pair_verify=0x%04X",
            self._iid_pair_setup, self._iid_pair_verify,
        )

    async def _read_iid(self, client, char) -> int:
        """Read a characteristic's IID from its descriptor."""
        for desc in char.descriptors:
            if desc.uuid.lower() == IID_DESCRIPTOR_UUID.lower():
                try:
                    val: bytearray = await client.read_gatt_descriptor(
                        desc.handle
                    )
                    return int.from_bytes(val, byteorder="little")
                except Exception as exc:
                    logger.warning(
                        "IID descriptor read failed for %s: %s — "
                        "using GATT handle %d",
                        char.uuid, exc, char.handle,
                    )
                    return char.handle
        return char.handle

    # ------------------------------------------------------------------
    # HAP PDU I/O
    # ------------------------------------------------------------------

    def _build_write_pdu(
        self, tid: int, iid: int, payload_tlv: bytes
    ) -> bytes:
        """Build a HAP-BLE write PDU.

        Format: control(0x00) | opcode(CHAR_WRITE) | TID | IID(LE)
                | body_len(LE) | body

        Body wraps the payload TLV in ParamReturnResponse + Value.
        """
        body: bytes = tlv.encode([
            (HAP_PARAM_RETURN_RESPONSE, b"\x01"),
            (HAP_PARAM_VALUE, payload_tlv),
        ])
        header: bytes = struct.pack(
            "<BBBHH", 0x00, OPCODE_CHAR_WRITE, tid, iid, len(body)
        )
        return header + body

    async def _hap_write_read(
        self,
        char_uuid: str,
        iid: int,
        payload_tlv: bytes,
        context: str,
    ) -> dict[int, bytes]:
        """Write a HAP PDU and read the response.

        Handles response fragmentation (multiple GATT reads for large
        responses like SRP public keys).

        Args:
            char_uuid: GATT characteristic UUID.
            iid: HAP instance ID.
            payload_tlv: TLV payload to write.
            context: Label for logging.

        Returns:
            Decoded TLV dict from the response Value parameter.

        Raises:
            HapError: On protocol errors.
        """
        import asyncio

        tid: int = self._next_tid()
        pdu: bytes = self._build_write_pdu(tid, iid, payload_tlv)

        await self._gatt.write_characteristic(char_uuid, pdu, response=True)

        # Wait for accessory to process and update characteristic value.
        await asyncio.sleep(2)

        # Read response — may be fragmented across multiple reads.
        resp: bytearray = bytearray(
            await self._gatt.read_characteristic(char_uuid)
        )

        # Response format: control(1) + TID(1) + status(1) + body_len(2) + body
        if len(resp) >= 5:
            expected_body_len: int = struct.unpack_from("<H", resp, 3)[0]
            # Read continuation fragments until we have the full body.
            for _ in range(50):
                if len(resp) - 5 >= expected_body_len:
                    break
                await asyncio.sleep(0.5)
                frag: bytes = await self._gatt.read_characteristic(char_uuid)
                resp.extend(frag)

        # Parse response header.
        if len(resp) < 3:
            raise HapError(f"{context}: response too short ({len(resp)} bytes)")

        status: int = resp[2]
        if status != 0:
            desc: str = ERROR_DESCRIPTIONS.get(
                status, f"0x{status:02X}"
            )
            raise HapError(f"{context}: status error — {desc}")

        # Extract body TLV.
        body: bytes = bytes(resp[5:]) if len(resp) > 5 else b""
        body_tlv: dict[int, bytes] = tlv.decode_dict(body)

        # Unwrap Value parameter.
        if HAP_PARAM_VALUE in body_tlv:
            return tlv.decode_dict(body_tlv[HAP_PARAM_VALUE])
        return body_tlv

    # ------------------------------------------------------------------
    # Pair Setup (M1–M6)
    # ------------------------------------------------------------------

    async def pair_setup(self, setup_code: str) -> PairingKeys:
        """Execute the full pair-setup flow.

        Args:
            setup_code: The 8-digit code with dashes, e.g. ``"164-77-432"``.

        Returns:
            :class:`PairingKeys` — must be persisted for future sessions.

        Raises:
            HapError: If any step fails.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import (
            ChaCha20Poly1305,
        )

        # Discover IIDs if not already done.
        if self._iid_pair_setup == 0:
            await self.discover_iids()

        iid: int = self._iid_pair_setup
        uuid: str = CHAR_PAIR_SETUP

        # Use homekit_python-compatible SRP client.
        srp = SrpClient("Pair-Setup", setup_code)

        logger.info("Pair-setup: starting SRP exchange")

        # --- M1 → M2: SRP Start ---
        m1_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x01"),
            (TLV_METHOD, bytes([METHOD_PAIR_SETUP])),
        ])
        m2: dict[int, bytes] = await self._hap_write_read(
            uuid, iid, m1_tlv, "pair-setup M1→M2"
        )
        _check_error(m2, "pair-setup M2")

        srp.set_salt(m2[TLV_SALT])
        srp.set_server_public_key(m2[TLV_PUBLIC_KEY])
        logger.info("Pair-setup: M2 received (salt + server public key)")

        # --- M3 → M4: SRP Verify ---
        A_bytes: bytes = bytes(to_byte_array(srp.get_public_key()))
        M_bytes: bytes = bytes(to_byte_array(srp.get_proof()))

        m3_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x03"),
            (TLV_PUBLIC_KEY, A_bytes),
            (TLV_PROOF, M_bytes),
        ])
        m4: dict[int, bytes] = await self._hap_write_read(
            uuid, iid, m3_tlv, "pair-setup M3→M4"
        )
        _check_error(m4, "pair-setup M4")

        server_proof: bytes = m4[TLV_PROOF]
        if not srp.verify_server_proof(server_proof):
            raise HapError("Pair-setup M4: server proof verification failed")

        logger.info("Pair-setup: SRP verified — exchanging long-term keys")

        # --- M5 → M6: Key Exchange ---
        # Session key as minimum-byte representation (homekit_python compat).
        session_key: bytes = bytes(to_byte_array(srp.get_session_key()))

        # Derive encryption and signing keys.
        encrypt_key: bytes = derive_pair_setup_encrypt_key(session_key)
        controller_sign_key: bytes = derive_pair_setup_controller_sign_key(
            session_key
        )

        # Generate controller Ed25519 key pair.
        ltsk_obj = Ed25519PrivateKey.generate()
        ltpk: bytes = ltsk_obj.public_key().public_bytes_raw()
        ltsk: bytes = ltsk_obj.private_bytes_raw()

        # Sign: controller_sign_key || pairing_id || ltpk
        device_info: bytes = controller_sign_key + CONTROLLER_PAIRING_ID + ltpk
        signature: bytes = ltsk_obj.sign(device_info)

        # Build sub-TLV (inside the encrypted envelope).
        sub_tlv: bytes = tlv.encode([
            (TLV_IDENTIFIER, CONTROLLER_PAIRING_ID),
            (TLV_PUBLIC_KEY, ltpk),
            (TLV_SIGNATURE, signature),
        ])

        # Encrypt with correct nonce: 4 zero bytes + "PS-Msg05".
        nonce_m5: bytes = build_pairing_nonce(b"PS-Msg05")
        encrypted: bytes = ChaCha20Poly1305(encrypt_key).encrypt(
            nonce_m5, sub_tlv, bytes()
        )

        m5_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x05"),
            (TLV_ENCRYPTED_DATA, encrypted),
        ])
        m6: dict[int, bytes] = await self._hap_write_read(
            uuid, iid, m5_tlv, "pair-setup M5→M6"
        )
        _check_error(m6, "pair-setup M6")

        # Decrypt M6.
        nonce_m6: bytes = build_pairing_nonce(b"PS-Msg06")
        m6_decrypted: bytes = ChaCha20Poly1305(encrypt_key).decrypt(
            nonce_m6, m6[TLV_ENCRYPTED_DATA], bytes()
        )
        m6_sub: dict[int, bytes] = tlv.decode_dict(m6_decrypted)

        accessory_id: bytes = m6_sub[TLV_IDENTIFIER]
        accessory_ltpk: bytes = m6_sub[TLV_PUBLIC_KEY]
        accessory_sig: bytes = m6_sub[TLV_SIGNATURE]

        # Verify accessory signature.
        accessory_sign_key: bytes = derive_pair_setup_accessory_sign_key(
            session_key
        )
        accessory_info: bytes = (
            accessory_sign_key + accessory_id + accessory_ltpk
        )
        Ed25519PublicKey.from_public_bytes(accessory_ltpk).verify(
            accessory_sig, accessory_info
        )

        logger.info(
            "Pair-setup complete — accessory: %s",
            accessory_id.decode("utf-8", errors="replace"),
        )

        return PairingKeys(
            controller_ltsk=ltsk,
            controller_ltpk=ltpk,
            accessory_ltpk=accessory_ltpk,
            accessory_pairing_id=accessory_id,
        )

    # ------------------------------------------------------------------
    # Pair Verify (M1–M4)
    # ------------------------------------------------------------------

    async def pair_verify(self, keys: PairingKeys) -> None:
        """Establish an encrypted session using persisted keys.

        Args:
            keys: Long-term keys from a previous :meth:`pair_setup`.

        Raises:
            HapError: If verification fails.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )
        from cryptography.hazmat.primitives.ciphers.aead import (
            ChaCha20Poly1305,
        )

        if self._iid_pair_verify == 0:
            await self.discover_iids()

        iid: int = self._iid_pair_verify
        uuid: str = CHAR_PAIR_VERIFY

        logger.info("Pair-verify: starting Curve25519 exchange")

        # Generate ephemeral key pair.
        ephemeral_sk = X25519PrivateKey.generate()
        ephemeral_pk: bytes = ephemeral_sk.public_key().public_bytes_raw()

        # --- M1 → M2: Start ---
        m1_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x01"),
            (TLV_PUBLIC_KEY, ephemeral_pk),
        ])
        m2: dict[int, bytes] = await self._hap_write_read(
            uuid, iid, m1_tlv, "pair-verify M1→M2"
        )
        _check_error(m2, "pair-verify M2")

        accessory_pk_bytes: bytes = m2[TLV_PUBLIC_KEY]
        m2_encrypted: bytes = m2[TLV_ENCRYPTED_DATA]

        # Compute shared secret.
        accessory_pk = X25519PublicKey.from_public_bytes(accessory_pk_bytes)
        shared_secret: bytes = ephemeral_sk.exchange(accessory_pk)

        # Derive verify encryption key.
        verify_key: bytes = derive_pair_verify_encrypt_key(shared_secret)

        # Decrypt M2 sub-TLV.
        nonce_m2: bytes = build_pairing_nonce(b"PV-Msg02")
        m2_dec: bytes = ChaCha20Poly1305(verify_key).decrypt(
            nonce_m2, m2_encrypted, bytes()
        )
        m2_sub: dict[int, bytes] = tlv.decode_dict(m2_dec)

        # Verify accessory signature.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from cryptography.exceptions import InvalidSignature

        accessory_info: bytes = (
            accessory_pk_bytes
            + m2_sub[TLV_IDENTIFIER]
            + ephemeral_pk
        )
        try:
            Ed25519PublicKey.from_public_bytes(keys.accessory_ltpk).verify(
                m2_sub[TLV_SIGNATURE], accessory_info
            )
        except InvalidSignature:
            raise HapError("Pair-verify M2: accessory signature invalid")

        logger.info("Pair-verify: M2 verified — sending controller proof")

        # --- M3 → M4: Finish ---
        controller_info: bytes = (
            ephemeral_pk + CONTROLLER_PAIRING_ID + accessory_pk_bytes
        )
        controller_ltsk = Ed25519PrivateKey.from_private_bytes(
            keys.controller_ltsk
        )
        controller_sig: bytes = controller_ltsk.sign(controller_info)

        sub_tlv: bytes = tlv.encode([
            (TLV_IDENTIFIER, CONTROLLER_PAIRING_ID),
            (TLV_SIGNATURE, controller_sig),
        ])
        nonce_m3: bytes = build_pairing_nonce(b"PV-Msg03")
        encrypted: bytes = ChaCha20Poly1305(verify_key).encrypt(
            nonce_m3, sub_tlv, bytes()
        )

        m3_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x03"),
            (TLV_ENCRYPTED_DATA, encrypted),
        ])
        m4: dict[int, bytes] = await self._hap_write_read(
            uuid, iid, m3_tlv, "pair-verify M3→M4"
        )
        _check_error(m4, "pair-verify M4")

        # Derive session keys.
        c2a_key, a2c_key = derive_session_keys(shared_secret)
        logger.info("Pair-verify complete — encrypted session established")

        # Session keys derived but not yet stored — encrypted characteristic
        # I/O requires persisting c2a_key/a2c_key for subscriptions.
        # Tracked in project backlog, not blocking current BLE scanning use.


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HapError(Exception):
    """Raised when a HAP protocol operation fails."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_error(tlv_dict: dict[int, bytes], context: str) -> None:
    """Raise HapError if the TLV response contains an error code."""
    error_bytes: Optional[bytes] = tlv_dict.get(TLV_ERROR)
    if error_bytes is not None:
        code: int = error_bytes[0]
        desc: str = ERROR_DESCRIPTIONS.get(code, f"0x{code:02X}")
        raise HapError(f"{context}: accessory error — {desc}")
