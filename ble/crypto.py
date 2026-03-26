"""Session cryptography — HKDF key derivation and ChaCha20-Poly1305.

After SRP pair-setup or Curve25519 pair-verify, both sides share a
secret.  This module derives encryption keys from that secret using
HKDF-SHA-512 and provides encrypt/decrypt using ChaCha20-Poly1305
with HAP's nonce construction.

HAP-BLE nonce format (Section 7.3.7):
    - 4 bytes: ``\\x00\\x00\\x00\\x00`` (fixed padding)
    - 8 bytes: little-endian 64-bit counter

The counter increments with every encrypted PDU.  Each direction
(controller→accessory and accessory→controller) maintains its own
counter starting at zero.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import hashlib
import hmac
import logging
import struct
from typing import Optional

logger: logging.Logger = logging.getLogger("glowup.ble.crypto")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HKDF-SHA-512 hash length (bytes).
HKDF_HASH_LEN: int = 64

# ChaCha20-Poly1305 key size (bytes).
CHACHA_KEY_LEN: int = 32

# ChaCha20-Poly1305 nonce size (bytes, HAP uses 12-byte nonce).
CHACHA_NONCE_LEN: int = 12

# ChaCha20-Poly1305 authentication tag size (bytes).
CHACHA_TAG_LEN: int = 16

# Fixed 4-byte nonce padding prepended to every HAP nonce (Section 7.3.7).
_NONCE_PAD: bytes = b"\x00\x00\x00\x00"


# ---------------------------------------------------------------------------
# HKDF-SHA-512 (RFC 5869)
#
# We implement HKDF directly rather than pulling in a large crypto
# library dependency.  It is two HMAC calls — extract and expand.
# ---------------------------------------------------------------------------

def hkdf_sha512(
    ikm: bytes,
    salt: bytes,
    info: bytes,
    length: int = CHACHA_KEY_LEN,
) -> bytes:
    """Derive *length* bytes from *ikm* using HKDF-SHA-512.

    Args:
        ikm: Input keying material (the shared secret).
        salt: Optional salt (use ``b""`` for no salt; HKDF substitutes
            a zero-filled block internally).
        info: Context and application-specific info string.
            HAP uses info strings like ``b"Pair-Setup-Encrypt-Salt"``
            and ``b"Pair-Setup-Encrypt-Info"``.
        length: Desired output length in bytes (max 255 * 64).

    Returns:
        Derived key material of exactly *length* bytes.

    Raises:
        ValueError: If *length* exceeds the HKDF maximum.
    """
    max_length: int = 255 * HKDF_HASH_LEN
    if length > max_length:
        raise ValueError(
            f"HKDF output length {length} exceeds maximum {max_length}"
        )

    # Extract: PRK = HMAC-SHA-512(salt, IKM)
    if not salt:
        salt = b"\x00" * HKDF_HASH_LEN
    prk: bytes = hmac.new(salt, ikm, hashlib.sha512).digest()

    # Expand: output = T(1) || T(2) || ... truncated to *length*
    # T(i) = HMAC-SHA-512(PRK, T(i-1) || info || i)
    okm: bytearray = bytearray()
    prev: bytes = b""
    counter: int = 1

    while len(okm) < length:
        prev = hmac.new(
            prk,
            prev + info + bytes([counter]),
            hashlib.sha512,
        ).digest()
        okm.extend(prev)
        counter += 1

    return bytes(okm[:length])


# ---------------------------------------------------------------------------
# ChaCha20-Poly1305 encrypt/decrypt
#
# Uses the `cryptography` library for the AEAD cipher.  This is the
# only external crypto dependency — Python's stdlib does not include
# ChaCha20-Poly1305.
# ---------------------------------------------------------------------------

def _build_nonce(counter: int) -> bytes:
    """Construct a 12-byte HAP nonce from a 64-bit counter.

    Format: 4 zero bytes || 8-byte little-endian counter.
    Used for post-pair-verify encrypted session PDUs.

    Args:
        counter: Message sequence number (0, 1, 2, ...).

    Returns:
        12-byte nonce.
    """
    return _NONCE_PAD + struct.pack("<Q", counter)


def build_pairing_nonce(label: bytes) -> bytes:
    """Construct a 12-byte nonce for pair-setup/verify messages.

    Format: 4 zero bytes || 8-byte label (e.g., b"PS-Msg05").

    CRITICAL: the 4 zero bytes come FIRST, then the label.  Getting
    this backwards (label first) was a bug that caused M5/M6 auth
    failures during initial development.  Verified correct against
    homekit_python's chacha20_aead_encrypt which uses
    ``nonce = constant + iv`` where constant = 4 zero bytes.

    Args:
        label: 8-byte message label (e.g., ``b"PS-Msg05"``).

    Returns:
        12-byte nonce.
    """
    return _NONCE_PAD + label


def encrypt(
    key: bytes,
    counter: int,
    plaintext: bytes,
    aad: Optional[bytes] = None,
) -> bytes:
    """Encrypt with ChaCha20-Poly1305 using HAP nonce construction.

    Args:
        key: 32-byte encryption key.
        counter: Nonce counter (incremented per message).
        plaintext: Data to encrypt.
        aad: Additional authenticated data (not encrypted, but
            integrity-protected).  HAP uses this for PDU headers.

    Returns:
        Ciphertext with 16-byte Poly1305 tag appended.
    """
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    nonce: bytes = _build_nonce(counter)
    cipher = ChaCha20Poly1305(key)
    # cryptography library appends the 16-byte tag to the ciphertext.
    return cipher.encrypt(nonce, plaintext, aad)


def decrypt(
    key: bytes,
    counter: int,
    ciphertext_with_tag: bytes,
    aad: Optional[bytes] = None,
) -> bytes:
    """Decrypt with ChaCha20-Poly1305 using HAP nonce construction.

    Args:
        key: 32-byte decryption key.
        counter: Expected nonce counter.
        ciphertext_with_tag: Ciphertext with 16-byte Poly1305 tag
            appended (as returned by :func:`encrypt`).
        aad: Additional authenticated data (must match what was used
            during encryption).

    Returns:
        Decrypted plaintext.

    Raises:
        cryptography.exceptions.InvalidTag: If the tag verification
            fails (wrong key, tampered data, or wrong counter).
    """
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    nonce: bytes = _build_nonce(counter)
    cipher = ChaCha20Poly1305(key)
    return cipher.decrypt(nonce, ciphertext_with_tag, aad)


# ---------------------------------------------------------------------------
# HAP session key derivation helpers
#
# These wrap HKDF with the exact salt/info strings that HAP mandates
# for each phase of the protocol.
# ---------------------------------------------------------------------------

def derive_pair_setup_encrypt_key(srp_session_key: bytes) -> bytes:
    """Derive the encryption key for pair-setup M5/M6 exchanges.

    Uses the SRP session key *K* as input keying material.

    Args:
        srp_session_key: 64-byte SRP session key from
            :meth:`SrpClient.get_session_key`.

    Returns:
        32-byte ChaCha20-Poly1305 key.
    """
    return hkdf_sha512(
        ikm=srp_session_key,
        salt=b"Pair-Setup-Encrypt-Salt",
        info=b"Pair-Setup-Encrypt-Info",
        length=CHACHA_KEY_LEN,
    )


def derive_pair_setup_controller_sign_key(srp_session_key: bytes) -> bytes:
    """Derive the key material used for the controller's Ed25519 signature.

    The controller signs (iOSDeviceX || iOSDevicePairingID || iOSDeviceLTPK)
    where iOSDeviceX is this derived key.

    Args:
        srp_session_key: 64-byte SRP session key.

    Returns:
        32-byte key material.
    """
    return hkdf_sha512(
        ikm=srp_session_key,
        salt=b"Pair-Setup-Controller-Sign-Salt",
        info=b"Pair-Setup-Controller-Sign-Info",
        length=CHACHA_KEY_LEN,
    )


def derive_pair_setup_accessory_sign_key(srp_session_key: bytes) -> bytes:
    """Derive the key material used for the accessory's Ed25519 signature.

    Args:
        srp_session_key: 64-byte SRP session key.

    Returns:
        32-byte key material.
    """
    return hkdf_sha512(
        ikm=srp_session_key,
        salt=b"Pair-Setup-Accessory-Sign-Salt",
        info=b"Pair-Setup-Accessory-Sign-Info",
        length=CHACHA_KEY_LEN,
    )


def derive_pair_verify_encrypt_key(shared_secret: bytes) -> bytes:
    """Derive the encryption key for pair-verify M2/M3 exchanges.

    Uses the Curve25519 shared secret as input keying material.

    Args:
        shared_secret: 32-byte Curve25519 shared secret.

    Returns:
        32-byte ChaCha20-Poly1305 key.
    """
    return hkdf_sha512(
        ikm=shared_secret,
        salt=b"Pair-Verify-Encrypt-Salt",
        info=b"Pair-Verify-Encrypt-Info",
        length=CHACHA_KEY_LEN,
    )


def derive_session_keys(shared_secret: bytes) -> tuple[bytes, bytes]:
    """Derive the controller→accessory and accessory→controller keys.

    Called after pair-verify succeeds to establish the encrypted
    session for all subsequent characteristic reads/writes.

    Args:
        shared_secret: 32-byte Curve25519 shared secret from pair-verify.

    Returns:
        Tuple of (controller_to_accessory_key, accessory_to_controller_key),
        each 32 bytes.
    """
    c2a: bytes = hkdf_sha512(
        ikm=shared_secret,
        salt=b"Control-Salt",
        info=b"Control-Read-Encryption-Key",
        length=CHACHA_KEY_LEN,
    )
    a2c: bytes = hkdf_sha512(
        ikm=shared_secret,
        salt=b"Control-Salt",
        info=b"Control-Write-Encryption-Key",
        length=CHACHA_KEY_LEN,
    )
    return c2a, a2c
