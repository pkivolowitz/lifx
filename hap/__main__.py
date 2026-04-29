"""HAP-CoAP-over-Thread smoke test: pair-verify + accessory-database dump.

Usage:

    python3 -m lifx.hap pair-verify \\
        --keys-file /tmp/ble_pairing.json --label foyer3 \\
        --address fd90:ac98:4b8b:1:2556:200a:42e9:1ea4 --port 5683

Steps performed:

    1. Load long-term pairing keys from a BLE-style ``ble_pairing.json``
       (controller LTSK/LTPK + per-device accessory LTPK + accessory
       pairing ID).  HAP pair-setup must have already been run over BLE
       (see ``~/glowup-infra/scripts/hap-thread-commission/``).
    2. Open a CoAP UDP socket to the accessory's OMR address + port.
    3. Run pair-verify (M1→M2→M3→M4) over CoAP POST ``/2`` with
       TLV-framed bodies.
    4. Derive the three post-verify session keys (Control-Read,
       Control-Write, Event-Read) from the X25519 shared secret.
    5. Send an encrypted POST ``/`` with HAP-PDU opcode 0x09 (read
       accessory database) at IID 0.
    6. Decrypt the response, parse the top-level TLV, dump the
       service / characteristic tree to stdout.

This is M1 from the implementation plan
(``lifx/.claude/plans/hap_thread.md``).  No MQTT, no daemon, no
subscriptions — just the pair-verify + DB-dump round trip to confirm
the wire works end-to-end.

Reuses primitives from ``lifx/ble/``:
    - tlv.encode / tlv.decode_dict
    - crypto.hkdf_sha512, build_pairing_nonce, derive_pair_verify_encrypt_key
    - hap_constants.TLV_* (state, public_key, encrypted_data, identifier,
      signature, error)

CoAP-specific primitives stay in this file for now; will be split into
``transport.py`` / ``session.py`` / ``pdu.py`` once M1 is green.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "0.1"

import argparse
import asyncio
import json
import logging
import struct
import sys
from pathlib import Path
from typing import Optional

# --- Guarded import for the optional CoAP transport dep --------------------
# Per the architecture rule "Everything above core is optional", we fail
# loud at runtime with a precise install hint rather than at import time.
try:
    from aiocoap import Context, Message, Code, GET, POST  # noqa: F401
    _HAS_AIOCOAP: bool = True
    _AIOCOAP_IMPORT_ERROR: Optional[Exception] = None
except Exception as exc:
    _HAS_AIOCOAP = False
    _AIOCOAP_IMPORT_ERROR = exc

# --- Reuse from lifx/ble (transport-agnostic HAP primitives) ----------------
from ble import tlv as _tlv
from ble.crypto import (
    hkdf_sha512,
    build_pairing_nonce,
    derive_pair_verify_encrypt_key,
)
from ble.hap_constants import (
    TLV_ENCRYPTED_DATA,
    TLV_ERROR,
    TLV_IDENTIFIER,
    TLV_PUBLIC_KEY,
    TLV_SIGNATURE,
    TLV_STATE,
)

# --- Constants --------------------------------------------------------------

# Controller pairing identifier.  Same constant the BLE pair-setup uses
# (``ble/hap_session.py:76`` — ``CONTROLLER_PAIRING_ID = b"GlowUp"``).
# Imported as a literal here to avoid pulling all of hap_session's BLE
# machinery into the CoAP path.
CONTROLLER_PAIRING_ID: bytes = b"GlowUp"

# CoAP-flavour HAP-PDU opcodes (from the wire-format reference doc;
# matches ``ble/hap_constants`` opcode numbering since these are
# transport-agnostic HAP-spec values).
OPCODE_READ_ACCESSORY_DB: int = 0x09  # opcode 0x09: read /accessories
OPCODE_CHAR_READ: int = 0x03
OPCODE_CHAR_WRITE: int = 0x02

# CoAP URI paths.  All paired ops POST to ``/``; pair-verify POSTs to
# ``/2``.  Per ``docs/40-hap-coap-wire-format.md`` §1.
URI_PAIR_VERIFY: str = "2"
URI_PAIRED: str = ""  # bare path: coap://[host]:port/

# HAP-PDU header field sizes (CoAP variant — same as HAP-BLE):
#   request:  control(1) | opcode(1) | TID(1) | IID(2 LE) | Len(2 LE)
#   response: control(1) | TID(1) | status(1) | Len(2 LE)
_HAP_PDU_REQ_HEADER_FMT: str = "<BBBHH"
_HAP_PDU_RESP_HEADER_FMT: str = "<BBBH"
_HAP_PDU_REQ_HEADER_LEN: int = struct.calcsize(_HAP_PDU_REQ_HEADER_FMT)
_HAP_PDU_RESP_HEADER_LEN: int = struct.calcsize(_HAP_PDU_RESP_HEADER_FMT)

# HAP TLV parameter for the value field inside read/write request bodies.
# Same value as ble/hap_pdu.py:HAP_PARAM_VALUE.
HAP_PARAM_VALUE: int = 0x01

# CoAP control byte for HAP-CoAP requests (per the wire-format ref §4):
# always 0b00000000 (no fragmentation flag, since CoAP layer handles MTU).
_CONTROL_REQUEST: int = 0x00

# HKDF salt/info constants for the post-pair-verify session keys
# (per docs/40-hap-coap-wire-format.md §2a).
_CONTROL_SALT: bytes = b"Control-Salt"
_CONTROL_READ_INFO: bytes = b"Control-Read-Encryption-Key"
_CONTROL_WRITE_INFO: bytes = b"Control-Write-Encryption-Key"
_EVENT_SALT: bytes = b"Event-Salt"
_EVENT_READ_INFO: bytes = b"Event-Read-Encryption-Key"
_KEY_LEN: int = 32

# Encrypted-frame nonce: 4-byte zero pad + 8-byte LE counter (12 bytes).
_NONCE_PAD: bytes = b"\x00\x00\x00\x00"

logger: logging.Logger = logging.getLogger("glowup.hap")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class HapError(Exception):
    """Raised when a HAP-CoAP operation fails."""


class HapDecryptError(HapError):
    """Raised when a CoAP frame fails ChaCha20Poly1305 verification."""


class HapNotFoundError(HapError):
    """Raised on CoAP 4.04 from a paired session.

    Per the wire-format reference §6: 4.04 Not Found from an encrypted
    POST means the accessory has rebooted and the session is gone.
    Caller must tear down the session and re-run pair-verify.
    """


# ---------------------------------------------------------------------------
# Pairing-data loader
# ---------------------------------------------------------------------------

def load_pairing(keys_file: Path, label: str) -> dict:
    """Load HAP-CoAP pairing material from a BLE-style ble_pairing.json.

    Args:
        keys_file: Path to a ble_pairing.json (the format produced by
            lifx/ble/registry.py).
        label: Device label (e.g., ``"foyer3"``).

    Returns:
        Dict with hex-encoded fields:
        ``accessory_ltpk``, ``accessory_pairing_id``, ``controller_ltsk``,
        ``controller_ltpk``, ``ios_pairing_id`` (the latter is always
        ``"GlowUp"`` to match the BLE-side CONTROLLER_PAIRING_ID).

    Raises:
        HapError: If the file or device entry is missing or unpaired.
    """
    if not keys_file.is_file():
        raise HapError(f"keys file not found: {keys_file}")

    data: dict = json.loads(keys_file.read_text())

    ctrl: dict = data.get("controller", {}) or {}
    ltsk_hex: Optional[str] = ctrl.get("ltsk")
    ltpk_hex: Optional[str] = ctrl.get("ltpk")
    if not ltsk_hex or not ltpk_hex:
        raise HapError(
            f"controller keys missing in {keys_file} — has any pair-setup "
            f"been run?"
        )

    devices: dict = data.get("devices", {}) or {}
    dev: Optional[dict] = devices.get(label)
    if dev is None:
        raise HapError(
            f"device {label!r} not in {keys_file}; available: "
            f"{list(devices.keys())}"
        )
    if not dev.get("paired"):
        raise HapError(f"device {label!r} is not paired (paired=false)")

    acc_ltpk: Optional[str] = dev.get("accessory_ltpk")
    acc_id: Optional[str] = dev.get("accessory_pairing_id")
    if not acc_ltpk or not acc_id:
        raise HapError(
            f"device {label!r} missing accessory_ltpk or "
            f"accessory_pairing_id — pair-setup may have been interrupted"
        )

    return {
        "controller_ltsk": ltsk_hex,
        "controller_ltpk": ltpk_hex,
        "accessory_ltpk": acc_ltpk,
        "accessory_pairing_id": acc_id,
        "ios_pairing_id": CONTROLLER_PAIRING_ID.decode("ascii"),
    }


# ---------------------------------------------------------------------------
# CoAP transport (thin wrapper around aiocoap)
# ---------------------------------------------------------------------------

class CoapTransport:
    """Async CoAP UDP client targeting a single accessory endpoint.

    M1-only: client-side request/response.  Event-listener server (for
    accessory-initiated PUTs carrying subscription events) is not
    implemented yet — that lands in M2.
    """

    def __init__(self, host: str, port: int) -> None:
        if not _HAS_AIOCOAP:
            raise HapError(
                "aiocoap not installed — `pip install -r "
                "requirements-hap-thread.txt`"
            ) from _AIOCOAP_IMPORT_ERROR
        self._host: str = host
        self._port: int = port
        self._ctx: Optional["Context"] = None

    async def __aenter__(self) -> "CoapTransport":
        self._ctx = await Context.create_client_context()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ctx is not None:
            await self._ctx.shutdown()
            self._ctx = None

    async def post(self, uri_path: str, payload: bytes) -> bytes:
        """POST ``payload`` to ``coap://[<host>]:<port>/<uri_path>``.

        Returns the response payload bytes.  Raises HapError on
        non-2.04 response codes (with HapNotFoundError for 4.04).
        """
        if self._ctx is None:
            raise HapError("CoapTransport used outside async context")
        # Bracket IPv6 host literal in the URI.
        uri: str = f"coap://[{self._host}]:{self._port}/{uri_path}"
        request = Message(code=Code.POST, payload=payload, uri=uri)
        try:
            response = await asyncio.wait_for(
                self._ctx.request(request).response, timeout=10.0
            )
        except asyncio.TimeoutError:
            raise HapError(f"CoAP POST {uri} timed out")

        code_class: int = (response.code >> 5) & 0x7
        code_detail: int = response.code & 0x1F
        if code_class == 4 and code_detail == 4:
            raise HapNotFoundError(
                f"CoAP 4.04 from {uri} — accessory likely rebooted; "
                f"re-run pair-verify"
            )
        if response.code != Code.CHANGED:  # 2.04
            raise HapError(
                f"CoAP {code_class}.{code_detail:02d} from {uri} "
                f"(payload {len(response.payload)}B)"
            )
        return bytes(response.payload)


# ---------------------------------------------------------------------------
# HAP session crypto (post-pair-verify, three-key)
# ---------------------------------------------------------------------------

class HapSession:
    """Holds the three post-verify session keys + their counters.

    Per docs/40-hap-coap-wire-format.md §3:
        - ``send_key`` / ``send_ctr``: encrypts controller→accessory POSTs
        - ``recv_key`` / ``recv_ctr``: decrypts accessory→controller responses
        - ``event_key`` / ``event_ctr``: decrypts accessory-initiated PUTs
          (event channel).  Unused in M1; reserved for M2.

    Counters start at 0 and increment per direction.  No AAD on any frame.
    """

    def __init__(
        self,
        send_key: bytes,
        recv_key: bytes,
        event_key: bytes,
    ) -> None:
        self.send_key: bytes = send_key
        self.recv_key: bytes = recv_key
        self.event_key: bytes = event_key
        self.send_ctr: int = 0
        self.recv_ctr: int = 0
        self.event_ctr: int = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt one CoAP-body plaintext, advancing send_ctr."""
        from cryptography.hazmat.primitives.ciphers.aead import (
            ChaCha20Poly1305,
        )
        nonce: bytes = _NONCE_PAD + struct.pack("<Q", self.send_ctr)
        self.send_ctr += 1
        return ChaCha20Poly1305(self.send_key).encrypt(nonce, plaintext, b"")

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt one CoAP-body response, advancing recv_ctr."""
        from cryptography.hazmat.primitives.ciphers.aead import (
            ChaCha20Poly1305,
        )
        nonce: bytes = _NONCE_PAD + struct.pack("<Q", self.recv_ctr)
        self.recv_ctr += 1
        try:
            return ChaCha20Poly1305(self.recv_key).decrypt(
                nonce, ciphertext, b""
            )
        except Exception as exc:
            raise HapDecryptError(
                f"decrypt failed at recv_ctr={self.recv_ctr - 1}: {exc}"
            )


def _derive_three_keys(shared_secret: bytes) -> tuple[bytes, bytes, bytes]:
    """Derive (send, recv, event) session keys from the X25519 secret.

    Per docs/40-hap-coap-wire-format.md §2a — three HKDF-SHA512
    derivations with distinct salt+info pairs.
    """
    send_key: bytes = hkdf_sha512(
        ikm=shared_secret,
        salt=_CONTROL_SALT,
        info=_CONTROL_WRITE_INFO,
        length=_KEY_LEN,
    )
    recv_key: bytes = hkdf_sha512(
        ikm=shared_secret,
        salt=_CONTROL_SALT,
        info=_CONTROL_READ_INFO,
        length=_KEY_LEN,
    )
    event_key: bytes = hkdf_sha512(
        ikm=shared_secret,
        salt=_EVENT_SALT,
        info=_EVENT_READ_INFO,
        length=_KEY_LEN,
    )
    return send_key, recv_key, event_key


# ---------------------------------------------------------------------------
# Pair-verify over CoAP
# ---------------------------------------------------------------------------

async def pair_verify(
    transport: CoapTransport, pairing: dict
) -> HapSession:
    """Run HAP pair-verify M1→M4 over CoAP POST /2.

    Args:
        transport: An open :class:`CoapTransport`.
        pairing: Dict from :func:`load_pairing` (hex strings).

    Returns:
        An established :class:`HapSession` ready for encrypted I/O.

    Raises:
        HapError: on any TLV-level error from the accessory.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import (
        ChaCha20Poly1305,
    )
    from cryptography.exceptions import InvalidSignature

    accessory_ltpk: bytes = bytes.fromhex(pairing["accessory_ltpk"])
    controller_ltsk: bytes = bytes.fromhex(pairing["controller_ltsk"])

    # --- M1 → M2: ECDH start ---
    ephemeral_sk = X25519PrivateKey.generate()
    ephemeral_pk: bytes = ephemeral_sk.public_key().public_bytes_raw()

    m1_tlv: bytes = _tlv.encode([
        (TLV_STATE, b"\x01"),
        (TLV_PUBLIC_KEY, ephemeral_pk),
    ])
    m2_payload: bytes = await transport.post(URI_PAIR_VERIFY, m1_tlv)
    m2: dict[int, bytes] = _tlv.decode_dict(m2_payload)
    _check_error(m2, "pair-verify M2")

    accessory_pk_bytes: bytes = m2[TLV_PUBLIC_KEY]
    m2_encrypted: bytes = m2[TLV_ENCRYPTED_DATA]

    # Derive verify-intermediate key (same labels as HAP-BLE — see §2a).
    accessory_pk = X25519PublicKey.from_public_bytes(accessory_pk_bytes)
    shared_secret: bytes = ephemeral_sk.exchange(accessory_pk)
    verify_key: bytes = derive_pair_verify_encrypt_key(shared_secret)

    nonce_m2: bytes = build_pairing_nonce(b"PV-Msg02")
    m2_dec: bytes = ChaCha20Poly1305(verify_key).decrypt(
        nonce_m2, m2_encrypted, b""
    )
    m2_sub: dict[int, bytes] = _tlv.decode_dict(m2_dec)

    # Verify accessory signature: signed(accessory_pk || pairing_id || ephemeral_pk).
    accessory_info: bytes = (
        accessory_pk_bytes + m2_sub[TLV_IDENTIFIER] + ephemeral_pk
    )
    try:
        Ed25519PublicKey.from_public_bytes(accessory_ltpk).verify(
            m2_sub[TLV_SIGNATURE], accessory_info
        )
    except InvalidSignature:
        raise HapError("pair-verify M2: accessory signature invalid")

    logger.debug("pair-verify M2 verified — sending M3")

    # --- M3 → M4: controller proof ---
    controller_info: bytes = (
        ephemeral_pk + CONTROLLER_PAIRING_ID + accessory_pk_bytes
    )
    controller_sig: bytes = Ed25519PrivateKey.from_private_bytes(
        controller_ltsk
    ).sign(controller_info)

    sub_tlv: bytes = _tlv.encode([
        (TLV_IDENTIFIER, CONTROLLER_PAIRING_ID),
        (TLV_SIGNATURE, controller_sig),
    ])
    nonce_m3: bytes = build_pairing_nonce(b"PV-Msg03")
    m3_encrypted: bytes = ChaCha20Poly1305(verify_key).encrypt(
        nonce_m3, sub_tlv, b""
    )

    m3_tlv: bytes = _tlv.encode([
        (TLV_STATE, b"\x03"),
        (TLV_ENCRYPTED_DATA, m3_encrypted),
    ])
    m4_payload: bytes = await transport.post(URI_PAIR_VERIFY, m3_tlv)
    m4: dict[int, bytes] = _tlv.decode_dict(m4_payload)
    _check_error(m4, "pair-verify M4")

    # Derive the three post-verify session keys.
    send_key, recv_key, event_key = _derive_three_keys(shared_secret)
    return HapSession(send_key=send_key, recv_key=recv_key, event_key=event_key)


