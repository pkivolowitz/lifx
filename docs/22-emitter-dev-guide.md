# Emitter Developer Guide

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

This guide explains how to create new emitters for the SOE pipeline.
It parallels the [Effect Developer Guide](07-effect-dev-guide.md) —
if you've written an effect, the emitter framework will feel familiar.

## Architecture Overview

```
emitters/
    ├── __init__.py          Emitter ABC, EmitterManager, Param reuse, auto-registry
    ├── lifx.py              LIFX LAN protocol driver (first concrete emitter)
    ├── audio_out.py         Audio tone synthesizer (CoreAudio/PortAudio)
    ├── virtual.py           Virtual multizone composite device
    ├── virtual_grid.py      2D spatial arrangement of matrix and strip devices
    ├── screen.py            ANSI terminal simulator (1D strip)
    └── screen_matrix.py     tkinter simulator (2D matrix)
```

VirtualGridEmitter (`emitters/virtual_grid.py`) -- 2D spatial
arrangement of matrix and strip devices.  Routes pixel frames to
member devices by cell position.  Supports sparse grids (not all
cells filled), matrix devices (LIFX Tiles), and strip scanline
mapping.

**Key design principle:** Emitters are output endpoints.  They receive
abstract frames from an operator and express them in a specific medium.
They never produce data, decide *what* to emit, or reach back into the
pipeline.  Given a frame and metadata, they output it.

## Creating a New Emitter

1. Create a new file in `emitters/` (e.g., `emitters/csv.py`).
2. Subclass `Emitter` and set `emitter_type` and `description`.
3. Declare parameters as class-level `Param` instances.
4. Implement `on_emit()` and `capabilities()`.
5. That's it — the emitter auto-registers and is available by type.

No imports in `__init__.py` are needed.  The framework auto-discovers
all `.py` files in the `emitters/` directory via `pkgutil.iter_modules`
and imports them at startup.  The `EmitterMeta` metaclass automatically
registers any `Emitter` subclass that defines a non-`None` `emitter_type`.

## The Emitter Base Class

```python
from emitters import Emitter, EmitterCapabilities
from effects import Param

class MyEmitter(Emitter):
    emitter_type: str = "mytype"          # Registry key (must be unique)
    description: str = "One-liner"        # Shown in status/API

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Process one frame of output.

        Args:
            frame:    Data from the operator (type depends on pipeline).
            metadata: Per-frame context dict.

        Returns:
            True on success, False on transient failure.
        """
        ...

    def capabilities(self) -> EmitterCapabilities:
        """Declare what this emitter accepts."""
        return EmitterCapabilities(
            accepted_frame_types=["snapshot"],
            max_rate_hz=10.0,
        )
```

## Lifecycle Hooks

Every emitter follows a five-stage lifecycle managed by the
EmitterManager.  All hooks except `on_emit()` and `capabilities()`
have default no-op implementations — override only what you need.

| Method | When called | Override for |
|--------|------------|-------------|
| `__init__(name, config)` | Construction | Param application (handled by base class) |
| `on_configure(config)` | After construction | Device discovery, connection setup, file path resolution |
| `on_open()` | Pipeline start | Acquire resources: open sockets, files, GPIO pins |
| `on_emit(frame, metadata)` | Every frame | **Core method.** Process one frame of output. |
| `on_flush()` | Periodically + before close | Flush buffered output (databases, file buffers) |
| `on_close()` | Pipeline stop | Release all resources |

### on_configure

Called once after construction with the **full server configuration**.
Use this for deferred initialization that depends on external state.

```python
def on_configure(self, config: dict[str, Any]) -> None:
    self._output_dir = config.get("data_dir", "/tmp")
    self._path = os.path.join(
        self._output_dir,
        self._config.get("filename", "output.csv"),
    )
```

The emitter's own config is available as `self._config` (set during
`__init__`).  The `config` argument to `on_configure` is the full
server config — use it for cross-cutting concerns like data directories
or shared credentials.

### on_emit

The core method.  The frame type depends on the pipeline topology:

| Frame type | Example emitter | Description |
|-----------|----------------|-------------|
| `list[HSBK]` | LIFX, DMX, LED tape | Color strip — one HSBK per zone |
| `HSBK` | Single bulb | Single color tuple |
| `bool` | Relay, valve, solenoid | Binary on/off |
| `dict[str, float]` | CSV, database, webhook | Signal snapshot from the bus |
| `bytes` | Speaker, display | Raw media buffer |

