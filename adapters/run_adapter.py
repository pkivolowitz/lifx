"""Unified entry point for all GlowUp adapter processes.

Runs any adapter in its own process, isolated from the server's GIL.
Each adapter type is registered in a dispatch table with a factory
function, optional command handler, and optional post-start hook.

Adapter-specific dependencies are lazy-imported inside factories so
a missing ``vivintpy`` doesn't prevent the Zigbee adapter from
starting.

Usage::

    python -m adapters.run_adapter --adapter zigbee --config /etc/glowup/server.json
    python -m adapters.run_adapter --adapter vivint --config /etc/glowup/server.json
    python -m adapters.run_adapter --adapter nvr    --config /etc/glowup/server.json
    python -m adapters.run_adapter --adapter printer --config /etc/glowup/server.json
    python -m adapters.run_adapter --adapter matter  --config /etc/glowup/server.json
    python -m adapters.run_adapter --adapter ble     --config /etc/glowup/server.json

Each invocation is a separate OS process with its own PID and GIL.
Systemd services pass different ``--adapter`` flags to the same module.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import http.server
import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from adapters.process_base import ProcessAdapterBase, MqttSignalBus

logger: logging.Logger = logging.getLogger("glowup.run_adapter")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default MQTT broker for GlowUp lifecycle (LWT, heartbeat, commands).
DEFAULT_BROKER: str = "localhost"

# Default MQTT broker port.
DEFAULT_PORT: int = 1883

# Log format for standalone processes.
LOG_FORMAT: str = "%(asctime)s %(name)s %(levelname)s %(message)s"

# NVR HTTP sidecar default port.
NVR_SIDECAR_PORT: int = 8421

# NVR sidecar listen address (all interfaces — server proxies to localhost).
NVR_SIDECAR_BIND: str = ""


# ---------------------------------------------------------------------------
# AdapterSpec — dispatch table entry
# ---------------------------------------------------------------------------

@dataclass
class AdapterSpec:
    """Specification for a process-isolated adapter.

    Attributes:
        config_key:      JSON key in server.json for this adapter's config.
        factory:         Callable that creates the adapter instance.
                         Signature: ``(config, bus, mqtt_client, broker, port) -> adapter``
        needs_bus:       Whether the adapter requires a SignalBus.
        command_handler: Optional callable for adapter-specific commands.
                         Signature: ``(adapter, action, params) -> Optional[dict]``
                         Return ``None`` to fall through to base handler.
        post_start:      Optional callable run after adapter.start().
                         Signature: ``(adapter, config) -> None``
        enabled_check:   Optional callable to verify adapter is enabled.
                         Signature: ``(config) -> bool``
                         Default checks ``config[config_key]["enabled"]``.
    """

    config_key: str
    factory: Callable[..., Any]
    needs_bus: bool = True
    command_handler: Optional[Callable[..., Optional[dict[str, Any]]]] = None
    post_start: Optional[Callable[..., None]] = None
    enabled_check: Optional[Callable[..., bool]] = None


# ---------------------------------------------------------------------------
# AdapterProcess — generic process wrapper
# ---------------------------------------------------------------------------

class AdapterProcess(ProcessAdapterBase):
    """Generic process wrapper for any GlowUp adapter.

    Creates the adapter via the :class:`AdapterSpec` factory, manages
    its lifecycle inside :meth:`run`, and routes commands through the
    spec's command handler.

    Args:
        adapter_id: Unique adapter identifier (e.g., ``"zigbee"``).
        config:     Full server.json config dict.
        spec:       :class:`AdapterSpec` for this adapter type.
        broker:     GlowUp MQTT broker address.
        port:       GlowUp MQTT broker port.
    """

    def __init__(
        self,
        adapter_id: str,
        config: dict[str, Any],
        spec: AdapterSpec,
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_PORT,
    ) -> None:
        """Initialize the adapter process."""
        super().__init__(adapter_id, broker, port)
        self._config: dict[str, Any] = config
        self._spec: AdapterSpec = spec
        self._adapter: Any = None

    # ------------------------------------------------------------------
    # ProcessAdapterBase overrides
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Create the adapter, start it, and block until stopped."""
        bus: Optional[MqttSignalBus] = (
            MqttSignalBus(self) if self._spec.needs_bus else None
        )

        # Factory creates the adapter with lazy-imported dependencies.
        self._adapter = self._spec.factory(
            self._config, bus, self._client,
            self._broker, self._port,
        )
        self._adapter.start()

        logger.info("[%s] Adapter started", self._adapter_id)

        # Post-start hook (e.g. NVR HTTP sidecar).
        if self._spec.post_start is not None:
            self._spec.post_start(self._adapter, self._config)

        # Block until SIGTERM or stop().
        self._stop_event.wait()

        self._adapter.stop()
        logger.info("[%s] Adapter stopped", self._adapter_id)

    def get_status_detail(self) -> dict[str, Any]:
        """Return adapter-specific status for heartbeat.

        Delegates to the underlying adapter's ``get_status()`` method.

        Returns:
            Status dict, or ``{"running": False}`` if not yet started.
        """
        if self._adapter is None:
            return {"running": False}
        return self._adapter.get_status()

    def handle_command(
        self, action: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Route commands to adapter-specific handler, then base class.

        Args:
            action: Command action name.
            params: Command parameters.

        Returns:
            Response dict.
        """
        if self._spec.command_handler is not None:
            result: Optional[dict[str, Any]] = self._spec.command_handler(
                self._adapter, action, params,
            )
            if result is not None:
                return result

        # Fall through to base class (handles restart, shutdown).
        return super().handle_command(action, params)


# ---------------------------------------------------------------------------
# Factory functions — one per adapter, lazy imports
# ---------------------------------------------------------------------------

def _create_zigbee(
    config: dict[str, Any],
    bus: Optional[MqttSignalBus],
    mqtt_client: Any,
    broker: str,
    port: int,
) -> Any:
    """Create a ZigbeeAdapter with Z2M broker from config.

    The Z2M broker may differ from the GlowUp broker.  The adapter
    creates its own MQTT client for Z2M subscription.
    """
    from adapters.zigbee_adapter import ZigbeeAdapter

    z_cfg: dict[str, Any] = config.get("zigbee", {})
    z_broker: str = z_cfg.get("broker", broker)
    z_port: int = z_cfg.get("port", port)

    adapter: Any = ZigbeeAdapter(
        config=z_cfg,
        bus=bus,
        broker=z_broker,
        port=z_port,
    )

    logger.info(
        "[zigbee] Z2M broker=%s:%d, GlowUp broker=%s:%d",
        z_broker, z_port, broker, port,
    )
    return adapter


def _create_vivint(
    config: dict[str, Any],
    bus: Optional[MqttSignalBus],
    mqtt_client: Any,
    broker: str,
    port: int,
) -> Any:
    """Create a VivintAdapter with the GlowUp MQTT client.

    Vivint uses the MQTT client for publishing lock/sensor state to
    ``glowup/vivint/`` topics on the GlowUp broker.
    """
    from contrib.adapters.vivint_adapter import VivintAdapter

    v_cfg: dict[str, Any] = config.get("vivint", {})
    return VivintAdapter(
        config=v_cfg,
        bus=bus,
        mqtt_client=mqtt_client,
    )


def _create_nvr(
    config: dict[str, Any],
    bus: Optional[MqttSignalBus],
    mqtt_client: Any,
    broker: str,
    port: int,
) -> Any:
    """Create an NvrAdapter.

    NVR does not use SignalBus — it caches JPEG snapshots in memory,
    served via the HTTP sidecar started in ``_nvr_post_start()``.

    The doorbell-boost feature needs to POST to the local server's
    ``/api/devices/{ip}/play`` endpoint to trigger phone-style
    overrides on the porch bulbs.  We inject the top-level
    ``auth_token`` and a derived ``server_url`` into the nvr config
    here so the adapter doesn't have to duplicate or re-read them.
    """
    from contrib.adapters.nvr_adapter import NvrAdapter

    nvr_cfg: dict[str, Any] = dict(config.get("nvr", {}))

    # Inject local server URL (from top-level host/port) and the
    # auth token so the doorbell boost can call /play, /stop, /resume.
    server_host: str = str(config.get("host", "localhost"))
    server_port: int = int(config.get("port", 8420))
    nvr_cfg.setdefault(
        "server_url", f"http://{server_host}:{server_port}",
    )
    auth_token: str = str(config.get("auth_token", ""))
    if auth_token:
        nvr_cfg.setdefault("auth_token", auth_token)

    return NvrAdapter(config=nvr_cfg)


def _create_printer(
    config: dict[str, Any],
    bus: Optional[MqttSignalBus],
    mqtt_client: Any,
    broker: str,
    port: int,
) -> Any:
    """Create a PrinterAdapter with the GlowUp MQTT client.

    Printer uses the MQTT client for publishing status to
    ``glowup/printer/`` topics on the GlowUp broker.
    """
    from contrib.adapters.printer_adapter import PrinterAdapter

    p_cfg: dict[str, Any] = config.get("printer", {})
    return PrinterAdapter(
        config=p_cfg,
        bus=bus,
        mqtt_client=mqtt_client,
    )


def _create_matter(
    config: dict[str, Any],
    bus: Optional[MqttSignalBus],
    mqtt_client: Any,
    broker: str,
    port: int,
) -> Any:
    """Create a MatterAdapter with WebSocket URL from config."""
    from adapters.matter_adapter import MatterAdapter

    m_cfg: dict[str, Any] = config.get("matter", {})
    server_url: str = m_cfg.get(
        "server_url", "ws://localhost:5580/ws",
    )
    return MatterAdapter(
        config=m_cfg,
        bus=bus,
        server_url=server_url,
    )


def _create_ble(
    config: dict[str, Any],
    bus: Optional[MqttSignalBus],
    mqtt_client: Any,
    broker: str,
    port: int,
) -> Any:
    """Create a BleAdapter with BLE broker from config.

    The BLE broker may differ from the GlowUp broker (e.g. broker-2
    at 10.0.0.123).  The adapter creates its own MQTT client for
    BLE topic subscription.
    """
    from adapters.ble_adapter import BleAdapter

    ble_cfg: dict[str, Any] = config.get("ble", {})
    ble_broker: str = ble_cfg.get("broker", broker)
    ble_port: int = ble_cfg.get("port", port)

    return BleAdapter(
        bus=bus,
        broker=ble_broker,
        port=ble_port,
        config=ble_cfg,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_zigbee_command(
    adapter: Any, action: str, params: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Handle Zigbee-specific commands.

    Supported:
        - ``"send"``: Publish a command to a Zigbee device.
          Requires ``params["device"]`` and ``params["payload"]``.
    """
    if action != "send":
        return None

    device: Optional[str] = params.get("device")
    payload: dict[str, Any] = params.get("payload", {})
    if not device:
        return {"status": "error", "error": "missing 'device' param"}

    ok: bool = adapter.send_command(device, payload)
    return {"status": "ok"} if ok else {
        "status": "error", "error": "MQTT publish failed",
    }


def _handle_printer_command(
    adapter: Any, action: str, params: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Handle Printer-specific commands.

    Supported:
        - ``"force_poll"``: Trigger an immediate poll cycle.
    """
    if action != "force_poll":
        return None

    status: dict[str, Any] = adapter.force_poll()
    return {"status": "ok", "detail": status}


def _handle_matter_command(
    adapter: Any, action: str, params: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Handle Matter-specific commands.

    Supported:
        - ``"power_on"``:       Turn a device on.
        - ``"power_off"``:      Turn a device off.
        - ``"toggle"``:         Toggle a device.
        - ``"get_power_state"``: Return cached power state.
        - ``"get_devices"``:    Return list of device names.

    All power commands require ``params["device_name"]``.
    """
    if action == "get_devices":
        names: list[str] = adapter.get_device_names()
        return {"status": "ok", "devices": names}

    if action == "get_power_state":
        name: Optional[str] = params.get("device_name")
        if not name:
            return {"status": "error", "error": "missing 'device_name'"}
        state: Optional[bool] = adapter.get_power_state(name)
        return {"status": "ok", "power": state}

    if action in ("power_on", "power_off", "toggle"):
        name = params.get("device_name")
        if not name:
            return {"status": "error", "error": "missing 'device_name'"}
        method: Callable[..., bool] = getattr(adapter, action)
        ok: bool = method(name)
        return {"status": "ok"} if ok else {
            "status": "error", "error": f"{action} failed",
        }

    return None


def _handle_ble_command(
    adapter: Any, action: str, params: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Handle BLE-specific commands.

    Supported:
        - ``"get_status_blob"``: Return the cached health JSON for a sensor.
          Requires ``params["label"]``.
    """
    if action != "get_status_blob":
        return None

    label: Optional[str] = params.get("label")
    if not label:
        return {"status": "error", "error": "missing 'label' param"}

    blob: Optional[dict[str, Any]] = adapter.get_status_blob(label)
    return {"status": "ok", "blob": blob}


# ---------------------------------------------------------------------------
# NVR HTTP sidecar
# ---------------------------------------------------------------------------

def _nvr_post_start(adapter: Any, config: dict[str, Any]) -> None:
    """Start the HTTP sidecar that serves NVR JPEG snapshots.

    MQTT is wrong for binary blobs.  The sidecar provides a simple
    HTTP endpoint:  ``GET /snapshot/{channel_id}`` returns a JPEG.
    The server proxies camera requests to this sidecar.

    Runs as a daemon thread within the NVR process.

    Args:
        adapter: The :class:`NvrAdapter` instance.
        config:  Full server.json config dict.
    """
    nvr_cfg: dict[str, Any] = config.get("nvr", {})
    sidecar_port: int = nvr_cfg.get("sidecar_port", NVR_SIDECAR_PORT)

    # Capture adapter reference for the handler class.
    _adapter: Any = adapter

    class SnapshotHandler(http.server.BaseHTTPRequestHandler):
        """Serve cached JPEG snapshots from the NVR adapter."""

        def do_GET(self) -> None:
            """Handle GET /snapshot/{channel_id}."""
            parts: list[str] = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "snapshot":
                self.send_error(404, "Not found")
                return

            try:
                channel_id: int = int(parts[1])
            except ValueError:
                self.send_error(400, "Channel ID must be integer")
                return

            jpeg_data: Optional[bytes] = _adapter.get_snapshot(channel_id)
            if jpeg_data is None:
                self.send_error(404, "No snapshot for channel")
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg_data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(jpeg_data)

        def log_message(self, format: str, *args: Any) -> None:
            """Suppress default access logging — use structured logger."""
            logger.debug("[nvr-sidecar] %s", format % args)

    httpd: http.server.HTTPServer = http.server.HTTPServer(
        (NVR_SIDECAR_BIND, sidecar_port), SnapshotHandler,
    )
    sidecar_thread: threading.Thread = threading.Thread(
        target=httpd.serve_forever,
        daemon=True,
        name="nvr-sidecar",
    )
    sidecar_thread.start()

    logger.info(
        "[nvr] HTTP sidecar started on port %d", sidecar_port,
    )


# ---------------------------------------------------------------------------
# Enabled checks
# ---------------------------------------------------------------------------

def _enabled_by_key(config: dict[str, Any], key: str) -> bool:
    """Check if adapter is enabled via ``config[key]["enabled"]``."""
    return config.get(key, {}).get("enabled", False)


def _enabled_by_host(config: dict[str, Any], key: str) -> bool:
    """Check if adapter is enabled by presence of ``"host"`` key."""
    return bool(config.get(key, {}).get("host"))


def _ble_enabled(config: dict[str, Any]) -> bool:
    """BLE is enabled if paho-mqtt is available and config exists."""
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

ADAPTERS: dict[str, AdapterSpec] = {
    "zigbee": AdapterSpec(
        config_key="zigbee",
        factory=_create_zigbee,
        command_handler=_handle_zigbee_command,
        enabled_check=lambda c: _enabled_by_key(c, "zigbee"),
    ),
    "vivint": AdapterSpec(
        config_key="vivint",
        factory=_create_vivint,
        command_handler=None,
        enabled_check=lambda c: _enabled_by_key(c, "vivint"),
    ),
    "nvr": AdapterSpec(
        config_key="nvr",
        factory=_create_nvr,
        needs_bus=False,
        post_start=_nvr_post_start,
        enabled_check=lambda c: _enabled_by_host(c, "nvr"),
    ),
    "printer": AdapterSpec(
        config_key="printer",
        factory=_create_printer,
        command_handler=_handle_printer_command,
        enabled_check=lambda c: _enabled_by_host(c, "printer"),
    ),
    "matter": AdapterSpec(
        config_key="matter",
        factory=_create_matter,
        command_handler=_handle_matter_command,
        enabled_check=lambda c: _enabled_by_key(c, "matter"),
    ),
    "ble": AdapterSpec(
        config_key="ble",
        factory=_create_ble,
        command_handler=_handle_ble_command,
        enabled_check=_ble_enabled,
    ),
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse args, load config, and start the requested adapter process."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="GlowUp adapter — standalone process",
    )
    parser.add_argument(
        "--adapter", required=True,
        choices=sorted(ADAPTERS.keys()),
        help="Adapter type to run",
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to server.json",
    )
    parser.add_argument(
        "--broker", default=None,
        help="GlowUp MQTT broker (overrides config)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="GlowUp MQTT port (overrides config)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    args: argparse.Namespace = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=LOG_FORMAT,
    )

    # Load config.
    try:
        with open(args.config) as f:
            config: dict[str, Any] = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load config %s: %s", args.config, exc)
        sys.exit(1)

    # Look up adapter spec.
    spec: AdapterSpec = ADAPTERS[args.adapter]

    # Check if adapter is enabled.
    if spec.enabled_check is not None:
        if not spec.enabled_check(config):
            logger.error(
                "Adapter '%s' is not enabled in config", args.adapter,
            )
            sys.exit(1)

    # Resolve GlowUp MQTT broker from args or config.
    mqtt_cfg: dict[str, Any] = config.get("mqtt", {})
    broker: str = args.broker or mqtt_cfg.get("broker", DEFAULT_BROKER)
    port: int = args.port or mqtt_cfg.get("port", DEFAULT_PORT)

    logger.info(
        "Starting adapter '%s' — GlowUp broker=%s:%d",
        args.adapter, broker, port,
    )

    process: AdapterProcess = AdapterProcess(
        adapter_id=args.adapter,
        config=config,
        spec=spec,
        broker=broker,
        port=port,
    )
    process.start()


if __name__ == "__main__":
    main()
