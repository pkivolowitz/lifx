"""HAP-BLE session — pair-setup, pair-verify, and encrypted I/O.

Orchestrates the full HAP-BLE lifecycle from initial pairing through
encrypted characteristic access.  This is the top-level protocol
module — it uses :mod:`ble.srp`, :mod:`ble.crypto`, :mod:`ble.tlv`,
and :mod:`ble.hap_pdu` internally.

Lifecycle::

    1. pair_setup()   — One-time.  Exchanges long-term Ed25519 keys
                        with the accessory using SRP-6a.  Persists
                        keys to ble_pairing.json.

    2. pair_verify()  — Per-connection.  Establishes an encrypted
                        session using Curve25519 + persisted keys.

    3. read/write/subscribe — Encrypted characteristic operations
                        using the session keys from pair_verify.

All BLE I/O goes through a :class:`GattClient` abstraction that
wraps bleak's read/write/notify operations.  This keeps the protocol
logic testable without hardware.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from . import tlv
from .crypto import (
    CHACHA_TAG_LEN,
    decrypt,
    derive_pair_setup_accessory_sign_key,
    derive_pair_setup_controller_sign_key,
    derive_pair_setup_encrypt_key,
    derive_pair_verify_encrypt_key,
    derive_session_keys,
    encrypt,
)
from .hap_constants import (
    CHAR_PAIR_SETUP,
    CHAR_PAIR_VERIFY,
    ERR_AUTHENTICATION,
    ERROR_DESCRIPTIONS,
    METHOD_PAIR_SETUP,
    METHOD_PAIR_VERIFY,
    TLV_CERTIFICATE,
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
from .hap_pdu import (
    HapResponse,
    TidAllocator,
    build_read_request,
    build_subscribe_request,
    build_write_request,
    parse_response,
)
from .srp import SrpClient

logger: logging.Logger = logging.getLogger("glowup.ble.hap_session")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Ed25519 key sizes (bytes).
ED25519_PUBLIC_KEY_LEN: int = 32
ED25519_PRIVATE_KEY_LEN: int = 32
ED25519_SIGNATURE_LEN: int = 64

# Curve25519 key size (bytes).
X25519_KEY_LEN: int = 32

# Controller pairing identifier — identifies GlowUp to the accessory.
CONTROLLER_PAIRING_ID: bytes = b"GlowUp"


# ---------------------------------------------------------------------------
# GATT client protocol
#
# Abstraction over bleak so the session logic is testable without
# hardware.  The real implementation is in scanner.py.
# ---------------------------------------------------------------------------

class GattClient(Protocol):
    """Protocol for BLE GATT read/write operations.

    Any object implementing these methods can be used with
    :class:`HapSession` — bleak's ``BleakClient`` wrapped in a thin
    adapter, or a mock for testing.
    """

    async def read_characteristic(self, uuid: str) -> bytes:
        """Read the value of a GATT characteristic by UUID."""
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
        """Unsubscribe from GATT notifications."""
        ...


# ---------------------------------------------------------------------------
# Pairing result
# ---------------------------------------------------------------------------

@dataclass
class PairingKeys:
    """Long-term keys exchanged during pair-setup.

    These must be persisted — they're used for all future pair-verify
    connections.  Losing them requires re-running pair-setup with the
    physical setup code.

    Attributes:
        controller_ltsk: Our Ed25519 private key (32 bytes).
        controller_ltpk: Our Ed25519 public key (32 bytes).
        accessory_ltpk: Accessory's Ed25519 public key (32 bytes).
        accessory_pairing_id: Accessory's pairing identifier.
    """

    controller_ltsk: bytes
    controller_ltpk: bytes
    accessory_ltpk: bytes
    accessory_pairing_id: bytes


@dataclass
class EncryptedSession:
    """Active encrypted session after pair-verify.

    Holds the per-connection keys and nonce counters for encrypting
    and decrypting HAP-BLE PDUs.

    Attributes:
        c2a_key: Controller-to-accessory encryption key (32 bytes).
        a2c_key: Accessory-to-controller decryption key (32 bytes).
        c2a_counter: Outgoing nonce counter.
        a2c_counter: Incoming nonce counter.
    """

    c2a_key: bytes
    a2c_key: bytes
    c2a_counter: int = 0
    a2c_counter: int = 0

    def encrypt_request(self, plaintext: bytes) -> bytes:
        """Encrypt an outgoing PDU body.

        Increments the outgoing counter after encryption.

        Args:
            plaintext: PDU body bytes to encrypt.

        Returns:
            Ciphertext with appended 16-byte auth tag.
        """
        ct: bytes = encrypt(self.c2a_key, self.c2a_counter, plaintext)
        self.c2a_counter += 1
        return ct

    def decrypt_response(self, ciphertext_with_tag: bytes) -> bytes:
        """Decrypt an incoming PDU body.

        Increments the incoming counter after decryption.

        Args:
            ciphertext_with_tag: Ciphertext with 16-byte auth tag.

        Returns:
            Decrypted plaintext.

        Raises:
            cryptography.exceptions.InvalidTag: On decryption failure.
        """
        pt: bytes = decrypt(self.a2c_key, self.a2c_counter, ciphertext_with_tag)
        self.a2c_counter += 1
        return pt


# ---------------------------------------------------------------------------
# HAP Session
# ---------------------------------------------------------------------------

class HapSession:
    """High-level HAP-BLE session manager.

    Orchestrates pair-setup, pair-verify, and encrypted characteristic
    access over a BLE GATT connection.

    Usage::

        session = HapSession(gatt_client)

        # First time — pair with setup code:
        keys = await session.pair_setup(b"164-77-432")
        persist(keys)  # Save to ble_pairing.json.

        # Subsequent connections — verify with persisted keys:
        await session.pair_verify(keys)

        # Now read/write/subscribe to characteristics:
        value = await session.read_characteristic(iid=0x0010)
        await session.subscribe(iid=0x0010, callback=on_motion)
    """

    def __init__(self, gatt: GattClient) -> None:
        """Initialize with a connected GATT client.

        Args:
            gatt: Connected BLE GATT client (bleak wrapper or mock).
        """
        self._gatt: GattClient = gatt
        self._tid: TidAllocator = TidAllocator()
        self._session: Optional[EncryptedSession] = None

        # HAP-BLE characteristic instance IDs.  Discovered at runtime
        # by reading the IID descriptor on each characteristic.
        # These are needed for HAP PDU framing (every request includes
        # the target characteristic's IID).
        self._iid_pair_setup: int = 0
        self._iid_pair_verify: int = 0
        self._iids_discovered: bool = False

    async def discover_iids(self) -> None:
        """Discover HAP characteristic instance IDs from GATT descriptors.

        HAP-BLE uses a custom descriptor (dc46f0fe-81d2-4616-b5d9-6abdd796939a)
        on each characteristic to declare its instance ID.  These IIDs are
        required in HAP PDU framing.

        Must be called before pair_setup() or pair_verify().
        """
        # The IID descriptor UUID used by HAP-BLE accessories.
        IID_DESCRIPTOR_UUID: str = "dc46f0fe-81d2-4616-b5d9-6abdd796939a"

        try:
            iids: dict[str, int] = await self._gatt.read_iid_descriptors(
                CHAR_PAIR_SETUP, CHAR_PAIR_VERIFY,
                iid_descriptor_uuid=IID_DESCRIPTOR_UUID,
            )
            self._iid_pair_setup = iids.get(CHAR_PAIR_SETUP, 0)
            self._iid_pair_verify = iids.get(CHAR_PAIR_VERIFY, 0)
        except (AttributeError, NotImplementedError):
            # Fallback: read descriptors manually via the GATT client.
            self._iid_pair_setup = await self._read_iid(
                CHAR_PAIR_SETUP, IID_DESCRIPTOR_UUID
            )
            self._iid_pair_verify = await self._read_iid(
                CHAR_PAIR_VERIFY, IID_DESCRIPTOR_UUID
            )

        self._iids_discovered = True
        logger.info(
            "HAP IIDs discovered: pair_setup=0x%04X, pair_verify=0x%04X",
            self._iid_pair_setup, self._iid_pair_verify,
        )

    async def _read_iid(self, char_uuid: str, desc_uuid: str) -> int:
        """Read a single characteristic's IID from its descriptor.

        Falls back to reading all descriptors via the GATT client's
        underlying bleak handle access.
        """
        # This requires direct bleak access — the GattClient protocol
        # doesn't have descriptor support.  Access the underlying client.
        client = getattr(self._gatt, "_client", None)
        if client is None:
            logger.warning("Cannot read IID descriptors — no bleak client")
            return 0

        for service in client.services:
            for char in service.characteristics:
                # bleak returns lowercase UUIDs; HAP constants are uppercase.
                if char.uuid.upper() == char_uuid.upper():
                    for desc in char.descriptors:
                        if desc.uuid.lower() == desc_uuid.lower():
                            try:
                                val: bytearray = (
                                    await client.read_gatt_descriptor(
                                        desc.handle
                                    )
                                )
                                iid: int = int.from_bytes(
                                    val, byteorder="little"
                                )
                                return iid
                            except Exception as exc:
                                logger.warning(
                                    "IID descriptor read failed for %s: "
                                    "%s — using GATT handle as IID",
                                    char_uuid, exc,
                                )
                                return char.handle
                    # No IID descriptor — use GATT handle.
                    logger.warning(
                        "No IID descriptor for %s — using handle %d",
                        char_uuid, char.handle,
                    )
                    return char.handle
        logger.warning("Characteristic %s not found", char_uuid)
        return 0

    @property
    def is_encrypted(self) -> bool:
        """True if pair-verify has succeeded and an encrypted session is active."""
        return self._session is not None

    # ------------------------------------------------------------------
    # Pair Setup (M1–M6)
    # ------------------------------------------------------------------

    async def pair_setup(self, setup_code: bytes) -> PairingKeys:
        """Execute the full pair-setup flow.

        This is a one-time operation that establishes long-term trust
        between GlowUp (controller) and the accessory.  After this,
        only pair-verify is needed.

        The accessory must be in pairing mode.  Most accessories enter
        pairing mode automatically when unpaired.

        Args:
            setup_code: The 8-digit code as bytes, e.g. ``b"164-77-432"``.

        Returns:
            :class:`PairingKeys` — must be persisted for future sessions.

        Raises:
            HapError: If any step fails (wrong code, accessory busy, etc.).
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        # Discover characteristic IIDs before any PDU I/O.
        if not self._iids_discovered:
            await self.discover_iids()

        logger.info("Pair-setup: starting SRP exchange")

        # Generate our long-term Ed25519 key pair.
        controller_ltsk_obj = Ed25519PrivateKey.generate()
        controller_ltpk_bytes: bytes = (
            controller_ltsk_obj.public_key().public_bytes_raw()
        )
        controller_ltsk_bytes: bytes = controller_ltsk_obj.private_bytes_raw()

        srp = SrpClient(setup_code)

        # --- M1: Controller → Accessory (SRP Start Request) ---
        m1_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x01"),
            (TLV_METHOD, bytes([METHOD_PAIR_SETUP])),
        ])
        await self._write_pairing(m1_tlv)

        # --- M2: Accessory → Controller (SRP Start Response) ---
        m2_raw: bytes = await self._read_pairing()
        m2: dict[int, bytes] = tlv.decode_dict(m2_raw)
        _check_state(m2, 2)
        _check_error(m2, "pair-setup M2")

        salt: bytes = m2[TLV_SALT]
        server_pk: bytes = m2[TLV_PUBLIC_KEY]
        srp.set_server_params(salt, server_pk)

        logger.info("Pair-setup: M2 received (salt + server public key)")

        # --- M3: Controller → Accessory (SRP Verify Request) ---
        m3_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x03"),
            (TLV_PUBLIC_KEY, srp.get_public_key()),
            (TLV_PROOF, srp.get_proof()),
        ])
        await self._write_pairing(m3_tlv)

        # --- M4: Accessory → Controller (SRP Verify Response) ---
        m4_raw: bytes = await self._read_pairing()
        m4: dict[int, bytes] = tlv.decode_dict(m4_raw)
        _check_state(m4, 4)
        _check_error(m4, "pair-setup M4")

        server_proof: bytes = m4[TLV_PROOF]
        if not srp.verify_server_proof(server_proof):
            raise HapError(
                "Pair-setup M4: server proof verification failed — "
                "wrong setup code?"
            )

        logger.info("Pair-setup: SRP verified — exchanging long-term keys")

        # Derive encryption key for M5/M6 exchange.
        session_key: bytes = srp.get_session_key()
        encrypt_key: bytes = derive_pair_setup_encrypt_key(session_key)

        # --- M5: Controller → Accessory (Exchange Request) ---
        # Sign: iOSDeviceX || iOSDevicePairingID || iOSDeviceLTPK
        ios_device_x: bytes = derive_pair_setup_controller_sign_key(
            session_key
        )
        ios_device_info: bytes = (
            ios_device_x + CONTROLLER_PAIRING_ID + controller_ltpk_bytes
        )
        ios_device_signature: bytes = controller_ltsk_obj.sign(ios_device_info)

        sub_tlv: bytes = tlv.encode([
            (TLV_IDENTIFIER, CONTROLLER_PAIRING_ID),
            (TLV_CERTIFICATE, controller_ltpk_bytes),
            (TLV_SIGNATURE, ios_device_signature),
        ])

        # Encrypt the sub-TLV with the pair-setup session key.
        # Nonce for M5 is "PS-Msg05" (HAP spec).
        encrypted_data: bytes = _encrypt_with_tag(
            encrypt_key, b"PS-Msg05", sub_tlv
        )

        m5_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x05"),
            (TLV_ENCRYPTED_DATA, encrypted_data),
        ])
        await self._write_pairing(m5_tlv)

        # --- M6: Accessory → Controller (Exchange Response) ---
        m6_raw: bytes = await self._read_pairing()
        m6: dict[int, bytes] = tlv.decode_dict(m6_raw)
        _check_state(m6, 6)
        _check_error(m6, "pair-setup M6")

        # Decrypt the accessory's sub-TLV.
        m6_encrypted: bytes = m6[TLV_ENCRYPTED_DATA]
        m6_decrypted: bytes = _decrypt_with_tag(
            encrypt_key, b"PS-Msg06", m6_encrypted
        )

        m6_sub: dict[int, bytes] = tlv.decode_dict(m6_decrypted)
        accessory_pairing_id: bytes = m6_sub[TLV_IDENTIFIER]
        accessory_ltpk: bytes = m6_sub[TLV_CERTIFICATE]
        accessory_signature: bytes = m6_sub[TLV_SIGNATURE]

        # Verify the accessory's signature.
        accessory_x: bytes = derive_pair_setup_accessory_sign_key(session_key)
        accessory_info: bytes = (
            accessory_x + accessory_pairing_id + accessory_ltpk
        )
        _verify_ed25519(accessory_ltpk, accessory_signature, accessory_info)

        logger.info(
            "Pair-setup complete — accessory ID: %s",
            accessory_pairing_id.decode("utf-8", errors="replace"),
        )

        return PairingKeys(
            controller_ltsk=controller_ltsk_bytes,
            controller_ltpk=controller_ltpk_bytes,
            accessory_ltpk=accessory_ltpk,
            accessory_pairing_id=accessory_pairing_id,
        )

    # ------------------------------------------------------------------
    # Pair Verify (M1–M4)
    # ------------------------------------------------------------------

    async def pair_verify(self, keys: PairingKeys) -> None:
        """Establish an encrypted session using persisted keys.

        Must be called at the start of every BLE connection after the
        initial pair-setup.  On success, all subsequent characteristic
        reads/writes are encrypted.

        Args:
            keys: Long-term keys from a previous :meth:`pair_setup`.

        Raises:
            HapError: If verification fails (keys revoked, wrong
                accessory, etc.).
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )

        logger.info("Pair-verify: starting Curve25519 exchange")

        # Discover characteristic IIDs before any PDU I/O.
        if not self._iids_discovered:
            await self.discover_iids()

        # Generate ephemeral Curve25519 key pair for this session.
        ephemeral_sk = X25519PrivateKey.generate()
        ephemeral_pk_bytes: bytes = ephemeral_sk.public_key().public_bytes_raw()

        # --- M1: Controller → Accessory (Start Request) ---
        m1_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x01"),
            (TLV_PUBLIC_KEY, ephemeral_pk_bytes),
        ])
        await self._write_verify(m1_tlv)

        # --- M2: Accessory → Controller (Start Response) ---
        m2_raw: bytes = await self._read_verify()
        m2: dict[int, bytes] = tlv.decode_dict(m2_raw)
        _check_state(m2, 2)
        _check_error(m2, "pair-verify M2")

        accessory_pk_bytes: bytes = m2[TLV_PUBLIC_KEY]
        m2_encrypted: bytes = m2[TLV_ENCRYPTED_DATA]

        # Compute the shared secret.
        accessory_ephemeral_pk = X25519PublicKey.from_public_bytes(
            accessory_pk_bytes
        )
        shared_secret: bytes = ephemeral_sk.exchange(accessory_ephemeral_pk)

        # Derive the session encryption key for M2/M3.
        verify_key: bytes = derive_pair_verify_encrypt_key(shared_secret)

        # Decrypt M2's sub-TLV.
        m2_decrypted: bytes = _decrypt_with_tag(
            verify_key, b"PV-Msg02", m2_encrypted
        )
        m2_sub: dict[int, bytes] = tlv.decode_dict(m2_decrypted)

        accessory_id: bytes = m2_sub[TLV_IDENTIFIER]
        accessory_signature: bytes = m2_sub[TLV_SIGNATURE]

        # Verify the accessory's signature over:
        # AccessoryX25519PK || AccessoryPairingID || ControllerX25519PK
        accessory_info: bytes = (
            accessory_pk_bytes + accessory_id + ephemeral_pk_bytes
        )
        _verify_ed25519(
            keys.accessory_ltpk, accessory_signature, accessory_info
        )

        logger.info("Pair-verify: M2 verified — sending controller proof")

        # --- M3: Controller → Accessory (Finish Request) ---
        # Sign: ControllerX25519PK || ControllerPairingID || AccessoryX25519PK
        controller_info: bytes = (
            ephemeral_pk_bytes + CONTROLLER_PAIRING_ID + accessory_pk_bytes
        )
        controller_ltsk = Ed25519PrivateKey.from_private_bytes(
            keys.controller_ltsk
        )
        controller_signature: bytes = controller_ltsk.sign(controller_info)

        sub_tlv: bytes = tlv.encode([
            (TLV_IDENTIFIER, CONTROLLER_PAIRING_ID),
            (TLV_SIGNATURE, controller_signature),
        ])
        encrypted_data: bytes = _encrypt_with_tag(
            verify_key, b"PV-Msg03", sub_tlv
        )

        m3_tlv: bytes = tlv.encode([
            (TLV_STATE, b"\x03"),
            (TLV_ENCRYPTED_DATA, encrypted_data),
        ])
        await self._write_verify(m3_tlv)

        # --- M4: Accessory → Controller (Finish Response) ---
        m4_raw: bytes = await self._read_verify()
        m4: dict[int, bytes] = tlv.decode_dict(m4_raw)
        _check_state(m4, 4)
        _check_error(m4, "pair-verify M4")

        # Derive the session keys for encrypted characteristic I/O.
        c2a_key, a2c_key = derive_session_keys(shared_secret)
        self._session = EncryptedSession(
            c2a_key=c2a_key,
            a2c_key=a2c_key,
        )

        logger.info("Pair-verify complete — encrypted session established")

    # ------------------------------------------------------------------
    # Encrypted characteristic operations
    # ------------------------------------------------------------------

    async def read_characteristic(self, iid: int) -> bytes:
        """Read a characteristic value over the encrypted session.

        Args:
            iid: Characteristic instance ID.

        Returns:
            Raw characteristic value bytes.

        Raises:
            HapError: If the session is not encrypted or the read fails.
        """
        self._require_encrypted()

        tid: int = self._tid.allocate()
        request: bytes = build_read_request(tid, iid)

        # Encrypt the request body (everything after the 5-byte header).
        encrypted_request: bytes = self._encrypt_pdu(request)
        await self._gatt.write_characteristic(
            CHAR_PAIR_VERIFY, encrypted_request, response=True
        )

        raw_response: bytes = await self._gatt.read_characteristic(
            CHAR_PAIR_VERIFY
        )
        response: HapResponse = parse_response(
            self._decrypt_pdu(raw_response)
        )

        if not response.ok:
            raise HapError(
                f"Characteristic read IID={iid} failed: "
                f"{response.status_description}"
            )

        return response.body or b""

    async def subscribe(
        self,
        iid: int,
        callback: Callable[[int, bytes], None],
    ) -> None:
        """Subscribe to characteristic event notifications.

        The callback receives ``(iid, value_bytes)`` on each event.
        For a motion sensor, this fires when occupancy changes.

        Args:
            iid: Characteristic instance ID.
            callback: Called with ``(iid, raw_value)`` on each event.

        Raises:
            HapError: If the session is not encrypted or subscribe fails.
        """
        self._require_encrypted()

        tid: int = self._tid.allocate()
        request: bytes = build_subscribe_request(tid, iid)
        encrypted_request: bytes = self._encrypt_pdu(request)
        await self._gatt.write_characteristic(
            CHAR_PAIR_VERIFY, encrypted_request, response=True
        )

        raw_response: bytes = await self._gatt.read_characteristic(
            CHAR_PAIR_VERIFY
        )
        response: HapResponse = parse_response(
            self._decrypt_pdu(raw_response)
        )

        if not response.ok:
            raise HapError(
                f"Subscribe IID={iid} failed: {response.status_description}"
            )

        # Set up GATT notification handler that decrypts and dispatches.
        async def _on_notify(handle: int, data: bytearray) -> None:
            try:
                decrypted: bytes = self._decrypt_pdu(bytes(data))
                resp: HapResponse = parse_response(decrypted)
                if resp.body:
                    callback(iid, resp.body)
            except Exception:
                logger.exception(
                    "Error processing notification for IID=%d", iid
                )

        await self._gatt.start_notify(CHAR_PAIR_VERIFY, _on_notify)
        logger.info("Subscribed to characteristic IID=%d", iid)

    # ------------------------------------------------------------------
    # Internal: GATT I/O for pairing characteristics
    #
    # HAP-BLE requires ALL characteristic access to go through HAP PDU
    # framing — even pair-setup/verify.  Each write is a HAP request
    # PDU (opcode + TID + IID + body), and each read returns a HAP
    # response PDU (TID + status + body).
    # ------------------------------------------------------------------

    async def _write_pairing(self, tlv_data: bytes) -> None:
        """Write TLV data to the Pair Setup characteristic via HAP PDU."""
        tid: int = self._tid.allocate()
        pdu: bytes = build_write_request(
            tid=tid, iid=self._iid_pair_setup, body=tlv_data
        )
        await self._gatt.write_characteristic(
            CHAR_PAIR_SETUP, pdu, response=True
        )

    async def _read_pairing(self) -> bytes:
        """Read raw TLV response from the Pair Setup characteristic.

        Pair-setup responses are raw TLV (not HAP PDU wrapped),
        potentially fragmented across multiple GATT reads.
        """
        return await self._read_raw_tlv(CHAR_PAIR_SETUP, "pair-setup")

    async def _write_verify(self, tlv_data: bytes) -> None:
        """Write TLV data to the Pair Verify characteristic via HAP PDU."""
        tid: int = self._tid.allocate()
        pdu: bytes = build_write_request(
            tid=tid, iid=self._iid_pair_verify, body=tlv_data
        )
        await self._gatt.write_characteristic(
            CHAR_PAIR_VERIFY, pdu, response=True
        )

    async def _read_verify(self) -> bytes:
        """Read raw TLV response from the Pair Verify characteristic."""
        return await self._read_raw_tlv(CHAR_PAIR_VERIFY, "pair-verify")

    async def _read_raw_tlv(
        self, char_uuid: str, context: str
    ) -> bytes:
        """Read a pairing response, assembling fragments.

        The ONVIS SMS2 returns pair-setup/verify responses as a HAP PDU
        with a 2-byte header (control + TID) followed by TLV body data.
        Large responses are fragmented: the first read contains the
        header + initial body bytes, continuation reads contain only
        body data.

        The response format appears to be:
            Control(1) + TID(1) + TLV body (variable, fragmented)

        No separate status or body-length fields for pairing responses.

        Args:
            char_uuid: GATT characteristic UUID to read from.
            context: Human-readable label for error messages.

        Returns:
            Complete TLV bytes (header stripped).
        """
        import struct

        # First read: PDU header + start of TLV body.
        raw: bytes = await self._gatt.read_characteristic(char_uuid)
        logger.info(
            "%s first read: %d bytes, hex: %s",
            context, len(raw), raw.hex(),
        )

        if len(raw) < 2:
            raise HapError(f"{context} response too short: {len(raw)} bytes")

        # Check for HAP PDU header: control byte with response bit set.
        has_pdu_header: bool = bool(raw[0] & 0x02)

        if has_pdu_header:
            # Strip 2-byte PDU header (control + TID).
            # Then check if bytes [2:4] look like a 2-byte status +
            # 2-byte body length (standard HAP PDU), or if they're
            # already TLV data.
            #
            # Standard HAP: control(1) + TID(1) + status(2) + len(2) + body
            # Pairing variant: control(1) + TID(1) + body (TLV directly)
            #
            # Detect by checking if bytes[2] is a valid TLV pairing type.
            PAIRING_TLV_TYPES: set[int] = {0x00, 0x01, 0x02, 0x03, 0x04,
                                            0x05, 0x06, 0x07, 0x09, 0x0A,
                                            0x13, 0xFF}
            if len(raw) > 2 and raw[2] in PAIRING_TLV_TYPES:
                # Byte 2 is a TLV type → no status/length fields.
                body_start: int = 2
            elif len(raw) >= 6:
                # Standard PDU: status(2) + body_length(2).
                status: int = struct.unpack_from("<H", raw, 2)[0]
                if status != 0:
                    raise HapError(
                        f"{context} PDU status error: 0x{status:04X}"
                    )
                body_start = 6
            else:
                body_start = 2
        else:
            # No PDU header — entire read is TLV.
            body_start = 0

        body: bytearray = bytearray(raw[body_start:])
        first_frag: bytes = raw  # For repeat detection.
        MAX_FRAGMENT_READS: int = 200
        reads: int = 0

        # Read continuation fragments.  Stop when we get repeated data
        # (the characteristic value hasn't changed) or an empty read.
        while reads < MAX_FRAGMENT_READS:
            frag: bytes = await self._gatt.read_characteristic(char_uuid)
            if not frag:
                break
            if bytes(frag) == bytes(first_frag):
                # Same data as first read — no new fragment.
                break
            body.extend(frag)
            reads += 1

        logger.info(
            "%s assembled %d TLV bytes from %d read(s), "
            "first TLV bytes: %s",
            context, len(body), reads + 1, body[:16].hex(),
        )
        return bytes(body)

    async def _read_fragmented(
        self, char_uuid: str, context: str
    ) -> bytes:
        """Read a potentially fragmented HAP-BLE response.

        The first GATT read returns the response header (control, TID,
        status, length) plus as many body bytes as fit in the MTU.
        If the declared body length exceeds what was received,
        additional GATT reads fetch continuation fragments.

        Args:
            char_uuid: GATT characteristic UUID to read from.
            context: Human-readable label for error messages.

        Returns:
            The TLV body from the assembled response.
        """
        import struct

        # First read: could be HAP PDU response or raw TLV depending
        # on the accessory implementation.
        raw: bytes = await self._gatt.read_characteristic(char_uuid)

        logger.debug(
            "%s first read: %d bytes, hex=%s",
            context, len(raw), raw[:32].hex(),
        )

        # Detect whether the response is HAP PDU or raw TLV.
        # HAP PDU response starts with control byte (bit 1 set = response)
        # followed by TID and 2-byte status.
        # Raw TLV starts with a TLV type byte (0x00–0x13 for HAP types).
        #
        # Heuristic: if byte 0 has bit 1 set (0x02), it's a PDU response.
        # Otherwise, treat as raw TLV and return after fragment assembly.
        is_pdu_response: bool = len(raw) >= 4 and bool(raw[0] & 0x02)

        if is_pdu_response:
            # HAP PDU response: control(1) + TID(1) + status(2) = 4 bytes.
            HEADER_LEN: int = 4
            status: int = struct.unpack_from("<H", raw, 2)[0]
            if status != 0:
                from .hap_constants import STATUS_DESCRIPTIONS
                desc: str = STATUS_DESCRIPTIONS.get(
                    status, f"0x{status:04X}"
                )
                raise HapError(f"{context} failed: {desc}")
        else:
            # Raw TLV response — no PDU header.  The entire read is
            # TLV data (possibly fragmented across multiple reads).
            logger.debug("%s response is raw TLV (no PDU header)", context)
            body: bytearray = bytearray(raw)
            MAX_FRAGMENT_READS: int = 100
            reads: int = 0
            # Keep reading until we get no more data or a short read
            # (indicating the last fragment).
            prev_len: int = len(raw)
            while reads < MAX_FRAGMENT_READS:
                frag: bytes = await self._gatt.read_characteristic(char_uuid)
                if not frag or frag == raw[:len(frag)]:
                    # No new data or repeated data — done.
                    break
                body.extend(frag)
                reads += 1
                if len(frag) < prev_len:
                    # Short read = last fragment.
                    break
                prev_len = len(frag)
            return bytes(body)

        # If no body length field, return empty.
        if len(raw) <= HEADER_LEN:
            return b""

        # Parse declared body length.
        if len(raw) < HEADER_LEN + 2:
            raise HapError(
                f"{context} response missing body length field"
            )
        body_len: int = struct.unpack_from("<H", raw, HEADER_LEN)[0]
        body_start: int = HEADER_LEN + 2

        # Accumulate body fragments.
        body: bytearray = bytearray(raw[body_start:])

        # Maximum reads to prevent infinite loops on broken devices.
        MAX_FRAGMENT_READS: int = 100

        reads: int = 0
        while len(body) < body_len and reads < MAX_FRAGMENT_READS:
            frag: bytes = await self._gatt.read_characteristic(char_uuid)
            if not frag:
                break
            # Continuation fragments are pure body data (no header).
            body.extend(frag)
            reads += 1

        if len(body) < body_len:
            logger.warning(
                "%s response incomplete: expected %d bytes, got %d",
                context, body_len, len(body),
            )

        return bytes(body[:body_len])

    # ------------------------------------------------------------------
    # Internal: PDU encryption/decryption
    # ------------------------------------------------------------------

    def _encrypt_pdu(self, pdu: bytes) -> bytes:
        """Encrypt a PDU using the active session."""
        assert self._session is not None
        return self._session.encrypt_request(pdu)

    def _decrypt_pdu(self, data: bytes) -> bytes:
        """Decrypt a PDU using the active session."""
        assert self._session is not None
        return self._session.decrypt_response(data)

    def _require_encrypted(self) -> None:
        """Raise if no encrypted session is active."""
        if self._session is None:
            raise HapError(
                "No encrypted session — call pair_verify() first"
            )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HapError(Exception):
    """Raised when a HAP protocol operation fails."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_state(tlv_dict: dict[int, bytes], expected: int) -> None:
    """Verify the TLV_STATE field matches the expected step number."""
    state_bytes: Optional[bytes] = tlv_dict.get(TLV_STATE)
    if state_bytes is None:
        raise HapError(f"Missing TLV_STATE in response (expected M{expected})")
    actual: int = state_bytes[0]
    if actual != expected:
        raise HapError(
            f"Unexpected state: got M{actual}, expected M{expected}"
        )