def _check_error(tlv_dict: dict[int, bytes], context: str) -> None:
    """Raise HapError if the TLV response carries an Error TLV."""
    err: Optional[bytes] = tlv_dict.get(TLV_ERROR)
    if err is not None and err != b"\x00":
        raise HapError(f"{context}: accessory error 0x{err[0]:02X}")


# ---------------------------------------------------------------------------
# HAP-PDU builders / parsers (CoAP-flavour)
# ---------------------------------------------------------------------------

def build_request_pdu(
    opcode: int, tid: int, iid: int, body: bytes = b""
) -> bytes:
    """Build a CoAP-flavour HAP-PDU request.

    Layout per docs/40-hap-coap-wire-format.md §4:
        control(1=0x00) | opcode(1) | TID(1) | IID(2 LE) | Len(2 LE) | body
    """
    return struct.pack(
        _HAP_PDU_REQ_HEADER_FMT,
        _CONTROL_REQUEST, opcode, tid, iid, len(body),
    ) + body


def parse_response_pdu(data: bytes) -> tuple[int, int, bytes]:
    """Parse a CoAP-flavour HAP-PDU response.

    Returns (tid, status, body).  Response control byte must satisfy
    ``control & 0x0E == 0x02`` (per the wire-format ref §4).
    """
    if len(data) < _HAP_PDU_RESP_HEADER_LEN:
        raise HapError(
            f"response PDU too short: {len(data)} < "
            f"{_HAP_PDU_RESP_HEADER_LEN}"
        )
    control, tid, status, body_len = struct.unpack(
        _HAP_PDU_RESP_HEADER_FMT, data[:_HAP_PDU_RESP_HEADER_LEN]
    )
    if (control & 0x0E) != 0x02:
        raise HapError(f"response PDU bad control byte: 0x{control:02X}")
    body: bytes = data[_HAP_PDU_RESP_HEADER_LEN:_HAP_PDU_RESP_HEADER_LEN + body_len]
    if len(body) != body_len:
        raise HapError(
            f"response PDU body truncated: {len(body)} < {body_len}"
        )
    return tid, status, body


