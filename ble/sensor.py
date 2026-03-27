"""BLE sensor daemon — encrypted HAP-BLE reads published to MQTT.

Standalone daemon for Raspberry Pi.  Connects to paired HomeKit BLE
accessories, runs pair-verify to establish an encrypted session, and
polls sensor characteristics (motion, temperature, humidity).

Events are published to MQTT for the GlowUp server to act on.
The server subscribes and triggers group actions (lights on/off,
brightness changes) based on the ``ble_triggers`` configuration.

Architecture (distributed):
    Pi near sensor → BLE → encrypted HAP reads → MQTT publish
    GlowUp server  → MQTT subscribe → trigger group actions

MQTT topics::

    glowup/ble/{label}/motion       — "1" or "0"
    glowup/ble/{label}/temperature  — float Celsius
    glowup/ble/{label}/humidity     — float percentage
    glowup/ble/{label}/status       — JSON health/status

Usage::

    python3 -m ble.sensor
    python3 -m ble.sensor --config /path/to/ble_pairing.json

Press Ctrl+C to stop.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "2.0"

import argparse
import asyncio
import hashlib
import json
import logging
import math
import signal
import struct
import sys
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.ble.sensor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# MQTT topic prefix.
MQTT_PREFIX: str = "glowup/ble"

# Default MQTT broker — pulled from network_config (which reads
# ~/.glowup/network.json or the GLOWUP_NETWORK env var).
try:
    from network_config import net as _net
    DEFAULT_BROKER: str = _net.broker
except Exception:
    DEFAULT_BROKER: str = "localhost"

# Default MQTT port.
DEFAULT_MQTT_PORT: int = 1883

# Seconds between motion polls.  Keep short for responsiveness.
# Temperature/humidity are read every 30s regardless of this value.
POLL_INTERVAL: float = 1.0

# Seconds between reconnection attempts.
RECONNECT_DELAY: float = 5.0

# Maximum reconnect backoff.
MAX_RECONNECT_DELAY: float = 60.0

# Seconds between status publishes.
STATUS_INTERVAL: float = 60.0

# HAP characteristic UUIDs (Apple base).
CHAR_MOTION: str = "00000022-0000-1000-8000-0026bb765291"
CHAR_OCCUPANCY: str = "00000071-0000-1000-8000-0026bb765291"
CHAR_TEMPERATURE: str = "00000011-0000-1000-8000-0026bb765291"
CHAR_HUMIDITY: str = "00000010-0000-1000-8000-0026bb765291"

# HAP-BLE constants.
PAIR_VERIFY_UUID: str = "0000004e-0000-1000-8000-0026bb765291"
PAIR_VERIFY_IID: int = 0x0023
IID_DESC_UUID: str = "dc46f0fe-81d2-4616-b5d9-6abdd796939a"
OP_WRITE: int = 0x02
OP_READ: int = 0x03
HAP_VALUE: int = 0x01
HAP_RETURN_RESP: int = 0x09

# Sensor names for logging and MQTT topics.
# All standard HomeKit sensor characteristic UUIDs we recognize.
# New device types (EVE contact sensors, etc.) can be added here.
SENSOR_NAMES: dict[str, str] = {
    CHAR_MOTION: "motion",
    CHAR_OCCUPANCY: "motion",      # ONVIS uses 0x22, EVE may use 0x71.
    CHAR_TEMPERATURE: "temperature",
    CHAR_HUMIDITY: "humidity",
    # EVE contact sensor (if present).
    # "000000d0-0000-1000-8000-0026bb765291": "contact",
}


# ---------------------------------------------------------------------------
# TLV helpers (self-contained — no imports from ble.tlv for standalone use)
# ---------------------------------------------------------------------------

def _tlv_enc(pairs: list[tuple[int, bytes]]) -> bytes:
    """TLV8 encode."""
    b: bytearray = bytearray()
    for t, v in pairs:
        o, r = 0, len(v)
        if r == 0:
            b.append(t)
            b.append(0)
        while r > 0:
            c: int = min(r, 255)
            b.append(t)
            b.append(c)
            b.extend(v[o : o + c])
            o += c
            r -= c
    return bytes(b)


def _tlv_dec(data: bytes) -> dict[int, bytes]:
    """TLV8 decode to dict (merges fragments)."""
    items: list[tuple[int, bytes]] = []
    pos: int = 0
    while pos < len(data) - 1:
        t: int = data[pos]
        l: int = data[pos + 1]
        pos += 2
        v: bytes = data[pos : pos + l]
        pos += l
        if items and items[-1][0] == t:
            items[-1] = (t, items[-1][1] + v)
        else:
            items.append((t, bytes(v)))
    return dict(items)


# ---------------------------------------------------------------------------
# HKDF + nonce helpers
# ---------------------------------------------------------------------------

def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA-512."""
    try:
        import hkdf as hkdf_lib
        return hkdf_lib.Hkdf(salt, ikm, hashlib.sha512).expand(info, length)
    except ImportError:
        # Fallback to our own implementation.
        import hmac as hm
        if not salt:
            salt = b"\x00" * 64
        prk: bytes = hm.new(salt, ikm, hashlib.sha512).digest()
        okm: bytes = b""
        prev: bytes = b""
        ctr: int = 1
        while len(okm) < length:
            prev = hm.new(
                prk, prev + info + bytes([ctr]), hashlib.sha512
            ).digest()
            okm += prev
            ctr += 1
        return okm[:length]


