"""SRP-6a implementation — HAP variant for pair-setup authentication.

Implements the Secure Remote Password protocol (version 6a) as
specified by HAP for pair-setup.  The HAP variant uses:

    - 3072-bit prime group (RFC 5054 Appendix A)
    - SHA-512 as the hash function
    - Setup code formatted as ``XXX-XX-XXX`` (no ``Pair-Setup`` user)

The flow:

    1. Client sends username ``b"Pair-Setup"`` (HAP mandated).
    2. Server (accessory) sends salt *s* and public value *B*.
    3. Client computes public value *A*, shared key *S*, and proof *M1*.
    4. Server verifies *M1*, responds with proof *M2*.
    5. Both sides derive the session key *K* from *S*.

This module provides the *client* side only — GlowUp is always the
controller initiating pair-setup with an accessory.

Reference:
    - RFC 2945 (SRP), RFC 5054 (SRP for TLS)
    - HAP Specification R17, Section 5.6 (Pair Setup)
    - T. Wu, "The SRP Authentication and Key Exchange System" (1998)
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import hashlib
import logging
import os
from typing import Optional

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
SRP_USERNAME: bytes = b"Pair-Setup"

# Byte length of the 3072-bit prime.
_PRIME_BYTE_LEN: int = (SRP_PRIME_3072.bit_length() + 7) // 8  # 384 bytes

# Byte length of SHA-512 output.
_HASH_LEN: int = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash(*args: bytes) -> bytes:
    """SHA-512 hash of concatenated arguments."""
    h = hashlib.new(SRP_HASH_NAME)
    for a in args:
        h.update(a)
    return h.digest()


def _int_to_bytes(n: int) -> bytes:
    """Convert a positive integer to big-endian bytes, padded to prime length."""
    return n.to_bytes(_PRIME_BYTE_LEN, byteorder="big")


def _bytes_to_int(b: bytes) -> int:
    """Convert big-endian bytes to a positive integer."""
    return int.from_bytes(b, byteorder="big")


def _hash_int(*args: bytes) -> int:
    """SHA-512 hash of concatenated arguments, returned as an integer."""
    return _bytes_to_int(_hash(*args))


def _compute_k() -> int:
    """Compute the SRP multiplier *k* = H(N, g).

    Per SRP-6a, *k* prevents a 2-for-1 guess attack.  HAP uses the
    standard formulation: H(N || PAD(g)).
    """
    return _hash_int(
        _int_to_bytes(SRP_PRIME_3072),
        _int_to_bytes(SRP_GENERATOR),
    )


def _compute_x(salt: bytes, username: bytes, password: bytes) -> int:
    """Compute the private key *x* = H(s, H(I | ':' | P)).

    This is the standard SRP-6a formulation.  HAP sets:
        I = b"Pair-Setup"
        P = the setup code bytes (e.g., b"164-77-432")
    """
    # Inner hash: H(I | ":" | P)
    inner: bytes = _hash(username, b":", password)
    # Outer hash: H(s | inner)
    return _hash_int(salt, inner)


def _compute_u(A: int, B: int) -> int:
    """Compute the scrambling parameter *u* = H(A, B).

    Both public values are padded to the full prime byte length before
    hashing — this is required by SRP-6a to prevent truncation attacks.
    """
    return _hash_int(_int_to_bytes(A), _int_to_bytes(B))


# ---------------------------------------------------------------------------
# SRP Client
# ---------------------------------------------------------------------------

class SrpClient:
    """SRP-6a client for HAP pair-setup.

    Usage::

        client = SrpClient(b"164-77-432")
        client.set_server_params(salt, server_public_key_bytes)
        A = client.get_public_key()
        M1 = client.get_proof()
        # Send A and M1 to the accessory.
        # Receive M2.
        if client.verify_server_proof(M2):
            session_key = client.get_session_key()

    All byte arguments are big-endian.
    """

    def __init__(self, setup_code: bytes) -> None:
        """Initialize the SRP client with the accessory's setup code.

        Args:
            setup_code: The 8-digit code as bytes, e.g. ``b"164-77-432"``.
                Dashes included — HAP uses the display format as-is.
        """
        self._setup_code: bytes = setup_code
        self._N: int = SRP_PRIME_3072
        self._g: int = SRP_GENERATOR
        self._k: int = _compute_k()

        # Private ephemeral value *a* — 32 bytes of randomness (256 bits).
        # Must be kept secret for the duration of the session.
        self._a: int = _bytes_to_int(os.urandom(32))

        # Public ephemeral value *A* = g^a mod N.
        self._A: int = pow(self._g, self._a, self._N)

        # Set after set_server_params().
        self._salt: Optional[bytes] = None
        self._B: Optional[int] = None
        self._S: Optional[int] = None  # Shared secret.
        self._K: Optional[bytes] = None  # Session key.
        self._M1: Optional[bytes] = None  # Client proof.
        self._M2: Optional[bytes] = None  # Expected server proof.

    def get_public_key(self) -> bytes:
        """Return the client's public ephemeral value *A* as bytes.

        Returns:
            Big-endian byte representation, padded to 384 bytes.
        """
        return _int_to_bytes(self._A)

    def set_server_params(self, salt: bytes, B_bytes: bytes) -> None:
        """Accept the server's salt and public ephemeral value *B*.

        Must be called before :meth:`get_proof`.

        Args:
            salt: Server-provided SRP salt (16 bytes per HAP).
            B_bytes: Server's public ephemeral value *B* (big-endian).

        Raises:
            ValueError: If *B* is zero mod N (protocol abort per RFC).
        """
        self._salt = salt
        self._B = _bytes_to_int(B_bytes)

        if self._B % self._N == 0:
            raise ValueError(
                "SRP abort: server public key B is 0 mod N "
                "(possible attack or broken implementation)"
            )

        self._compute_shared_secret()

    def _compute_shared_secret(self) -> None:
        """Derive the shared secret *S*, session key *K*, and proofs."""
        assert self._salt is not None and self._B is not None

        # x = H(salt, H("Pair-Setup" | ":" | setup_code))
        x: int = _compute_x(self._salt, SRP_USERNAME, self._setup_code)

        # u = H(A, B) — scrambling parameter
        u: int = _compute_u(self._A, self._B)
        if u == 0:
            raise ValueError(
                "SRP abort: scrambling parameter u is 0 "
                "(possible attack)"
            )

        # S = (B - k * g^x)^(a + u * x) mod N
        # This is the core SRP-6a computation.
        # k*g^x could be larger than B, so we add N to keep it positive.
        base: int = (self._B - self._k * pow(self._g, x, self._N)) % self._N
        exponent: int = (self._a + u * x) % self._N
        self._S = pow(base, exponent, self._N)

        # K = H(S) — session key (64 bytes, the full SHA-512 output)
        self._K = _hash(_int_to_bytes(self._S))

        # Client proof: M1 = H(H(N) XOR H(g), H(I), s, A, B, K)
        # This proves the client knows the password without revealing it.
        h_N: bytes = _hash(_int_to_bytes(self._N))
        h_g: bytes = _hash(_int_to_bytes(self._g))
        h_xor: bytes = bytes(a ^ b for a, b in zip(h_N, h_g))
        h_I: bytes = _hash(SRP_USERNAME)

        self._M1 = _hash(
            h_xor,
            h_I,
            self._salt,
            _int_to_bytes(self._A),
            _int_to_bytes(self._B),
            self._K,
        )

        # Server proof: M2 = H(A, M1, K)
        self._M2 = _hash(
            _int_to_bytes(self._A),
            self._M1,
            self._K,
        )

    def get_proof(self) -> bytes:
        """Return the client proof *M1*.

        Must be called after :meth:`set_server_params`.

        Returns:
            64-byte SHA-512 proof.

        Raises:
            RuntimeError: If server params have not been set.
        """
        if self._M1 is None:
            raise RuntimeError(
                "Cannot compute proof: call set_server_params() first"
            )
        return self._M1

    def verify_server_proof(self, server_M2: bytes) -> bool:
        """Verify the server's proof *M2*.

        Args:
            server_M2: The 64-byte proof received from the accessory.

        Returns:
            True if the server proved it also knows the setup code.
        """
        if self._M2 is None:
            raise RuntimeError(
                "Cannot verify: call set_server_params() first"
            )
        import hmac
        is_valid: bool = hmac.compare_digest(self._M2, server_M2)
        if not is_valid:
            logger.warning("SRP server proof M2 verification failed")
        return is_valid

    def get_session_key(self) -> bytes:
        """Return the shared session key *K* (64 bytes).

        Used to derive encryption keys for the pair-setup exchange
        (M5/M6 encrypted data).

        Returns:
            64-byte session key.

        Raises:
            RuntimeError: If the shared secret has not been computed.
        """
        if self._K is None:
            raise RuntimeError(
                "Session key not available: complete the SRP handshake first"
            )
        return self._K