# ---------------------------------------------------------------------------
# Encrypted POST (post-pair-verify CoAP)
# ---------------------------------------------------------------------------

async def encrypted_post(
    transport: CoapTransport,
    session: HapSession,
    pdu: bytes,
) -> bytes:
    """Send an encrypted HAP-PDU as CoAP POST ``/`` and decrypt the reply."""
    ciphertext: bytes = session.encrypt(pdu)
    response_ct: bytes = await transport.post(URI_PAIRED, ciphertext)
    plaintext: bytes = session.decrypt(response_ct)
    return plaintext


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def cmd_pair_verify(args: argparse.Namespace) -> int:
    """``pair-verify`` subcommand: smoke-test pair-verify + dump accessory db."""
    pairing: dict = load_pairing(Path(args.keys_file), args.label)
    print(
        f"loaded keys for {args.label!r} "
        f"(accessory={pairing['accessory_pairing_id']}, "
        f"controller={pairing['ios_pairing_id']})",
        flush=True,
    )

    async with CoapTransport(args.address, args.port) as transport:
        print(f"opening pair-verify to [{args.address}]:{args.port}/2",
              flush=True)
        session: HapSession = await pair_verify(transport, pairing)
        print("pair-verify OK — encrypted session established", flush=True)
        print(
            f"  send_key={session.send_key.hex()[:16]}...  "
            f"recv_key={session.recv_key.hex()[:16]}...  "
            f"event_key={session.event_key.hex()[:16]}...",
            flush=True,
        )

        # Read accessory database (opcode 0x09, IID 0).
        # TID is arbitrary, range 1..254 per wire-format ref §6.
        tid: int = 1
        pdu_req: bytes = build_request_pdu(
            opcode=OPCODE_READ_ACCESSORY_DB, tid=tid, iid=0,
        )
        print(
            f"sending opcode 0x09 (read accessory database), "
            f"TID={tid}, plaintext_len={len(pdu_req)}B",
            flush=True,
        )

        response_pt: bytes = await encrypted_post(transport, session, pdu_req)
        resp_tid, status, body = parse_response_pdu(response_pt)
        if resp_tid != tid:
            raise HapError(
                f"TID mismatch: sent {tid}, got {resp_tid}"
            )
        if status != 0:
            raise HapError(f"opcode 0x09 returned status 0x{status:02X}")

        print(
            f"opcode 0x09 OK — status=0, body_len={len(body)}B",
            flush=True,
        )
        Path("/tmp/foyer3_db.bin").write_bytes(body)
        _walk_accessory_db(body)

    return 0