def _enc_nonce(counter: int) -> bytes:
    """12-byte nonce from counter: 4 zero bytes + 8-byte LE counter."""
    return b"\x00\x00\x00\x00" + struct.pack("<Q", counter)


def _pv_nonce(label: bytes) -> bytes:
    """12-byte nonce for pair-verify: 4 zero bytes + label."""
    return b"\x00\x00\x00\x00" + label


# ---------------------------------------------------------------------------
# HAP-BLE encrypted session
# ---------------------------------------------------------------------------

class HapEncryptedSession:
    """Manages an encrypted HAP-BLE session after pair-verify.

    Provides encrypted characteristic reads using the session keys
    derived from the pair-verify shared secret.

    Attributes:
        connected: Whether the BLE connection is active.
    """

    def __init__(self, client, c2a_key: bytes, a2c_key: bytes) -> None:
        """Initialize with a connected BleakClient and session keys.

        Args:
            client: Connected bleak.BleakClient.
            c2a_key: Controller-to-accessory encryption key (32 bytes).
            a2c_key: Accessory-to-controller decryption key (32 bytes).
        """
        self._client = client
        self._c2a_key: bytes = c2a_key
        self._a2c_key: bytes = a2c_key
        self._c2a_ctr: int = 0
        self._a2c_ctr: int = 0
        self._tid: int = 1

    def _next_tid(self) -> int:
        """Allocate next transaction ID."""
        t: int = self._tid
        self._tid = (t + 1) % 256
        return t

    @property
    def connected(self) -> bool:
        """True if the BLE connection is active."""
        return self._client.is_connected

    async def read_characteristic(
        self, char_uuid: str, iid: int
    ) -> Optional[bytes]:
        """Read a characteristic value via encrypted HAP PDU.

        The entire PDU (header) is encrypted before writing.
        The response is fully encrypted and must be decrypted.

        Args:
            char_uuid: GATT characteristic UUID.
            iid: HAP instance ID.

        Returns:
            Raw value bytes, or None on failure.
        """
        from cryptography.hazmat.primitives.ciphers.aead import (
            ChaCha20Poly1305,
        )

        tid: int = self._next_tid()

        # Build 5-byte read PDU and encrypt the ENTIRE thing.
        plaintext: bytes = struct.pack("<BBBH", 0x00, OP_READ, tid, iid)
        ct: bytes = ChaCha20Poly1305(self._c2a_key).encrypt(
            _enc_nonce(self._c2a_ctr), plaintext, b""
        )
        self._c2a_ctr += 1

        # Write encrypted PDU to the characteristic.
        await self._client.write_gatt_char(char_uuid, ct, response=True)
        # Wait for accessory to process.  500ms balances latency vs
        # reliability — 200ms caused ATT 0x0E errors on the ONVIS
        # when polled rapidly.
        await asyncio.sleep(0.5)

        # Read encrypted response.
        resp: bytes = bytes(await self._client.read_gatt_char(char_uuid))

        # Decrypt entire response.  If decryption fails (nonce
        # desync, corrupt data), increment the counter anyway — the
        # accessory already incremented its counter when it sent
        # the response, so we must stay in sync even on failure.
        try:
            dec: bytes = ChaCha20Poly1305(self._a2c_key).decrypt(
                _enc_nonce(self._a2c_ctr), resp, b""
            )
            self._a2c_ctr += 1
        except Exception as exc:
            self._a2c_ctr += 1  # Stay in sync.
            logger.warning("Decrypt failed for IID=%d: %s", iid, exc)
            return None

        # Parse: control(1) + TID(1) + status(1) + [body_len(2) + body]
        if len(dec) < 3:
            return None
        status: int = dec[2]
        if status != 0:
            logger.warning("HAP read IID=%d status=%d", iid, status)
            return None
        if len(dec) < 5:
            return b""

        body_len: int = struct.unpack_from("<H", dec, 3)[0]
        body: bytes = dec[5 : 5 + body_len]
        body_tlv: dict[int, bytes] = _tlv_dec(body) if body else {}
        return body_tlv.get(HAP_VALUE, body)


