# Engine and Controller API

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The `Controller` class in `engine.py` is the thread-safe public interface
for controlling the effect engine. It is designed to be driven by the CLI
today and a REST API in the future.

### Controller Methods

```python
from transport import LifxDevice
from engine import Controller

# Create a controller with one or more devices
device = LifxDevice("<device-ip>")
device.query_all()
ctrl = Controller([device], fps=20)
```

**`play(effect_name: str, **params) -> None`**
Start an effect by its registered name. Any keyword arguments override
the effect's default parameters.

```python
ctrl.play("cylon", speed=1.5, width=12, hue=0)
```

**`stop(fade_ms: int = 500) -> None`**
Stop the current effect and fade to black. Pass `fade_ms=0` to skip
the fade.

```python
ctrl.stop(fade_ms=1000)  # 1-second fade out
```

**`update_params(**kwargs) -> None`**
Update parameters on the running effect without restarting it. Unknown
parameter names are silently ignored.

```python
ctrl.update_params(speed=3.0, hue=240)
```

**`get_status() -> dict`**
Returns the current engine state as a JSON-serializable dict:

```python
{
    "running": True,
    "effect": "cylon",
    "params": {"speed": 1.5, "width": 12, "hue": 0.0, ...},
    "fps": 20,
    "devices": [
        {"ip": "<device-ip>", "mac": "aa:bb:cc:dd:ee:ff",
         "label": "My Light", "product": "String Light", "zones": 108}
    ]
}
```

**`list_effects() -> dict`**
Returns all registered effects with parameter metadata:

```python
{
    "cylon": {
        "description": "Larson scanner — a bright eye sweeps back and forth",
        "params": {
            "speed": {"default": 2.0, "min": 0.2, "max": 30.0,
                      "description": "Seconds per full sweep", "type": "float"},
            ...
        }
    },
    ...
}
```

### Emitters

As of v2.0, devices are wrapped in **emitters** — the output stage of the
[SOE pipeline](21-soe-pipeline.md).  The `LifxEmitter` class in
`emitters/lifx.py` wraps a `LifxDevice` and bridges both the SOE lifecycle
and the engine's direct-method interface.

```python
from transport import LifxDevice
from emitters.lifx import LifxEmitter

# Create an emitter from a queried device
device = LifxDevice("10.0.0.62")
device.query_all()
emitter = LifxEmitter.from_device(device)

# Use with the Controller
ctrl = Controller([emitter], fps=20)
ctrl.play("aurora", speed=3.0)
```

The emitter exposes the same properties the engine expects (`zone_count`,
`is_multizone`, `send_zones`, `send_color`) plus the SOE lifecycle hooks
(`on_open`, `on_emit`, `on_close`).  See the
[Emitter Developer Guide](22-emitter-dev-guide.md) for details.

### VirtualMultizoneDevice

The `VirtualMultizoneDevice` class in `emitters/virtual.py` wraps any
combination of LIFX emitters into a single virtual multizone device.
Multizone devices contribute all their physical zones; single bulbs
contribute one zone each.

```python
from transport import LifxDevice
from emitters.lifx import LifxEmitter
from emitters.virtual import VirtualMultizoneDevice

# Connect devices of any type
string_light = LifxDevice("10.0.0.62")  # 108-zone multizone
white_bulb_1 = LifxDevice("10.0.0.25")  # monochrome single
color_bulb_1 = LifxDevice("10.0.0.30")  # color single

for dev in [string_light, white_bulb_1, color_bulb_1]:
    dev.query_all()

# Wrap them as emitters — total zone count = 108 + 1 + 1 = 110
emitters = [LifxEmitter.from_device(d)
            for d in [string_light, white_bulb_1, color_bulb_1]]
vdev = VirtualMultizoneDevice(emitters)
print(vdev.zone_count)  # 110

# Use exactly like a regular emitter
ctrl = Controller([vdev], fps=20)
ctrl.play("cylon", speed=3.0)
```

**How dispatch works:**

The constructor builds a zone map — a list of `(emitter, zone_index)` tuples.
When `set_zones()` is called with the rendered colors:

- **Multizone emitter zones** are accumulated into a per-emitter batch, then
  flushed with a single `send_zones()` call (efficient 2-packet extended
  multizone protocol, same as direct use).
- **Single color bulbs** receive `send_color()` with full HSBK.
- **Monochrome bulbs** receive `send_color()` with BT.709 luma-converted
  brightness (hue and saturation are converted to perceptual brightness).

The class duck-types the emitter interface, so the `Engine`,
`Controller`, and all effects work without modification.

### LifxDevice Key Methods

```python
from transport import LifxDevice, discover_devices

# Discovery
devices = discover_devices(timeout=3.0)

# Direct connection
dev = LifxDevice("<device-ip>")
dev.query_all()          # Populates label, product, group, zone_count

# Properties
dev.label                # "My Light"
dev.product_name         # "String Light"
dev.zone_count           # 108
dev.mac_str              # "aa:bb:cc:dd:ee:ff"
dev.is_multizone         # True for string lights, beams, Z strips
dev.is_polychrome        # True for color devices, False for monochrome

# Zone control (multizone devices)
colors = [(hue, sat, bri, kelvin)] * dev.zone_count
dev.set_zones(colors, duration_ms=0, rapid=True)

# Single color (non-multizone)
dev.set_color(hue, sat, bri, kelvin, duration_ms=0)

# Power
dev.set_power(on=True, duration_ms=1000)

# Cleanup
dev.close()
```
