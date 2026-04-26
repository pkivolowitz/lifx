"""GlowUp SDR service — rtl_433 decoded signals published to hub MQTT.

Service-pattern producer (see docs/35-service-vs-adapter.md) running on
a dedicated Pi with an RTL-SDR dongle.  Spawns ``rtl_433`` as a
subprocess with JSON output, classifies each decoded packet, and
publishes normalized signals cross-host to the hub mosquitto.

Architecture (distributed)::

    SDR Pi: RTL-SDR dongle → rtl_433 → JSON stdout
                                        ↓
                               this service.py
                                        ↓
                              cross-host MQTT publish
                                        ↓
                                hub mosquitto (.214)
                                        ↓
                         _on_remote_signal → SignalBus
                         glowup/sdr/status/{label} → status store

MQTT topics published (all qos=0, retain=False)::

    glowup/signals/{label}:{property}    numeric scalars
    glowup/sdr/status/{label}            full JSON packet from rtl_433
    glowup/sdr/raw                       every decoded packet (diagnostic)

The hub's existing _on_remote_signal callback consumes
glowup/signals/# — no hub-side code change needed.

Device labeling: ``{model}_{id}`` normalized to lowercase with
underscores.  Model comes from rtl_433's ``model`` field, ID from
``id`` (integer or hex).  Devices without an ID use the model alone
(deduplicated by the hub's SignalBus).

Channel control: the service subscribes to ``glowup/sdr/command`` on
the hub broker.  Publishing ``{"frequency": 315000000}`` or
``{"frequency": 433920000}`` restarts rtl_433 on the new frequency.
The hub exposes this via POST /api/sdr/frequency.

Usage::

    python3 -m sdr.service --config /etc/glowup/sdr_config.json
    python3 -m sdr.service --frequency 433920000

Requires:
    - rtl_433 installed and in PATH
    - RTL-SDR dongle plugged in (lsusb shows RTL2832U)
    - paho-mqtt (pip install paho-mqtt)
    - Network reachability to hub mosquitto
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Optional

logger: logging.Logger = logging.getLogger("glowup.sdr_service")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hub broker — read from GLB_HUB_BROKER.  No hardcoded IP fallback;
# any specific address is fleet-topology, not generic GlowUp source.
# /etc/default/glowup-sdr (deployed by glowup-infra) provides the
# value via EnvironmentFile= in the systemd unit.  If neither env nor
# --hub-broker is supplied, the program exits at startup.
DEFAULT_HUB_BROKER: str | None = os.environ.get("GLB_HUB_BROKER") or None

# Default MQTT port.
DEFAULT_MQTT_PORT: int = int(os.environ.get("GLB_HUB_PORT", "1883"))

# Default frequency (Hz).  433.92 MHz is the richest ISM band in the US:
# weather stations, TPMS, garage doors, soil sensors, doorbells.
DEFAULT_FREQUENCY: int = int(os.environ.get("GLB_SDR_FREQ", "433920000"))

# MQTT topic prefixes — must match the hub's subscription patterns.
SIGNAL_PREFIX: str = "glowup/signals"
STATUS_PREFIX: str = "glowup/sdr/status"
RAW_TOPIC: str = "glowup/sdr/raw"
COMMAND_TOPIC: str = "glowup/sdr/command"

# Properties extracted per device type.  Keys map rtl_433 JSON fields
# to glowup signal property names.  Only properties listed here are
# published on glowup/signals/; everything else goes to the status
# blob only.  Add entries as new device types are encountered.
DEVICE_PROPERTIES: dict[str, dict[str, str]] = {
    # TPMS sensors (315 MHz in US, some on 433 MHz)
    "tpms": {
        "pressure_kPa": "pressure",
        "pressure_PSI": "pressure_psi",
        "temperature_C": "temperature",
        "temperature_F": "temperature_f",
    },
    # Weather stations (Acurite, LaCrosse, Oregon Scientific, etc.)
    "weather": {
        "temperature_C": "temperature",
        "temperature_F": "temperature_f",
        "humidity": "humidity",
        "wind_avg_km_h": "wind_speed",
        "wind_max_km_h": "wind_gust",
        "rain_mm": "rain",
        "pressure_hPa": "pressure",
        "uv": "uv_index",
    },
    # Generic (catch-all for unclassified devices)
    "generic": {
        "temperature_C": "temperature",
        "temperature_F": "temperature_f",
        "humidity": "humidity",
        "battery_ok": "battery",
    },
}

# Model substrings that classify a device type.
# Checked in order — first match wins.
DEVICE_TYPE_RULES: list[tuple[str, str]] = [
    ("tpms", "tpms"),
    ("tire", "tpms"),
    ("toyota", "tpms"),
    ("ford", "tpms"),
    ("schrader", "tpms"),
    ("acurite", "weather"),
    ("lacrosse", "weather"),
    ("oregon", "weather"),
    ("fineoffset", "weather"),
    ("ambient", "weather"),
    ("ws-", "weather"),
    ("ecowitt", "weather"),
    ("bresser", "weather"),
    ("davis", "weather"),
]

# Minimum seconds between publishes for the same device+property.
# Prevents flooding the bus when rtl_433 decodes the same
# transmission multiple times.
DEDUP_INTERVAL_S: float = 2.0

# Seconds between rtl_433 restart attempts on failure.
RESTART_DELAY_S: float = 5.0

# ---------------------------------------------------------------------------
# Label normalization
# ---------------------------------------------------------------------------

# Strip non-alphanumeric characters and collapse runs of underscores.
_LABEL_RE: re.Pattern = re.compile(r"[^a-z0-9]+")


def normalize_label(model: str, device_id: Any) -> str:
    """Build a stable, slug-safe label from rtl_433 model + id.

    Args:
        model:     Model string from rtl_433 (e.g. "Acurite-Tower").
        device_id: Integer or hex ID, or None.

    Returns:
        Lowercase slug like ``acurite_tower_12345``.
    """
    raw: str = model
    if device_id is not None:
        raw = f"{model}_{device_id}"
    return _LABEL_RE.sub("_", raw.lower()).strip("_")


def classify_device(model: str) -> str:
    """Classify an rtl_433 model string into a device type.

    Args:
        model: Model string from rtl_433.

    Returns:
        Device type key from DEVICE_PROPERTIES.
    """
    model_lower: str = model.lower()
    for substring, dtype in DEVICE_TYPE_RULES:
        if substring in model_lower:
            return dtype
    return "generic"


# ---------------------------------------------------------------------------
# MQTT publisher (same pattern as ble/sensor.py MqttPublisher)
# ---------------------------------------------------------------------------

class MqttPublisher:
    """Publishes SDR events cross-host to the hub mosquitto.

    Service-pattern producer: opens its own paho client to the hub
    broker.  Numeric signals go on ``glowup/signals/{label}:{prop}``
    and feed the hub's _on_remote_signal callback.  The full rtl_433
    JSON goes on ``glowup/sdr/status/{label}`` for diagnostic UI.

    Also publishes every decoded packet to ``glowup/sdr/raw`` for
    debugging and new-device discovery.

    All publishes use qos=0 retain=False — sensor data must NEVER be
    retained (see feedback_multi_topic_config_deletion).
    """

    def __init__(
        self,
        hub_broker: str = DEFAULT_HUB_BROKER,
        hub_port: int = DEFAULT_MQTT_PORT,
        on_command: Optional[Any] = None,
    ) -> None:
        self._broker: str = hub_broker
        self._port: int = hub_port
        self._client: Any = None
        self._connected: bool = False
        self._on_command = on_command
        # Deduplication: (label, prop) → last_publish_time
        self._last_pub: dict[tuple[str, str], float] = {}

    def connect(self) -> None:
        """Open the cross-host paho client to the hub broker."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise ImportError("pip install paho-mqtt")

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"glowup-sdr-{int(time.time())}",
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self._broker, self._port)
        self._client.loop_start()
        logger.info(
            "SDR→hub publisher connecting to %s:%d",
            self._broker, self._port,
        )

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            logger.info("MQTT connected to %s:%d", self._broker, self._port)
            # Subscribe to command topic for frequency changes.
            client.subscribe(COMMAND_TOPIC, qos=1)
            logger.info("Subscribed to %s", COMMAND_TOPIC)
        else:
            logger.warning("MQTT connect failed rc=%s — paho will retry", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            logger.warning("MQTT disconnected (rc=%d), will reconnect", rc)

    def _on_message(self, client, userdata, msg):
        """Handle inbound commands (frequency changes)."""
        if msg.topic == COMMAND_TOPIC and self._on_command:
            try:
                payload: dict = json.loads(msg.payload.decode())
                self._on_command(payload)
            except Exception as exc:
                logger.warning("Bad command payload: %s", exc)

    def disconnect(self) -> None:
        """Clean shutdown."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            logger.info("MQTT disconnected (clean)")

    def publish_signal(
        self, label: str, prop: str, value: str,
    ) -> None:
        """Publish a single numeric signal to glowup/signals/."""
        now: float = time.time()
        key: tuple[str, str] = (label, prop)
        last: float = self._last_pub.get(key, 0.0)
        if now - last < DEDUP_INTERVAL_S:
            return
        self._last_pub[key] = now

        topic: str = f"{SIGNAL_PREFIX}/{label}:{prop}"
        self._pub(topic, value)

    def publish_status(self, label: str, payload: str) -> None:
        """Publish full rtl_433 JSON to glowup/sdr/status/{label}."""
        self._pub(f"{STATUS_PREFIX}/{label}", payload)

    def publish_raw(self, payload: str) -> None:
        """Publish raw rtl_433 JSON to glowup/sdr/raw."""
        self._pub(RAW_TOPIC, payload)

    def _pub(self, topic: str, payload: str) -> None:
        """Low-level publish with error logging."""
        if not self._client:
            return
        try:
            info = self._client.publish(topic, payload, qos=0, retain=False)
            if info.rc != 0:
                logger.warning("publish %s rc=%s", topic, info.rc)
        except Exception as exc:
            logger.warning("publish %s raised: %s", topic, exc)


# ---------------------------------------------------------------------------
# rtl_433 subprocess manager
# ---------------------------------------------------------------------------

class Rtl433Runner:
    """Manages the rtl_433 subprocess lifecycle.

    Spawns rtl_433 with JSON output on stdout, reads decoded packets
    line by line, and calls the packet handler for each one.  Handles
    restart on crash and frequency changes.
    """

    def __init__(
        self,
        frequency: int = DEFAULT_FREQUENCY,
        device_index: int = 0,
        gain: Optional[str] = None,
        on_packet: Optional[Any] = None,
    ) -> None:
        self._frequency: int = frequency
        self._device_index: int = device_index
        self._gain: Optional[str] = gain
        self._on_packet = on_packet
        self._proc: Optional[subprocess.Popen] = None
        self._running: bool = False
        self._lock: threading.Lock = threading.Lock()

    @property
    def frequency(self) -> int:
        """Current frequency in Hz."""
        return self._frequency

    def start(self) -> None:
        """Start the rtl_433 subprocess."""
        self._running = True
        self._spawn()

    def stop(self) -> None:
        """Stop the rtl_433 subprocess."""
        self._running = False
        self._kill()

    def set_frequency(self, freq_hz: int) -> None:
        """Change frequency — restarts rtl_433.

        Args:
            freq_hz: New frequency in Hz.
        """
        logger.info(
            "Frequency change: %d Hz → %d Hz",
            self._frequency, freq_hz,
        )
        self._frequency = freq_hz
        if self._running:
            self._kill()
            self._spawn()

    def run_forever(self) -> None:
        """Block, reading rtl_433 output and restarting on failure.

        Call from the main thread.  Returns when stop() is called.
        """
        while self._running:
            try:
                self._read_loop()
            except Exception as exc:
                if self._running:
                    logger.error("rtl_433 read loop error: %s", exc)
            if self._running:
                logger.info(
                    "rtl_433 exited — restarting in %ds", RESTART_DELAY_S,
                )
                time.sleep(RESTART_DELAY_S)
                self._spawn()

    def _build_cmd(self) -> list[str]:
        """Build the rtl_433 command line."""
        cmd: list[str] = [
            "rtl_433",
            "-f", str(self._frequency),
            "-d", str(self._device_index),
            "-F", "json",
            # Decode all known protocols.
            "-M", "time:utc",
            "-M", "level",
        ]
        if self._gain is not None:
            cmd.extend(["-g", self._gain])
        return cmd

    def _spawn(self) -> None:
        """Spawn the rtl_433 process."""
        with self._lock:
            self._kill()
            cmd: list[str] = self._build_cmd()
            logger.info("Starting: %s", " ".join(cmd))
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                logger.error(
                    "rtl_433 not found in PATH — install with: "
                    "sudo apt install rtl-433"
                )
                self._running = False
                return
            # Log stderr in a background thread so rtl_433 startup
            # messages and warnings are visible in journalctl.
            t = threading.Thread(
                target=self._stderr_reader,
                daemon=True,
                name="rtl433-stderr",
            )
            t.start()
            logger.info(
                "rtl_433 started (pid=%d, freq=%d Hz, %.3f MHz)",
                self._proc.pid, self._frequency,
                self._frequency / 1_000_000,
            )

    def _kill(self) -> None:
        """Kill the running rtl_433 process if any."""
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
                except Exception as exc:
                    logger.debug("Error stopping rtl_433 process: %s", exc)
                logger.info("rtl_433 stopped (pid=%d)", self._proc.pid)
                self._proc = None

    def _read_loop(self) -> None:
        """Read JSON lines from rtl_433 stdout."""
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            if not self._running:
                break
            line = line.strip()
            if not line:
                continue
            try:
                packet: dict = json.loads(line)
                if self._on_packet:
                    self._on_packet(packet)
            except json.JSONDecodeError:
                logger.debug("Non-JSON from rtl_433: %s", line[:120])
        # Process ended.
        if self._proc is not None:
            rc: int = self._proc.wait()
            logger.warning("rtl_433 exited with code %d", rc)

    def _stderr_reader(self) -> None:
        """Drain rtl_433 stderr and log it."""
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            line = line.strip()
            if line:
                logger.debug("[rtl_433 stderr] %s", line)


# ---------------------------------------------------------------------------
# Packet handler — the bridge between rtl_433 and MQTT
# ---------------------------------------------------------------------------

class PacketHandler:
    """Classifies rtl_433 packets and publishes to MQTT.

    Each decoded packet is:
    1. Labeled (model + id → slug)
    2. Classified (TPMS, weather, generic)
    3. Published: numeric properties → glowup/signals/{label}:{prop},
       full JSON → glowup/sdr/status/{label},
       raw JSON → glowup/sdr/raw
    """

    def __init__(self, publisher: MqttPublisher) -> None:
        self._pub: MqttPublisher = publisher
        # Track unique devices seen this session.
        self._seen_devices: dict[str, str] = {}  # label → model

    @property
    def device_count(self) -> int:
        """Number of unique devices seen this session."""
        return len(self._seen_devices)

    def handle(self, packet: dict) -> None:
        """Process a single rtl_433 decoded packet.

        Args:
            packet: Decoded JSON dict from rtl_433 stdout.
        """
        model: Optional[str] = packet.get("model")
        if model is None:
            return

        device_id: Any = packet.get("id")
        label: str = normalize_label(model, device_id)
        dtype: str = classify_device(model)

        # Track new devices.
        if label not in self._seen_devices:
            self._seen_devices[label] = model
            logger.info(
                "New device: %s (model=%s, type=%s, id=%s)",
                label, model, dtype, device_id,
            )

        # Publish raw packet for discovery/debugging.
        raw_json: str = json.dumps(packet)
        self._pub.publish_raw(raw_json)

        # Publish full status blob.
        self._pub.publish_status(label, raw_json)

        # Extract and publish numeric properties.
        prop_map: dict[str, str] = DEVICE_PROPERTIES.get(
            dtype, DEVICE_PROPERTIES["generic"]
        )
        for rtl_key, signal_prop in prop_map.items():
            value: Any = packet.get(rtl_key)
            if value is not None:
                self._pub.publish_signal(label, signal_prop, str(value))


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

def run_daemon(
    config_path: Optional[str] = None,
    hub_broker: str = DEFAULT_HUB_BROKER,
    hub_port: int = DEFAULT_MQTT_PORT,
    frequency: int = DEFAULT_FREQUENCY,
    device_index: int = 0,
    gain: Optional[str] = None,
) -> None:
    """Run the SDR service daemon.

    Args:
        config_path: Path to JSON config file (optional).
        hub_broker:  Hub mosquitto address.
        hub_port:    Hub mosquitto port.
        frequency:   Initial frequency in Hz.
        device_index: RTL-SDR device index (for multiple dongles).
        gain:        RTL-SDR gain setting (None = auto).
    """
    # Load config file if provided.
    if config_path is not None:
        with open(config_path) as f:
            config: dict = json.load(f)
        hub_broker = config.get("hub_broker", hub_broker)
        hub_port = config.get("hub_port", hub_port)
        frequency = config.get("frequency", frequency)
        device_index = config.get("device_index", device_index)
        gain = config.get("gain", gain)

    # Wire up the components.
    runner: Optional[Rtl433Runner] = None

    def on_command(payload: dict) -> None:
        """Handle MQTT command messages (frequency changes)."""
        freq: Optional[int] = payload.get("frequency")
        if freq is not None and runner is not None:
            runner.set_frequency(int(freq))

    publisher = MqttPublisher(
        hub_broker=hub_broker,
        hub_port=hub_port,
        on_command=on_command,
    )
    publisher.connect()

    handler = PacketHandler(publisher)

    runner = Rtl433Runner(
        frequency=frequency,
        device_index=device_index,
        gain=gain,
        on_packet=handler.handle,
    )

    logger.info(
        "SDR service starting — freq=%d Hz (%.3f MHz), "
        "hub=%s:%d, device=%d",
        frequency, frequency / 1_000_000,
        hub_broker, hub_port, device_index,
    )

    runner.start()

    try:
        runner.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        publisher.disconnect()
        logger.info(
            "SDR service stopped — %d unique devices seen",
            handler.device_count,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="GlowUp SDR service — rtl_433 → MQTT",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to sdr_config.json",
    )
    parser.add_argument(
        "--hub-broker",
        default=DEFAULT_HUB_BROKER,
        help=(
            "Hub mosquitto address.  Default from GLB_HUB_BROKER env "
            "var (set via EnvironmentFile=-/etc/default/glowup-sdr in "
            "the systemd unit).  No hardcoded fallback — required."
        ),
    )
    parser.add_argument(
        "--hub-port",
        type=int,
        default=DEFAULT_MQTT_PORT,
        help=f"Hub mosquitto port (default: {DEFAULT_MQTT_PORT})",
    )
    parser.add_argument(
        "--frequency", "-f",
        type=int,
        default=DEFAULT_FREQUENCY,
        help=(
            f"Frequency in Hz (default: {DEFAULT_FREQUENCY}, "
            "from GLB_SDR_FREQ env var).  "
            "Common: 433920000 (433.92 MHz), 315000000 (315 MHz)"
        ),
    )
    parser.add_argument(
        "--device", "-d",
        type=int,
        default=0,
        dest="device_index",
        help="RTL-SDR device index (default: 0)",
    )
    parser.add_argument(
        "--gain", "-g",
        default=None,
        help="RTL-SDR gain (default: auto). Use 'auto' or a dB value.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Debug logging",
    )
    args = parser.parse_args()

    # Fail fast — no broker means we can't run.
    if not args.hub_broker:
        parser.error(
            "no hub broker configured: set GLB_HUB_BROKER (typically "
            "via /etc/default/glowup-sdr) or pass --hub-broker"
        )

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Graceful shutdown on SIGTERM/SIGINT.
    def _shutdown(sig, frame):
        logger.info("Shutting down (signal %d)", sig)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run_daemon(
        config_path=args.config,
        hub_broker=args.hub_broker,
        hub_port=args.hub_port,
        frequency=args.frequency,
        device_index=args.device_index,
        gain=args.gain,
    )


if __name__ == "__main__":
    main()