# HAP accessory-database TLV tags (per docs/40-hap-coap-wire-format.md
# §4 + the HAP spec).  Inner-vs-outer naming matches aiohomekit's
# Pdu09Database structure for cross-reference.
_TAG_ACCESSORY_RECORD: int = 0x18
_TAG_SERVICE_RECORD: int = 0x19
_TAG_CHARACTERISTIC_RECORD: int = 0x14
_TAG_AID: int = 0x01
_TAG_SERVICES: int = 0x02
_TAG_SERVICE_IID: int = 0x06
_TAG_SERVICE_TYPE: int = 0x07
_TAG_CHARACTERISTICS: int = 0x15
_TAG_CHAR_IID: int = 0x04
_TAG_CHAR_TYPE: int = 0x14
_TAG_CHAR_PERMS: int = 0x0A
_TAG_CHAR_FORMAT: int = 0x0C


def _split_records(blob: bytes, record_tag: int) -> list[bytes]:
    """Split a list-of-records blob into one bytes object per record.

    The list TLV's body is a sequence of ``record_tag``-prefixed entries
    concatenated back-to-back.  Each entry may itself span multiple
    255-byte chunks (HAP TLV continuation).  This function walks the
    blob byte-by-byte and emits one bytes-per-record, with continuation
    chunks pre-merged.
    """
    records: list[bytes] = []
    cur: bytearray = bytearray()
    i: int = 0
    while i < len(blob):
        tag: int = blob[i]
        length: int = blob[i + 1]
        chunk: bytes = blob[i + 2:i + 2 + length]
        i += 2 + length
        if tag == record_tag:
            if cur:
                records.append(bytes(cur))
                cur = bytearray()
            cur.extend(chunk)
            # If length == 255 the next entry of the same tag is a
            # continuation of THIS record; keep accumulating until we
            # see a length < 255.
            if length < 0xFF:
                records.append(bytes(cur))
                cur = bytearray()
        # Stray non-record-tag bytes (shouldn't happen but tolerate).
    if cur:
        records.append(bytes(cur))
    return records