def _check_error(tlv_dict: dict[int, bytes], context: str) -> None:
    """Raise HapError if the response contains a TLV_ERROR."""
    error_bytes: Optional[bytes] = tlv_dict.get(TLV_ERROR)
    if error_bytes is not None:
        error_code: int = error_bytes[0]
        desc: str = ERROR_DESCRIPTIONS.get(error_code, f"0x{error_code:02X}")
        raise HapError(f"{context}: accessory error — {desc}")


def _verify_ed25519(
    public_key_bytes: bytes,
    signature: bytes,
    message: bytes,
) -> None:
    """Verify an Ed25519 signature.

    Raises:
        HapError: If the signature is invalid.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )
    from cryptography.exceptions import InvalidSignature

    pk = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        pk.verify(signature, message)
    except InvalidSignature:
        raise HapError("Ed25519 signature verification failed")


def _encrypt_with_tag(key: bytes, nonce_str: bytes, plaintext: bytes) -> bytes:
    """Encrypt using a fixed string nonce (for pair-setup/verify messages).

    HAP uses fixed ASCII nonces like ``b"PS-Msg05"`` for the pair-setup
    exchange, padded to 12 bytes with trailing zeros.

    Args:
        key: 32-byte ChaCha20-Poly1305 key.
        nonce_str: ASCII nonce (e.g., ``b"PS-Msg05"``), max 12 bytes.
        plaintext: Data to encrypt.

    Returns:
        Ciphertext with 16-byte auth tag appended.
    """
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    # Pad nonce to 12 bytes with trailing zeros.
    nonce: bytes = nonce_str.ljust(12, b"\x00")
    cipher = ChaCha20Poly1305(key)
    return cipher.encrypt(nonce, plaintext, None)


def _decrypt_with_tag(key: bytes, nonce_str: bytes, ciphertext: bytes) -> bytes:
    """Decrypt using a fixed string nonce (for pair-setup/verify messages).

    Args:
        key: 32-byte ChaCha20-Poly1305 key.
        nonce_str: ASCII nonce (must match what was used for encryption).
        ciphertext: Ciphertext with 16-byte auth tag appended.

    Returns:
        Decrypted plaintext.

    Raises:
        HapError: If decryption fails (wrong key or tampered data).
    """
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.exceptions import InvalidTag

    nonce: bytes = nonce_str.ljust(12, b"\x00")
    cipher = ChaCha20Poly1305(key)
    try:
        return cipher.decrypt(nonce, ciphertext, None)
    except InvalidTag:
        raise HapError(
            f"Decryption failed (nonce={nonce_str!r}) — wrong key or "
            "tampered data"
        )