Return `True` on success, `False` on transient failure.  The manager
tracks consecutive failures — after 10 in a row, the emitter is
auto-disabled.  Do not raise exceptions for expected failures; return
`False` instead.  Unexpected exceptions are caught by the manager,
logged, and counted as failures.

### Metadata

The `metadata` dict always contains:

| Key | Type | Description |
|-----|------|-------------|
| `"t"` | `float` | Seconds since pipeline start |
| `"dt"` | `float` | Seconds since last emit |
| `"frame_number"` | `int` | Monotonic frame counter |

Event-driven emitters also receive:

| Key | Type | Description |
|-----|------|-------------|
| `"trigger_signal"` | `str` | Signal name that fired |
| `"trigger_value"` | `float` | Signal value at trigger time |
| `"signal_bus"` | `SignalBus` | Bus reference for direct reads |

### on_flush

Called periodically (every 5 seconds by default) and always before
`on_close()`.  Override for emitters that batch writes:

```python
def on_flush(self) -> None:
    if self._buffer:
        self._writer.writerows(self._buffer)
        self._file.flush()
        self._buffer.clear()
```

## The Param System

Emitters reuse the same `Param` system as effects — imported from
`effects/__init__.py`.  Parameters declared as class attributes become
instance attributes with automatic validation and clamping.

```python
from effects import Param

class CsvEmitter(Emitter):
    emitter_type: str = "csv"
    description: str = "Write signal snapshots to CSV"

    flush_interval = Param(5.0, min=0.1, max=300.0,
                           description="Seconds between disk flushes")
    max_rows = Param(100000, min=1000, max=10000000,
                     description="Max rows before rotation")
    delimiter = Param(",", description="CSV delimiter character")
```

Parameters serve three purposes:

1. **Config** — overridden from `server.json` emitter config.
2. **API** — exposed as metadata for runtime parameter updates.
3. **Runtime** — accessed as `self.flush_interval` in `on_emit()`.