# ---------------------------------------------------------------------------
# Pair-verify helper
# ---------------------------------------------------------------------------

async def pair_verify(
    client, ltsk: bytes, acc_ltpk: bytes
) -> Optional[HapEncryptedSession]:
    """Run pair-verify and return an encrypted session.

    Args:
        client: Connected bleak.BleakClient.
        ltsk: Controller Ed25519 private key (32 bytes).
        acc_ltpk: Accessory Ed25519 public key (32 bytes).

    Returns:
        :class:`HapEncryptedSession` on success, None on failure.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    tid_ctr: list[int] = [1]

    def nt() -> int:
        t: int = tid_ctr[0]
        tid_ctr[0] = (t + 1) % 256
        return t

    try:
        # M1: send ephemeral public key.
        esk = X25519PrivateKey.generate()
        epk: bytes = esk.public_key().public_bytes_raw()

        m1: bytes = _tlv_enc([(0x06, b"\x01"), (0x03, epk)])
        body: bytes = _tlv_enc([(HAP_RETURN_RESP, b"\x01"), (HAP_VALUE, m1)])
        pdu: bytes = struct.pack(
            "<BBBHH", 0x00, OP_WRITE, nt(), PAIR_VERIFY_IID, len(body)
        ) + body
        await client.write_gatt_char(PAIR_VERIFY_UUID, pdu, response=True)
        await asyncio.sleep(2)

        # Read M2 response.
        r: bytearray = bytearray(await client.read_gatt_char(PAIR_VERIFY_UUID))
        if len(r) >= 5:
            exp: int = struct.unpack_from("<H", r, 3)[0]
            for _ in range(30):
                if len(r) - 5 >= exp:
                    break
                await asyncio.sleep(0.3)
                r.extend(await client.read_gatt_char(PAIR_VERIFY_UUID))

        m2: dict[int, bytes] = _tlv_dec(_tlv_dec(bytes(r[5:]))[HAP_VALUE])
        if 0x07 in m2:
            logger.error("Pair-verify M2 error: %s", m2[0x07].hex())
            return None

        # Compute shared secret and derive verify key.
        shared: bytes = esk.exchange(
            X25519PublicKey.from_public_bytes(m2[0x03])
        )
        vk: bytes = _hkdf(
            shared, b"Pair-Verify-Encrypt-Salt", b"Pair-Verify-Encrypt-Info"
        )

        # Decrypt and verify M2 sub-TLV.
        m2_dec: bytes = ChaCha20Poly1305(vk).decrypt(
            _pv_nonce(b"PV-Msg02"), m2[0x05], b""
        )
        m2_sub: dict[int, bytes] = _tlv_dec(m2_dec)
        acc_info: bytes = m2[0x03] + m2_sub[0x01] + epk
        Ed25519PublicKey.from_public_bytes(acc_ltpk).verify(
            m2_sub[0x0A], acc_info
        )

        # M3: send controller proof.
        ctrl_info: bytes = epk + b"GlowUp" + m2[0x03]
        ctrl_sig: bytes = Ed25519PrivateKey.from_private_bytes(ltsk).sign(
            ctrl_info
        )
        sub: bytes = _tlv_enc([(0x01, b"GlowUp"), (0x0A, ctrl_sig)])
        enc_sub: bytes = ChaCha20Poly1305(vk).encrypt(
            _pv_nonce(b"PV-Msg03"), sub, b""
        )
        m3: bytes = _tlv_enc([(0x06, b"\x03"), (0x05, enc_sub)])
        body3: bytes = _tlv_enc(
            [(HAP_RETURN_RESP, b"\x01"), (HAP_VALUE, m3)]
        )
        pdu3: bytes = struct.pack(
            "<BBBHH", 0x00, OP_WRITE, nt(), PAIR_VERIFY_IID, len(body3)
        ) + body3
        await client.write_gatt_char(
            PAIR_VERIFY_UUID, pdu3, response=True
        )
        await asyncio.sleep(1)
        await client.read_gatt_char(PAIR_VERIFY_UUID)  # M4 ack

        # Derive session keys.
        c2a: bytes = _hkdf(
            shared, b"Control-Salt", b"Control-Write-Encryption-Key"
        )
        a2c: bytes = _hkdf(
            shared, b"Control-Salt", b"Control-Read-Encryption-Key"
        )

        logger.info("Pair-verify complete — encrypted session established")
        return HapEncryptedSession(client, c2a, a2c)

    except Exception as exc:
        logger.error("Pair-verify failed: %s", exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# IID discovery
# ---------------------------------------------------------------------------

async def discover_sensor_iids(
    client,
) -> dict[str, int]:
    """Discover IIDs for sensor characteristics.

    Returns:
        Dict mapping characteristic UUID to IID.
    """
    result: dict[str, int] = {}
    for svc in client.services:
        for ch in svc.characteristics:
            if ch.uuid in SENSOR_NAMES:
                iid: int = ch.handle  # fallback
                for desc in ch.descriptors:
                    if IID_DESC_UUID in desc.uuid.lower():
                        try:
                            val: bytearray = (
                                await client.read_gatt_descriptor(desc.handle)
                            )
                            iid = int.from_bytes(val, "little")
                        except Exception:
                            pass
                result[ch.uuid] = iid
                logger.info(
                    "  %s: IID=%d", SENSOR_NAMES[ch.uuid], iid
                )
    return result


# ---------------------------------------------------------------------------
# MQTT publisher
# ---------------------------------------------------------------------------

class MqttPublisher:
    """Publishes BLE sensor events to MQTT."""

    def __init__(
        self,
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_MQTT_PORT,
    ) -> None:
        self._broker: str = broker
        self._port: int = port
        self._client: Any = None
        self._connected: bool = False

    def connect(self) -> None:
        """Connect to MQTT broker."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError("pip install paho-mqtt")

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"glowup-ble-{int(time.time())}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected to %s:%d", self._broker, self._port)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            logger.warning("MQTT disconnected (rc=%d), will reconnect", rc)

    def publish(self, label: str, subtopic: str, payload: str) -> None:
        """Publish an event.

        Uses try/except rather than checking the _connected flag to
        avoid a TOCTOU race during broker reconnection.
        """
        topic: str = f"{MQTT_PREFIX}/{label}/{subtopic}"
        if not self._client:
            return
        try:
            self._client.publish(topic, payload, qos=1, retain=True)
        except Exception:
            logger.debug("MQTT publish failed — dropping %s", topic)

    def disconnect(self) -> None:
        """Disconnect."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()


# ---------------------------------------------------------------------------
# Main sensor loop
# ---------------------------------------------------------------------------

async def monitor_device(
    label: str,
    address: str,
    pairing: dict,
    publisher: MqttPublisher,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    """Connect, verify, and poll a single device.

    Runs until the BLE connection drops, then returns (caller retries).
    """
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        raise ImportError("pip install bleak")

    logger.info("Scanning for %s (%s)...", label, address)
    ble_dev = await BleakScanner.find_device_by_address(address, timeout=15)
    if ble_dev is None:
        logger.warning("%s not found", label)
        return

    async with BleakClient(ble_dev, timeout=30) as client:
        logger.info("Connected to %s", label)

        # Pair-verify.
        ctrl_ltsk: bytes = bytes.fromhex(pairing["controller"]["ltsk"])
        acc_ltpk: bytes = bytes.fromhex(
            pairing["devices"][label]["accessory_ltpk"]
        )
        session: Optional[HapEncryptedSession] = await pair_verify(
            client, ctrl_ltsk, acc_ltpk
        )
        if session is None:
            logger.error("Pair-verify failed for %s", label)
            return

        # Discover sensor IIDs.
        iids: dict[str, int] = await discover_sensor_iids(client)
        if not iids:
            logger.error("No sensor characteristics found for %s", label)
            return

        publisher.publish(label, "status", json.dumps({
            "state": "monitoring",
            "address": address,
            "sensors": list(SENSOR_NAMES[u] for u in iids),
            "timestamp": time.time(),
        }))

        # Poll loop.
        #
        # Motion is read every cycle for responsiveness.
        # Temperature and humidity change slowly — read them every
        # ENV_READ_INTERVAL seconds to avoid wasting BLE bandwidth.
        last_values: dict[str, bytes] = {}
        last_status: float = time.time()
        last_env_read: float = 0.0
        ENV_READ_INTERVAL: float = 30.0  # seconds between temp/humidity reads

        # Separate motion IIDs from environmental IIDs.
        motion_iids: dict[str, int] = {
            u: i for u, i in iids.items()
            if SENSOR_NAMES.get(u) == "motion"
        }
        env_iids: dict[str, int] = {
            u: i for u, i in iids.items()
            if SENSOR_NAMES.get(u) in ("temperature", "humidity")
        }

        while session.connected:
            # Always read motion — this is the latency-critical path.
            for uuid, iid in motion_iids.items():
                try:
                    val: Optional[bytes] = await session.read_characteristic(
                        uuid, iid
                    )
                except Exception as exc:
                    logger.warning("motion read error: %s", exc)
                    break

                if val is not None and val != last_values.get(uuid):
                    last_values[uuid] = val
                    payload: str = str(val[0]) if val else "0"
                    publisher.publish(label, "motion", payload)
                    logger.info("%s motion=%s", label, payload)

            # Read temperature/humidity less frequently.
            now: float = time.time()
            if now - last_env_read >= ENV_READ_INTERVAL:
                last_env_read = now
                for uuid, iid in env_iids.items():
                    name: str = SENSOR_NAMES[uuid]
                    try:
                        val = await session.read_characteristic(uuid, iid)
                    except Exception as exc:
                        logger.warning("%s read error: %s", name, exc)
                        break

                    if val is None or val == last_values.get(uuid):
                        continue
                    last_values[uuid] = val

                    if name == "temperature" and len(val) == 4:
                        temp: float = struct.unpack("<f", val)[0]
                        publisher.publish(
                            label, "temperature", f"{temp:.1f}"
                        )
                        logger.info("%s temp=%.1f°C", label, temp)
                    elif name == "humidity" and len(val) == 4:
                        hum: float = struct.unpack("<f", val)[0]
                        publisher.publish(
                            label, "humidity", f"{hum:.1f}"
                        )
                        logger.info("%s humidity=%.1f%%", label, hum)

            # Periodic status.
            if now - last_status >= STATUS_INTERVAL:
                publisher.publish(label, "status", json.dumps({
                    "state": "monitoring",
                    "last_values": {
                        SENSOR_NAMES[u]: v.hex()
                        for u, v in last_values.items()
                    },
                    "timestamp": now,
                }))
                last_status = now

            await asyncio.sleep(poll_interval)

    logger.warning("%s: BLE disconnected", label)
    publisher.publish(label, "status", json.dumps({
        "state": "disconnected",
        "timestamp": time.time(),
    }))


async def _monitor_with_reconnect(
    label: str,
    address: str,
    pairing: dict,
    publisher: MqttPublisher,
    poll_interval: float,
) -> None:
    """Monitor a single device with auto-reconnect.

    Runs forever (until cancelled), reconnecting with exponential
    backoff when the BLE connection drops.
    """
    delay: float = RECONNECT_DELAY
    while True:
        try:
            await monitor_device(
                label, address, pairing, publisher, poll_interval
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("%s: error — %s", label, exc, exc_info=True)

        logger.info("Reconnecting to %s in %.0fs...", label, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, MAX_RECONNECT_DELAY)


async def run_daemon(
    config_path: str = "ble_pairing.json",
    broker: str = DEFAULT_BROKER,
    mqtt_port: int = DEFAULT_MQTT_PORT,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    """Run the BLE sensor daemon with concurrent multi-device support.

    Each paired device gets its own monitor task with independent
    reconnect logic.  All tasks run concurrently via asyncio.gather.
    """
    with open(config_path) as f:
        pairing: dict = json.load(f)

    paired: list[str] = [
        label
        for label, dev in pairing.get("devices", {}).items()
        if dev.get("paired")
    ]
    if not paired:
        logger.error("No paired devices in %s", config_path)
        return

    publisher = MqttPublisher(broker=broker, port=mqtt_port)
    publisher.connect()

    logger.info(
        "BLE sensor daemon starting — %d device(s): %s",
        len(paired), ", ".join(paired),
    )

    # Launch one monitor task per device — they run concurrently.
    tasks: list[asyncio.Task] = []
    for label in paired:
        address: str = pairing["devices"][label]["address"]
        task: asyncio.Task = asyncio.create_task(
            _monitor_with_reconnect(
                label, address, pairing, publisher, poll_interval
            ),
            name=f"ble-{label}",
        )
        tasks.append(task)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        publisher.disconnect()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="GlowUp BLE sensor daemon",
    )
    parser.add_argument(
        "--config",
        default="ble_pairing.json",
        help="Path to ble_pairing.json",
    )
    parser.add_argument(
        "--broker",
        default=DEFAULT_BROKER,
        help=f"MQTT broker (default: {DEFAULT_BROKER})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_MQTT_PORT,
        help=f"MQTT port (default: {DEFAULT_MQTT_PORT})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL,
        help=f"Seconds between polls (default: {POLL_INTERVAL})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    loop = asyncio.new_event_loop()

    def _shutdown(sig, frame):
        logger.info("Shutting down (signal %d)", sig)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(
            run_daemon(
                config_path=args.config,
                broker=args.broker,
                mqtt_port=args.port,
                poll_interval=args.poll_interval,
            )
        )
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
