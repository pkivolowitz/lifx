"""SRP-6a implementation — HAP variant for pair-setup authentication.

Implements the Secure Remote Password protocol (version 6a) as
specified by HAP for pair-setup.  The HAP variant uses:

    - 3072-bit prime group (RFC 5054 Appendix A)
    - SHA-512 as the hash function
    - Username ``"Pair-Setup"``, password is the setup code with dashes

This module matches homekit_python's SRP implementation exactly —
verified by successful pair-setup with ONVIS SMS2 on 2026-03-25.

CRITICAL implementation detail: integer-to-bytes conversion uses
*minimum* byte length (``math.ceil(bit_length / 8)``), NOT fixed
384-byte padding.  homekit_python and real accessories expect this.
Using fixed-width padding produces wrong hashes and auth failures.

Reference:
    - RFC 2945 (SRP), RFC 5054 (SRP for TLS)
    - HAP Specification R17, Section 5.6 (Pair Setup)
    - homekit_python crypto/srp.py (reference implementation)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import hashlib
import logging
import math
import os
from typing import Optional, Union

from .hap_constants import (
    SRP_GENERATOR,
    SRP_HASH_NAME,
    SRP_PRIME_3072,
    SRP_SALT_LENGTH,
)

logger: logging.Logger = logging.getLogger("glowup.ble.srp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# HAP mandates this literal string as the SRP username (Section 5.6.1).
SRP_USERNAME: str = "Pair-Setup"

# Byte length of the 3072-bit prime.
_PRIME_BYTE_LEN: int = (SRP_PRIME_3072.bit_length() + 7) // 8  # 384 bytes

# Byte length of SHA-512 output.
_HASH_LEN: int = 64

# Private key size (bytes).  homekit_python uses 16 bytes.
_PRIVATE_KEY_LEN: int = 16


# ---------------------------------------------------------------------------
# Helpers
#
# IMPORTANT: to_byte_array uses MINIMUM byte representation, matching
# homekit_python's Srp.to_byte_array.  Using fixed-width padding
# (to_bytes(384, 'big')) produces different hashes and breaks auth.
# ---------------------------------------------------------------------------

def to_byte_array(num: int) -> bytearray:
    """Convert a positive integer to big-endian bytes, minimum length.

    This matches homekit_python's ``Srp.to_byte_array`` exactly.
    The minimum-length representation means leading zero bytes are
    stripped, which changes hash inputs compared to fixed-width padding.

    Args:
        num: Non-negative integer.

    Returns:
        Big-endian bytearray with no leading zeros (except for 0 → b'\\x00').
    """
    if num == 0:
        return bytearray(b"\x00")
    byte_len: int = math.ceil(num.bit_length() / 8)
    return bytearray(num.to_bytes(byte_len, byteorder="big"))


def _hash(*args: bytes) -> bytes:
    """SHA-512 hash of concatenated arguments."""
    h = hashlib.new(SRP_HASH_NAME)
    for a in args:
        h.update(a)
    return h.digest()


def _hash_int(*args: bytes) -> int:
    """SHA-512 hash of concatenated arguments, returned as an integer."""
    return int.from_bytes(_hash(*args), byteorder="big")


# ---------------------------------------------------------------------------
# SRP Client
# ---------------------------------------------------------------------------

class SrpClient:
    """SRP-6a client for HAP pair-setup.

    Matches homekit_python's SrpClient interface and behavior.

    Usage::

        client = SrpClient("Pair-Setup", "164-77-432")
        client.set_salt(salt_bytes)
        client.set_server_public_key(B_bytes)
        A = to_byte_array(client.get_public_key())
        M1 = to_byte_array(client.get_proof())
        # Send A and M1 to the accessory.
        # Receive M2.
        if client.verify_server_proof(M2_bytes):
            session_key = to_byte_array(client.get_session_key())
    """

    def __init__(self, username: str, password: str) -> None:
        """Initialize the SRP client.

        Args:
            username: SRP username — always ``"Pair-Setup"`` for HAP.
            password: The setup code with dashes, e.g. ``"164-77-432"``.
        """
        self._username: str = username
        self._password: str = password
        self._N: int = SRP_PRIME_3072
        self._g: int = SRP_GENERATOR

        # Private ephemeral value *a* — 16 bytes (matches homekit_python).
        self._a: int = int.from_bytes(os.urandom(_PRIVATE_KEY_LEN), "big")

        # Public ephemeral value *A* = g^a mod N.
        self._A: int = pow(self._g, self._a, self._N)

        # Set after set_salt / set_server_public_key.
        self._salt: Optional[int] = None
        self._B: Optional[int] = None

    def set_salt(self, salt: Union[bytes, bytearray, int]) -> None:
        """Set the server-provided salt.

        Args:
            salt: Salt as bytes/bytearray or integer.
        """
        if isinstance(salt, (bytes, bytearray)):
            self._salt = int.from_bytes(salt, "big")
        else:
            self._salt = salt

    def set_server_public_key(self, B: Union[bytes, bytearray, int]) -> None:
        """Set the server's public ephemeral value *B*.

        Args:
            B: Server public key as bytes/bytearray or integer.

        Raises:
            ValueError: If *B* is zero mod N.
        """
        if isinstance(B, (bytes, bytearray)):
            self._B = int.from_bytes(B, "big")
        else:
            self._B = B

        if self._B % self._N == 0:
            raise ValueError(
                "SRP abort: server public key B is 0 mod N"
            )

    def get_public_key(self) -> int:
        """Return the client's public ephemeral value *A* as integer."""
        return self._A

    def _calculate_k(self) -> int:
        """Compute SRP multiplier *k* = H(N, PAD(g)).

        PAD(g) is g zero-padded to 384 bytes (matching homekit_python).
        """
        n_bytes: bytearray = to_byte_array(self._N)
        # g padded to 384 bytes — 383 zero bytes + 0x05.
        g_padded: bytearray = bytearray.fromhex("00" * 383 + "05")
        return _hash_int(n_bytes, g_padded)

    def _calculate_u(self) -> int:
        """Compute scrambling parameter *u* = H(A, B).

        Uses minimum-byte representation for both A and B.
        """
        return _hash_int(to_byte_array(self._A), to_byte_array(self._B))

    def _calculate_x(self) -> int:
        """Compute private key *x* = H(salt, H(username : password)).

        The salt is converted to minimum-byte representation.
        """
        # Inner hash: H("Pair-Setup:164-77-432")
        identity: bytes = (self._username + ":" + self._password).encode()
        inner: bytes = _hash(identity)
        # Outer hash: H(salt_bytes || inner)
        return _hash_int(to_byte_array(self._salt), inner)

    def get_shared_secret(self) -> int:
        """Compute the shared secret *S*.

        Returns:
            The shared secret as an integer.
        """
        if self._B is None:
            raise RuntimeError("Server public key not set")
        u: int = self._calculate_u()
        x: int = self._calculate_x()
        k: int = self._calculate_k()
        # S = (B - k * g^x)^(a + u*x) mod N
        base: int = self._B - k * pow(self._g, x, self._N)
        exponent: int = self._a + u * x
        return pow(base, exponent, self._N)

    def get_session_key(self) -> int:
        """Return the session key *K* = H(S) as an integer.

        The caller should use ``to_byte_array(K)`` to get bytes for HKDF.
        """
        return int.from_bytes(
            _hash(to_byte_array(self.get_shared_secret())),
            byteorder="big",
        )

    def get_proof(self) -> int:
        """Compute and return the client proof *M1* as an integer.

        M1 = H(H(N) XOR H(g), H(username), salt, A, B, K)
        """
        if self._B is None:
            raise RuntimeError("Server public key not set")

        h_N: bytearray = bytearray(_hash(to_byte_array(self._N)))
        h_g: bytearray = bytearray(_hash(to_byte_array(self._g)))
        # XOR H(N) and H(g)
        for i in range(len(h_N)):
            h_N[i] ^= h_g[i]

        h_user: bytes = _hash(self._username.encode())
        K_bytes: bytearray = to_byte_array(self.get_session_key())

        proof: bytes = _hash(
            bytes(h_N),
            h_user,
            to_byte_array(self._salt),
            to_byte_array(self._A),
            to_byte_array(self._B),
            K_bytes,
        )
        return int.from_bytes(proof, byteorder="big")

    def verify_server_proof(self, server_M2: Union[bytes, bytearray, int]) -> bool:
        """Verify the server's proof *M2*.

        Args:
            server_M2: Server proof as bytes/bytearray or integer.

        Returns:
            True if the server proved knowledge of the setup code.
        """
        if isinstance(server_M2, (bytes, bytearray)):
            m2_int: int = int.from_bytes(server_M2, "big")
        else:
            m2_int = server_M2

        expected: bytes = _hash(
            to_byte_array(self._A),
            to_byte_array(self.get_proof()),
            to_byte_array(self.get_session_key()),
        )
        is_valid: bool = m2_int == int.from_bytes(expected, byteorder="big")
        if not is_valid:
            logger.warning("SRP server proof M2 verification failed")
        return is_valid