See the [Effect Developer Guide](07-effect-dev-guide.md#the-param-system)
for full Param documentation — the semantics are identical.

## EmitterCapabilities

Declare what your emitter accepts so the pipeline can validate
connections at construction time (not per-frame):

```python
def capabilities(self) -> EmitterCapabilities:
    return EmitterCapabilities(
        accepted_frame_types=["snapshot"],   # What frame types you handle
        max_rate_hz=10.0,                    # Don't call faster than this
        variable_topology=False,             # Zone count fixed after configure?
        zones=0,                             # 1D topology size
        width=0,                             # 2D topology width
        height=0,                            # 2D topology height
    )
```

**`accepted_frame_types`** — string identifiers that describe the
frame format.  Standard types:

| Type | Meaning |
|------|---------|
| `"strip"` | `list[HSBK]` — multizone color strip |
| `"single"` | `HSBK` tuple — single color |
| `"scalar"` | `dict` with named float values (e.g., frequency + amplitude) |
| `"snapshot"` | `dict[str, float]` — signal bus snapshot |
| `"binary"` | `bool` — on/off |
| `"raw"` | `bytes` — raw media buffer |

You may define custom types for specialized pipelines.

**`max_rate_hz`** — the highest meaningful update rate.  The
EmitterManager will never call `on_emit()` faster than this,
regardless of the engine frame rate.

## Auto-Registration

The `EmitterMeta` metaclass registers your emitter at class-creation
time.  Any subclass of `Emitter` with a non-`None` `emitter_type`
is added to the global registry:

```python
from emitters import get_registry, get_emitter_types, create_emitter

# See what's registered
print(get_emitter_types())       # ['lifx', 'csv', 'webhook', ...]

# Create by type name (how EmitterManager does it)
emitter = create_emitter("csv", "audio_log", {"path": "/data/log.csv"})
```

Emitters that should NOT be registered (internal, abstract, or
simulator-only) should leave `emitter_type = None`.

## Configuration

Each emitter is configured in `server.json` under the `"emitters"` key.
The EmitterManager consumes scheduling keys and passes the rest to
the emitter constructor:

```json
{
    "emitters": {
        "audio_log": {
            "type": "csv",
            "timing": "periodic",
            "rate_hz": 5,
            "signals": ["foyer:audio:*"],
            "path": "/data/audio_{timestamp}.csv",
            "max_rows": 50000
        }
    }
}
```

**Manager keys** (consumed by the slot, not passed to the emitter):

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `type` | `str` | — | Registry type (required) |
| `timing` | `str` | `"continuous"` | `"continuous"`, `"periodic"`, or `"event"` |
| `rate_hz` | `float` | `0.0` | Periodic dispatch rate |
| `signals` | `list[str]` | `[]` | Signal glob patterns for bus snapshots |
| `trigger_signal` | `str` | — | Event: bus signal to watch |
| `trigger_threshold` | `float` | `0.5` | Event: fire when signal crosses this |
| `trigger_edge` | `str` | `"rising"` | `"rising"`, `"falling"`, or `"any"` |
| `cooldown_seconds` | `float` | `0.0` | Event: minimum seconds between fires |

Everything else in the config dict is passed to the emitter's
`__init__` as its `config` parameter.

## Failure Tracking

The EmitterManager tracks consecutive failures per emitter:

- `on_emit()` returns `False` → failure counter increments.
- `on_emit()` returns `True` → failure counter resets to 0.
- `on_emit()` raises an exception → caught, logged, counted as failure.
- After **10 consecutive failures** → emitter auto-disabled, logged as error.

Auto-disabled emitters can be re-enabled via `EmitterManager.enable(name)`
(resets the failure counter) without restarting the server.

## Complete Example

Here is a complete emitter that writes signal snapshots to CSV:

```python
"""CSV emitter — write signal snapshots to disk.

Each on_emit() call appends one row per signal.  Rows are buffered
in memory and flushed periodically by the EmitterManager.
"""

__version__ = "1.0"

import csv
import logging
import os
import time
from typing import Any, Optional, TextIO

from effects import Param
from emitters import Emitter, EmitterCapabilities

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default CSV dialect settings.
_DEFAULT_DELIMITER: str = ","

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.emitters.csv")


class CsvEmitter(Emitter):
    """Write signal bus snapshots to a CSV file."""

    emitter_type: str = "csv"
    description: str = "Signal snapshot logger to CSV"

    path = Param("/tmp/glowup_signals.csv",
                 description="Output file path ({timestamp} is expanded)")
    delimiter = Param(_DEFAULT_DELIMITER,
                      description="CSV field delimiter")

    def on_configure(self, config: dict[str, Any]) -> None:
        """Expand path templates and prepare the output directory.

        Args:
            config: Full server configuration dict.
        """
        # Expand {timestamp} in path.
        raw_path: str = str(self.path)
        if "{timestamp}" in raw_path:
            ts: str = time.strftime("%Y%m%d_%H%M%S")
            raw_path = raw_path.replace("{timestamp}", ts)
        self._resolved_path: str = raw_path

        # Ensure parent directory exists.
        parent: str = os.path.dirname(self._resolved_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._file: Optional[TextIO] = None
        self._writer: Optional[csv.writer] = None
        self._buffer: list[list[Any]] = []
        self._header_written: bool = False

    def on_open(self) -> None:
        """Open the CSV file for writing."""
        self._file = open(self._resolved_path, "a", newline="")
        self._writer = csv.writer(self._file, delimiter=self.delimiter)
        logger.info("CSV emitter opened: %s", self._resolved_path)

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Append one row per signal in the snapshot.

        Args:
            frame:    dict[str, float] signal snapshot from the bus.
            metadata: Per-frame context dict.

        Returns:
            True on success.
        """
        if not isinstance(frame, dict) or self._writer is None:
            return False

        t: float = metadata.get("t", 0.0)

        # Write header on first emit.
        if not self._header_written:
            self._writer.writerow(["timestamp", "signal", "value"])
            self._header_written = True

        for signal_name, value in sorted(frame.items()):
            self._buffer.append([t, signal_name, value])

        return True

    def on_flush(self) -> None:
        """Write buffered rows to disk."""
        if self._buffer and self._writer is not None:
            self._writer.writerows(self._buffer)
            if self._file is not None:
                self._file.flush()
            self._buffer.clear()

    def on_close(self) -> None:
        """Flush and close the CSV file."""
        self.on_flush()
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None
        logger.info("CSV emitter closed: %s",
                    getattr(self, "_resolved_path", "?"))

    def capabilities(self) -> EmitterCapabilities:
        """Declare CSV emitter capabilities.

        Returns:
            Capabilities accepting signal snapshots at up to 60 Hz.
        """
        return EmitterCapabilities(
            accepted_frame_types=["snapshot"],
            max_rate_hz=60.0,
        )
```

Save this as `emitters/csv.py` and it will automatically register as
type `"csv"`.  Configure it in `server.json`:

```json
{
    "emitters": {
        "audio_log": {
            "type": "csv",
            "timing": "periodic",
            "rate_hz": 5,
            "signals": ["foyer:audio:*"],
            "path": "/data/audio_{timestamp}.csv"
        }
    }
}
```

## LifxEmitter — The Reference Implementation

The `LifxEmitter` (`emitters/lifx.py`) is the first concrete SOE emitter
and serves as the reference for the dual-interface pattern.  It supports
two creation paths:

**Config-based** (via EmitterManager):
```python
emitter = create_emitter("lifx", "porch", {"ip": "192.0.2.62"})
emitter.on_configure(full_server_config)
```

**Programmatic** (via factory classmethod):
```python
from emitters.lifx import LifxEmitter
from transport import LifxDevice

device = LifxDevice("192.0.2.62")
device.query_all()
emitter = LifxEmitter.from_device(device)
```

The Engine currently calls the legacy methods (`send_zones`,
`send_color`) directly.  The EmitterManager calls the SOE lifecycle
(`on_open`, `on_emit`, `on_close`).  Both paths work simultaneously
— the LifxEmitter bridges both interfaces to the same underlying
`LifxDevice` transport.

## AudioOutEmitter — Remote Audio Emitter

The `AudioOutEmitter` (`emitters/audio_out.py`) is the first remote
emitter — it runs on a Mac (or any machine with audio output) and
receives frames via MQTT from the pipeline.  It demonstrates the
distributed emitter pattern: any node with a speaker can be an audio
output endpoint.

### Frame Format

Accepts `scalar` frames as a dict:

```python
{"frequency": 440.0, "amplitude": 0.8}
```

Both keys are optional — partial updates leave the other value
unchanged.  Frequency is clamped to [20, 20000] Hz; amplitude to
[0.0, 1.0].

### Audio Synthesis

The emitter generates a continuous multi-harmonic waveform with:

- **4 harmonics** — fundamental + 2nd (octave) + 3rd (fifth) + 4th
  (warmth), producing a Theremin-like timbre
- **Portamento** — exponential pitch glide (default 50 ms time
  constant) for smooth note transitions
- **Vibrato** — LFO modulates both pitch (±1.5%) and amplitude
  (±8%) at 5.5 Hz, producing the characteristic Theremin "sound
  of the ether"

### Parameters

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `master_volume` | 0.3 | 0.0–1.0 | Output volume multiplier |
| `portamento` | 0.05 | 0.001–2.0 | Pitch glide time constant (seconds) |
| `vibrato_rate` | 5.5 | 0.0–20.0 | Vibrato LFO rate in Hz (0 = off) |
| `vibrato_depth` | 0.015 | 0.0–0.1 | Pitch modulation depth (fraction) |
| `vibrato_amp_depth` | 0.08 | 0.0–0.5 | Amplitude modulation depth |

### Mute Support

The emitter has a `toggle_mute()` method that silences output without
tearing down the audio stream.  The MQTT test harness
(`distributed/test_audio_emitter.py`) maps the `h` key to this toggle.

### Standalone Test

```bash
# Tone sweep (no MQTT needed)
~/venv/bin/python3 -m emitters.audio_out

# MQTT integration (requires Pi + theremin effect)
cd ~/glowup && ~/venv/bin/python3 -m distributed.test_audio_emitter
```

### Agent Configuration

For use with the distributed worker agent, declare the emitter in
the agent's JSON config:

```json
{
    "node_id": "bed",
    "mqtt_broker": "192.0.2.48",
    "roles": ["emitter"],
    "emitters": [
        {"type": "audio_out", "id": "bed:speaker", "topology": "scalar"}
    ]
}
```

## Emitter Checklist

When creating a new emitter, verify:

- [ ] File is in `emitters/` directory
- [ ] `emitter_type` is set (unique, non-`None`)
- [ ] `description` is set (one-liner)
- [ ] `on_emit()` implemented, returns `bool`
- [ ] `capabilities()` implemented
- [ ] `__version__` string in the module
- [ ] PEP 257 docstrings on all public classes and methods
- [ ] Type hints on all function signatures
- [ ] Constants section (no magic numbers)
- [ ] `py_compile` passes clean
- [ ] Tested: construct, configure, open, emit, flush, close
