# Adapter Base Classes

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp integrates with BLE sensors, Zigbee coordinators, network
printers, Vivint security panels, and NVR cameras.  Each integration
is an **adapter** — a small class that bridges an external data source
to the platform's `SignalBus`.  Before the adapter base classes
existed, every adapter duplicated the same init/start/stop lifecycle,
thread management, reconnect logic, and error handling.  Six adapters
meant six copies of the same boilerplate.

The `adapter_base` module extracts that boilerplate into three
transport-specific base classes.  Each adapter now inherits the
lifecycle it needs, implements one or two abstract methods, and gets
thread management, reconnect, and cleanup for free.

---

## Class Hierarchy

```
AdapterBase (ABC)
    |
    +-- MqttAdapterBase         MQTT subscriber lifecycle (paho)
    |
    +-- PollingAdapterBase      Synchronous polling on a daemon thread
    |
    +-- AsyncPollingAdapterBase Asyncio event loop + reconnect with backoff
```

All three transport bases inherit from `AdapterBase`, which provides
the `_running` flag and `running` read-only property.

### Which Adapter Uses Which Base

| Concrete Adapter | Base Class | Transport |
|------------------|------------|-----------|
| `PrinterAdapter` | `PollingAdapterBase` | SNMP/HTTP polling of network printers |
| `VivintAdapter` | `AsyncPollingAdapterBase` | Asyncio websocket to Vivint cloud |
| `NvrAdapter` | `AsyncPollingAdapterBase` | Asyncio polling of NVR camera feeds |

---

## `AdapterBase`

The abstract root class.  Every adapter in GlowUp inherits from it
(through one of the three transport bases).

```python
from adapter_base import AdapterBase

class AdapterBase(ABC):
    def __init__(self) -> None:
        self._running: bool = False

    @property
    def running(self) -> bool: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...
```

| Member | Type | Description |
|--------|------|-------------|
| `_running` | `bool` | Internal flag; `True` between `start()` and `stop()` |
| `running` | property | Read-only access to `_running` |
| `start()` | abstract | Begin processing (connect, subscribe, spawn thread) |
| `stop()` | abstract | Release resources (disconnect, join thread) |

---

## `MqttAdapterBase`

For adapters that subscribe to an MQTT topic tree and process incoming
messages.  Handles paho client creation (compatible with both paho v1
and v2), `connect_async`, the network loop, `on_connect` subscription,
and clean disconnect on stop.

### Constructor Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `broker` | `str` | MQTT broker hostname or IP |
| `port` | `int` | MQTT broker port |
| `subscribe_prefix` | `str` | Topic prefix; the base subscribes to `{prefix}/#` |
| `client_id_prefix` | `str` | Prefix for the MQTT client ID (timestamp appended for uniqueness) |

### Lifecycle

- **`start()`** — Creates a paho MQTT client, wires `on_connect` and
  `on_message` callbacks, calls `connect_async`, and starts the paho
  network loop.  If `paho-mqtt` is not installed, logs a warning and
  returns (guarded import).

- **`stop()`** — Sets `_running = False`, stops the paho network loop,
  and disconnects.

### Abstract Method

```python
@abstractmethod
def _handle_message(self, topic: str, payload: bytes) -> None:
    """Process an incoming MQTT message.

    Called from paho's network thread -- must be thread-safe.
    """
```

This is the only method a subclass must implement.  The base catches
exceptions from `_handle_message` to prevent crashing paho's internal
network thread.

### Example: Minimal MQTT Adapter

```python
from adapter_base import MqttAdapterBase

class WeatherAdapter(MqttAdapterBase):
    """Subscribe to weather station MQTT topics."""

    def __init__(self, bus, broker="localhost"):
        super().__init__(
            broker=broker,
            port=1883,
            subscribe_prefix="glowup/weather",
            client_id_prefix="weather-adapter",
        )
        self._bus = bus

    def _handle_message(self, topic, payload):
        # topic: "glowup/weather/outdoor/temperature"
        parts = topic.split("/")
        if len(parts) != 4:
            return
        label = parts[2]
        reading = parts[3]
        value = float(payload.decode())
        self._bus.write(f"{label}:{reading}", value)
```

### Paho Version Compatibility