def _decode_uuid(b: bytes) -> str:
    """Decode a HAP UUID (1, 2, 4, or 16 bytes little-endian)."""
    if len(b) == 16:
        # 128-bit UUID stored little-endian; render canonical form.
        u: int = int.from_bytes(b, "little")
        h: str = f"{u:032x}"
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    if len(b) in (1, 2, 4):
        return f"short:0x{int.from_bytes(b, 'little'):0{len(b) * 2}x}"
    return f"len{len(b)}:{b.hex()}"


def _walk_accessory_db(body: bytes) -> None:
    """Pretty-print the accessory database opcode-0x09 response."""
    outer: dict[int, bytes] = _tlv.decode_dict(body)
    accessories_blob: bytes = outer.get(_TAG_ACCESSORY_RECORD, b"")
    if not accessories_blob:
        # Some accessories return the inner content directly without
        # the 0x18 outer envelope — fall back to treating *body* as
        # one accessory record.
        accessories: list[bytes] = [body]
    else:
        accessories = _split_records(accessories_blob, _TAG_ACCESSORY_RECORD)
        # _split_records returns one record when the outer tag was
        # already extracted by decode_dict, so for the typical
        # single-accessory case this is a 1-element list.
        if not accessories:
            accessories = [accessories_blob]

    for acc_blob in accessories:
        acc: dict[int, bytes] = _tlv.decode_dict(acc_blob)
        aid: int = int.from_bytes(acc.get(_TAG_AID, b"\x00"), "little")
        services_blob: bytes = acc.get(_TAG_SERVICES, b"")
        services: list[bytes] = _split_records(
            services_blob, _TAG_SERVICE_RECORD
        )
        print(f"AID {aid} — {len(services)} service(s)", flush=True)
        for svc_blob in services:
            svc: dict[int, bytes] = _tlv.decode_dict(svc_blob)
            sid: int = int.from_bytes(
                svc.get(_TAG_SERVICE_IID, b"\x00"), "little"
            )
            svc_type: str = _decode_uuid(svc.get(_TAG_SERVICE_TYPE, b""))
            chars_blob: bytes = svc.get(_TAG_CHARACTERISTICS, b"")
            chars: list[bytes] = _split_records(
                chars_blob, _TAG_CHARACTERISTIC_RECORD
            )
            print(
                f"  svc iid={sid:>4} type={svc_type} "
                f"chars={len(chars)}",
                flush=True,
            )
            for ch_blob in chars:
                ch: dict[int, bytes] = _tlv.decode_dict(ch_blob)
                ciid: int = int.from_bytes(
                    ch.get(_TAG_CHAR_IID, b"\x00"), "little"
                )
                ch_type: str = _decode_uuid(ch.get(_TAG_CHAR_TYPE, b""))
                perms_b: bytes = ch.get(_TAG_CHAR_PERMS, b"\x00")
                perms: int = int.from_bytes(perms_b, "little")
                fmt_b: bytes = ch.get(_TAG_CHAR_FORMAT, b"")
                fmt_byte: int = fmt_b[0] if fmt_b else 0
                print(
                    f"    chr iid={ciid:>4} type={ch_type} "
                    f"perms=0x{perms:04x} fmt=0x{fmt_byte:02x}",
                    flush=True,
                )


def main() -> None:
    """Entry point for ``python3 -m lifx.hap``."""
    parser = argparse.ArgumentParser(
        prog="python3 -m lifx.hap",
        description="HAP-CoAP-over-Thread bridge (smoke-test scope).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "pair-verify",
        help="Run pair-verify against an accessory and dump its database",
    )
    p.add_argument("--keys-file", required=True,
                   help="Path to ble_pairing.json")
    p.add_argument("--label", required=True,
                   help="Device label (key in ble_pairing.json devices)")
    p.add_argument("--address", required=True,
                   help="IPv6 address (no brackets)")
    p.add_argument("--port", type=int, default=5683,
                   help="UDP port (default 5683)")
    p.set_defaults(func=cmd_pair_verify)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    rc: int = asyncio.run(args.func(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