The base detects paho v2 at import time via
`hasattr(mqtt, "CallbackAPIVersion")`.  When v2 is present, it passes
`CallbackAPIVersion.VERSION2` to the `Client` constructor.  The
`_on_connect` callback accepts an optional `properties` parameter
to handle both v1 (4 args) and v2 (5 args) signatures.

---

## `PollingAdapterBase`

For adapters that periodically poll an external resource on a
synchronous daemon thread.

### Constructor Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `poll_interval` | `float` | Seconds between poll cycles |
| `thread_name` | `str` | Name for the daemon thread (appears in logs and `threading.enumerate()`) |

### Lifecycle

- **`start()`** — Calls `_check_prerequisites()`.  If it returns
  `True`, sets `_running`, spawns a daemon thread, and calls
  `_on_started()`.

- **`stop()`** — Sets `_running = False` and joins the thread with a
  generous timeout (defined by `THREAD_JOIN_TIMEOUT`, 10 seconds).

### The Poll Loop

The first poll runs immediately on thread start.  Subsequent polls
wait `poll_interval` seconds.  Sleep is broken into small chunks
(defined by `SLEEP_CHUNK`, 5 seconds) so that `stop()` is responsive
-- the worst-case latency between calling `stop()` and the thread
noticing is one `SLEEP_CHUNK`.

```python
def _poll_loop(self) -> None:
    self._do_poll()          # immediate first poll
    while self._running:
        # interruptible sleep
        remaining = self._poll_interval
        while remaining > 0 and self._running:
            chunk = min(remaining, SLEEP_CHUNK)
            time.sleep(chunk)
            remaining -= chunk
        if self._running:
            self._do_poll()
```

### Abstract Method

```python
@abstractmethod
def _do_poll(self) -> None:
    """Execute a single poll cycle.

    Called on the polling thread.  Exceptions are NOT caught by
    the base; subclasses should handle their own errors to keep
    the loop running.
    """
```

### Optional Override: `_check_prerequisites()`

```python
def _check_prerequisites(self) -> bool:
    """Return False to prevent startup.  Default: True."""
```

Override this to validate configuration or dependencies before the
thread starts.  If it returns `False`, the adapter stays stopped and
the thread is never created.

### Example: Minimal Polling Adapter

```python
from adapter_base import PollingAdapterBase

class PrinterAdapter(PollingAdapterBase):
    """Poll a network printer for status."""

    def __init__(self, bus, printer_ip, interval=60.0):
        super().__init__(
            poll_interval=interval,
            thread_name="printer-poller",
        )
        self._bus = bus
        self._printer_ip = printer_ip

    def _check_prerequisites(self):
        if not self._printer_ip:
            logger.warning("No printer IP configured")
            return False
        return True

    def _do_poll(self):
        try:
            status = fetch_printer_status(self._printer_ip)
            self._bus.write("printer:toner", status["toner_pct"])
        except Exception as exc:
            logger.debug("Printer poll failed: %s", exc)
```

---

## `AsyncPollingAdapterBase`

For adapters that need an asyncio event loop — typically because the
external service uses websockets or an async client library.  Runs a
background daemon thread hosting its own event loop.

### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `thread_name` | `str` | (required) | Name for the daemon thread |
| `reconnect_delay` | `float` | `30.0` | Initial delay before retrying after a connection failure |
| `max_reconnect_delay` | `float` | `300.0` | Maximum backoff cap (5 minutes) |

### Lifecycle

- **`start()`** — Calls `_check_prerequisites()`.  If it returns
  `True`, sets `_running`, spawns a daemon thread that creates a new
  asyncio event loop, and calls `_on_started()`.

- **`stop()`** — Sets `_running = False`, schedules `_disconnect()`
  as a coroutine on the event loop, and joins the thread.

### The Main Loop

The event loop runs `_main_loop()`, which calls `_connect()`, then
`_run_cycle()`.  If either raises, the loop waits with exponential
backoff before retrying:

```python
async def _main_loop(self) -> None:
    delay = self._initial_reconnect_delay
    while self._running:
        try:
            await self._connect()
            delay = self._initial_reconnect_delay  # reset on success
            await self._run_cycle()
        except Exception as exc:
            if not self._running:
                break
            logger.error("%s: error: %s -- retrying in %.0fs",
                         self._thread_name, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, self._max_reconnect_delay)
```

Backoff doubles on each failure (30s, 60s, 120s, 240s, 300s, 300s,
...) and resets to the initial value after a successful `_connect()`.

### Abstract Methods

```python
@abstractmethod
async def _connect(self) -> None:
    """Connect to the external service."""

@abstractmethod
async def _disconnect(self) -> None:
    """Disconnect from the external service."""

@abstractmethod
async def _run_cycle(self) -> None:
    """Main work loop after connection.

    Should loop internally (checking self._running) until the
    connection drops or the adapter is stopped.
    """
```

### Optional Override: `_check_prerequisites()`

Same semantics as `PollingAdapterBase` — return `False` to prevent
startup.

### Example: Minimal Async Adapter

```python
from adapter_base import AsyncPollingAdapterBase

class CameraAdapter(AsyncPollingAdapterBase):
    """Stream events from a camera API."""

    def __init__(self, bus, api_url):
        super().__init__(
            thread_name="camera-adapter",
            reconnect_delay=10.0,
            max_reconnect_delay=120.0,
        )
        self._bus = bus
        self._api_url = api_url
        self._session = None

    async def _connect(self):
        import aiohttp
        self._session = aiohttp.ClientSession()

    async def _disconnect(self):
        if self._session:
            await self._session.close()

    async def _run_cycle(self):
        while self._running:
            async with self._session.get(self._api_url) as resp:
                data = await resp.json()
                self._bus.write("camera:motion", data["motion"])
            await asyncio.sleep(5)
```

---

## Hook Methods

All three transport bases provide hook methods that subclasses can
override for custom logging or initialization:

| Hook | Called When | Default Behavior |
|------|------------|------------------|
| `_on_started()` | After start completes successfully | Logs at INFO level |
| `_on_stopped()` | After stop completes | Logs at INFO level |

Override to add adapter-specific context:

```python
def _on_started(self):
    logger.info(
        "BLE adapter started -- broker %s:%d, subscribing to %s/#",
        self._broker, self._port, self._subscribe_prefix,
    )
```

---

## Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `THREAD_JOIN_TIMEOUT` | `10.0` | Seconds to wait when joining a thread during `stop()` |
| `SLEEP_CHUNK` | `5.0` | Maximum sleep increment for interruptible polling; smaller = more responsive stop, more CPU |

---

## Guarded Imports

The `paho-mqtt` dependency is optional.  The base module uses a
guarded import:

```python
try:
    import paho.mqtt.client as mqtt
    _HAS_PAHO: bool = True
    _PAHO_V2: bool = hasattr(mqtt, "CallbackAPIVersion")
except ImportError:
    _HAS_PAHO = False
    _PAHO_V2 = False
    mqtt = None
```

If `paho-mqtt` is not installed, `MqttAdapterBase.start()` logs a
warning and returns without starting.  This follows the project rule
that everything above core is optional — a Docker user with no
`paho-mqtt` will never see an `ImportError`.

---

## Writing a New Adapter

To add a new external data source to GlowUp:

- Decide the transport pattern: MQTT subscription, synchronous
  polling, or async event loop.

- Subclass the matching base.

- Implement the required abstract method(s):
  - `MqttAdapterBase` — `_handle_message(topic, payload)`
  - `PollingAdapterBase` — `_do_poll()`
  - `AsyncPollingAdapterBase` — `_connect()`, `_disconnect()`,
    `_run_cycle()`

- Optionally override `_check_prerequisites()` to gate startup on
  config or dependencies.

- Optionally override `_on_started()` / `_on_stopped()` for custom
  logging.

- Use a guarded import for any optional dependency your adapter
  needs.

- Register the adapter in `server.py` with the same conditional
  pattern used by the existing adapters.

The base handles thread creation, lifecycle flags, reconnect logic,
and clean shutdown.  Your adapter code contains only domain logic.

---

## See Also

- [BLE Sensor Integration](28-ble-sensors.md) — Concrete example
  of `MqttAdapterBase` in production
- [SOE Pipeline](21-soe-pipeline.md) — How adapter signals flow
  into operators and emitters
- [MQTT Integration](19-mqtt.md) — The MQTT broker and topic
  conventions
