"""Animation engine for LIFX effects.

The :class:`Engine` runs in a background thread, rendering the current effect
at a target frame rate and pushing frames to one or more devices.

The :class:`Controller` is the public interface -- it wraps the engine and
provides methods that are safe to call from any thread: CLI, REST API,
scheduler, etc.

Typical usage::

    from transport import LifxDevice
    from engine import Controller

    device = LifxDevice("<device-ip>")
    device.query_all()

    ctrl = Controller([device])
    ctrl.play("cylon", speed=1.5, width=12)
    # ... later ...
    ctrl.update_params(speed=3.0, hue=240)
    # ... later ...
    ctrl.stop()
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

__version__ = "1.3"

import threading
import time
from typing import Any, Optional

from effects import Effect, create_effect, get_registry, KELVIN_DEFAULT, hsbk_to_luminance
from transport import LifxDevice

# ---------------------------------------------------------------------------
# Named constants — no magic numbers
# ---------------------------------------------------------------------------

# Default rendering frame rate in frames per second.
DEFAULT_FPS: int = 20

# How long to wait (seconds) for the render thread to finish on stop().
THREAD_JOIN_TIMEOUT: float = 5.0

# Default transition time (ms) for fade-to-black when stopping an effect.
DEFAULT_FADE_MS: int = 500

# Minimum allowed fade duration in milliseconds (0 disables fade).
MIN_FADE_MS: int = 0

# Sentinel zone index for single-bulb devices in VirtualMultizoneDevice.
# Distinguishes single bulbs (set_color) from multizone zones (batched set_zones).
SINGLE_BULB_ZONE_SENTINEL: int = -1


class VirtualMultizoneDevice:
    """Wrap N devices as a virtual multizone device.

    Multizone devices (string lights, beams) contribute all their zones.
    Single-bulb devices contribute one zone each.  The total virtual zone
    count is the sum across all devices.

    For example, a group containing a 108-zone string light and 4 single
    bulbs becomes a 112-zone virtual device.  Effects render all 112 zones
    in one ``render()`` call, and :meth:`set_zones` routes each virtual
    zone's color back to the correct physical device — batching multizone
    updates into a single ``set_zones()`` call per device and dispatching
    single-bulb colors via ``set_color()``.

    Monochrome devices automatically receive BT.709 luma-converted brightness.

    The class implements the same interface that :class:`Engine` expects from
    a :class:`LifxDevice`, so no engine changes are needed.
    """

    def __init__(self, devices: list[LifxDevice]) -> None:
        """Initialize with a list of connected, queried devices.

        Builds a zone map that records which physical device and zone index
        each virtual zone corresponds to.  Multizone devices expand to
        their full zone count; single-bulb devices occupy one zone.

        Args:
            devices: :class:`LifxDevice` instances, each already connected
                     and queried via :meth:`LifxDevice.query_all`.  The list
                     order determines the zone assignment.

        Raises:
            ValueError: If *devices* is empty.
        """
        if not devices:
            raise ValueError("VirtualMultizoneDevice requires at least one device.")

        self._devices: list[LifxDevice] = list(devices)
        self.is_multizone: bool = True

        # Build the zone map: list of (device, zone_index) tuples.
        # For multizone devices, zone_index is the physical zone number.
        # For single-bulb devices, zone_index is -1 (sentinel).
        self._zone_map: list[tuple[LifxDevice, int]] = []
        for dev in self._devices:
            zones: int = dev.zone_count if dev.zone_count else 1
            if dev.is_multizone:
                # Multizone device: each physical zone becomes a virtual zone.
                for z in range(zones):
                    self._zone_map.append((dev, z))
            else:
                # Single bulb: one virtual zone, sentinel zone_index.
                self._zone_map.append((dev, SINGLE_BULB_ZONE_SENTINEL))

        self.zone_count: int = len(self._zone_map)

        # Synthesize display properties for status reporting.
        self.ip: str = f"group({len(devices)} devices)"
        self.label: str = "Virtual group"
        self.product_name: str = f"{self.zone_count}-zone virtual multizone"
        self.mac_str: str = "virtual"
        self.product: int = 0  # non-None so engine query checks pass
        self.group: str = ""

    @property
    def is_polychrome(self) -> bool:
        """Always True — not reached since is_multizone is True."""
        return True

    def set_zones(
        self,
        colors: list,
        duration_ms: int = 0,
        rapid: bool = True,
    ) -> None:
        """Route each virtual zone's color to the correct physical device.

        Multizone devices receive a single batched ``set_zones()`` call.
        Single-bulb color devices receive ``set_color()`` with full HSBK.
        Monochrome single bulbs receive BT.709 luma-converted brightness.

        Args:
            colors:      List of HSBK tuples (one per virtual zone).
            duration_ms: Transition time in milliseconds.
            rapid:       Passed through to multizone ``set_zones()`` calls.
        """
        # Collect colors destined for each multizone device so we can
        # batch them into one set_zones() call per device.
        multizone_batches: dict[int, list] = {}

        for vz, (dev, zone_idx) in enumerate(self._zone_map):
            if vz >= len(colors):
                break

            if zone_idx == SINGLE_BULB_ZONE_SENTINEL:
                # Single bulb — dispatch immediately.
                h, s, b, k = colors[vz]
                if dev.is_polychrome is False:
                    dev.set_color(*hsbk_to_luminance(h, s, b, k),
                                  duration_ms=duration_ms)
                else:
                    dev.set_color(h, s, b, k, duration_ms=duration_ms)
            else:
                # Multizone device — accumulate colors for batching.
                dev_id: int = id(dev)
                if dev_id not in multizone_batches:
                    # Pre-allocate the full zone list for this device.
                    multizone_batches[dev_id] = {
                        "dev": dev,
                        "colors": [None] * dev.zone_count,
                    }
                multizone_batches[dev_id]["colors"][zone_idx] = colors[vz]

        # Flush batched multizone updates.
        for batch in multizone_batches.values():
            dev = batch["dev"]
            batch_colors: list = batch["colors"]
            # Fill any gaps (shouldn't happen, but be safe).
            for i in range(len(batch_colors)):
                if batch_colors[i] is None:
                    batch_colors[i] = (0, 0, 0, KELVIN_DEFAULT)
            dev.set_zones(batch_colors, duration_ms=duration_ms, rapid=rapid)

    def set_color(
        self,
        hue: int,
        sat: int,
        bri: int,
        kelvin: int,
        duration_ms: int = 0,
    ) -> None:
        """Set all wrapped devices to the same color.

        Used by the engine's fade-to-black on stop.

        Args:
            hue:         Hue (0--65535).
            sat:         Saturation (0--65535).
            bri:         Brightness (0--65535).
            kelvin:      Color temperature (1500--9000).
            duration_ms: Transition time in milliseconds.
        """
        for dev in self._devices:
            dev.set_color(hue, sat, bri, kelvin, duration_ms=duration_ms)

    def set_power(self, on: bool, duration_ms: int = 0) -> None:
        """Turn all wrapped devices on or off.

        Args:
            on:          ``True`` to turn on, ``False`` to turn off.
            duration_ms: Transition duration in milliseconds.
        """
        for dev in self._devices:
            dev.set_power(on=on, duration_ms=duration_ms)

    def close(self) -> None:
        """Close all wrapped device sockets."""
        for dev in self._devices:
            dev.close()

    def get_device_list(self) -> list[LifxDevice]:
        """Return the list of wrapped devices (for status reporting).

        Returns:
            The underlying :class:`LifxDevice` list.
        """
        return list(self._devices)


class Engine:
    """Low-level animation engine that runs in a background thread.

    Renders the active :class:`Effect` at a target frame rate and pushes
    each frame to every attached device.

    Attributes:
        devices: List of :class:`LifxDevice` to drive.
        fps:     Target frames per second.
        effect:  Currently active effect (or ``None``).
        running: Whether the render loop is active.
    """

    def __init__(
        self,
        devices: list[LifxDevice],
        fps: int = DEFAULT_FPS,
    ) -> None:
        """Initialize the engine.

        Args:
            devices: List of :class:`LifxDevice` (must have ``zone_count``
                     populated via :meth:`LifxDevice.query_all`).
            fps:     Target frames per second.  Must be positive.

        Raises:
            ValueError: If *devices* is empty or *fps* is not positive.
        """
        if not devices:
            raise ValueError("At least one device is required.")
        if fps <= 0:
            raise ValueError(f"fps must be positive, got {fps}.")

        self.devices: list[LifxDevice] = list(devices)  # defensive copy
        self.fps: int = fps
        self.effect: Optional[Effect] = None
        self.running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._lock: threading.Lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()
        self._effect_start_time: float = 0.0

    def start(self, effect: Effect) -> None:
        """Start or hot-swap the current effect.

        If the engine thread is not yet running it is spawned automatically.
        If an effect is already running, its :meth:`Effect.on_stop` is called
        before the new effect takes over.

        Args:
            effect: The new :class:`Effect` instance to render.

        Raises:
            TypeError: If *effect* is not an :class:`Effect` instance.
        """
        if not isinstance(effect, Effect):
            raise TypeError(
                f"Expected an Effect instance, got {type(effect).__name__}."
            )

        with self._lock:
            # Cleanly shut down the previous effect before swapping.
            if self.effect is not None:
                self.effect.on_stop()
            self.effect = effect
            self._effect_start_time = time.time()
            # Notify the new effect of each device's zone count so it can
            # perform any one-time setup (e.g., pre-allocating buffers).
            for dev in self.devices:
                if dev.zone_count:
                    effect.on_start(dev.zone_count)

        if not self.running:
            self.running = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
            )
            self._thread.start()

    def stop(self, fade_ms: int = DEFAULT_FADE_MS) -> None:
        """Stop the animation loop and optionally fade to black.

        Args:
            fade_ms: Transition time in milliseconds for the fade-to-black.
                     Pass 0 to skip the fade entirely.

        Raises:
            ValueError: If *fade_ms* is negative.
        """
        if fade_ms < MIN_FADE_MS:
            raise ValueError(
                f"fade_ms must be >= {MIN_FADE_MS}, got {fade_ms}."
            )

        # Signal the render thread to exit and wait for it.
        self.running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=THREAD_JOIN_TIMEOUT)
            self._thread = None

        # Clean up the active effect under the lock.
        with self._lock:
            if self.effect is not None:
                self.effect.on_stop()
                self.effect = None

        # Fade all zones to black so the lights don't freeze on the last frame.
        if fade_ms > 0:
            for dev in self.devices:
                if dev.zone_count:
                    if dev.is_multizone:
                        off = [(0, 0, 0, KELVIN_DEFAULT)] * dev.zone_count
                        dev.set_zones(off, duration_ms=fade_ms, rapid=False)
                    else:
                        # Single bulb (color or monochrome): fade to black.
                        dev.set_color(0, 0, 0, KELVIN_DEFAULT,
                                      duration_ms=fade_ms)

    def _run_loop(self) -> None:
        """Render loop -- runs in a background thread.

        Paces itself to the target FPS using
        :meth:`threading.Event.wait` for clean, interruptible sleep
        (unlike ``time.sleep``, it returns immediately when the stop
        event is set).
        """
        # Pre-compute the target interval to avoid division every frame.
        interval: float = 1.0 / self.fps

        while self.running and not self._stop_event.is_set():
            frame_start: float = time.time()

            # Snapshot the current effect and elapsed time under the lock
            # so hot-swaps are safe.
            with self._lock:
                effect: Optional[Effect] = self.effect
                t: float = frame_start - self._effect_start_time

            if effect is None:
                # No effect loaded; idle until one arrives or we're stopped.
                self._stop_event.wait(interval)
                continue

            for dev in self.devices:
                if dev.zone_count is None:
                    # Device hasn't been queried yet; skip it.
                    continue
                try:
                    colors: list = effect.render(t, dev.zone_count)
                    if dev.is_multizone:
                        dev.set_zones(colors, duration_ms=0, rapid=True)
                    elif dev.is_polychrome:
                        # Single color bulb: apply the first rendered color.
                        h, s, b, k = colors[0]
                        dev.set_color(h, s, b, k, duration_ms=0)
                    else:
                        # Monochrome bulb: BT.709 luma for perceptual brightness.
                        dev.set_color(*hsbk_to_luminance(*colors[0]),
                                      duration_ms=0)
                except Exception:
                    # Don't crash the loop on a single frame error.
                    # A transient render glitch or network hiccup should not
                    # bring down the entire animation.
                    pass

            # Frame pacing: sleep only the remaining time in this frame slot.
            elapsed: float = time.time() - frame_start
            sleep_time: float = interval - elapsed
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)


class Controller:
    """High-level, thread-safe interface for controlling the effect engine.

    Designed to be wrapped by a CLI today and a REST API tomorrow.

    Attributes:
        engine:  The underlying :class:`Engine`.
        devices: List of :class:`LifxDevice` being driven.
    """

    def __init__(
        self,
        devices: list[LifxDevice],
        fps: int = DEFAULT_FPS,
    ) -> None:
        """Initialize the controller.

        Args:
            devices: List of :class:`LifxDevice` to drive.
            fps:     Target frames per second.

        Raises:
            ValueError: If *devices* is empty or *fps* is not positive
                        (propagated from :class:`Engine`).
        """
        self.engine: Engine = Engine(devices, fps)
        self.devices: list[LifxDevice] = list(devices)  # defensive copy
        self._current_effect_name: Optional[str] = None

    def play(self, effect_name: str, **params: Any) -> None:
        """Start playing an effect by name.

        Args:
            effect_name: Registered effect name (e.g., ``"cylon"``).
            **params:    Parameter overrides forwarded to the effect.

        Raises:
            ValueError: If *effect_name* is not a registered effect
                        (propagated from :func:`create_effect`).
            TypeError:  If *effect_name* is not a string.
        """
        if not isinstance(effect_name, str):
            raise TypeError(
                f"effect_name must be a string, got {type(effect_name).__name__}."
            )
        # create_effect validates that the name exists in the registry
        # and raises ValueError with a helpful message if not.
        effect: Effect = create_effect(effect_name, **params)
        self._current_effect_name = effect_name
        self.engine.start(effect)

    def stop(self, fade_ms: int = DEFAULT_FADE_MS) -> None:
        """Stop the current effect and optionally fade to black.

        Args:
            fade_ms: Transition time in milliseconds for the fade-to-black.
                     Pass 0 to skip the fade entirely.
        """
        self.engine.stop(fade_ms=fade_ms)
        self._current_effect_name = None

    def update_params(self, **kwargs: Any) -> None:
        """Update parameters on the running effect.

        Unknown parameter names are silently ignored so that callers
        can pass a superset of params safely.

        This is a no-op if no effect is currently running.

        Args:
            **kwargs: Parameter names mapped to new values.
        """
        with self.engine._lock:
            if self.engine.effect is not None:
                self.engine.effect.set_params(**kwargs)

    def get_status(self) -> dict[str, Any]:
        """Return the current engine state as a JSON-serializable dict.

        Useful for API responses and status displays.

        Returns:
            A dict with keys ``running``, ``effect``, ``params``, ``fps``,
            and ``devices``.
        """
        with self.engine._lock:
            effect: Optional[Effect] = self.engine.effect
            return {
                "running": self.engine.running,
                "effect": self._current_effect_name,
                "params": effect.get_params() if effect else {},
                "fps": self.engine.fps,
                "devices": [
                    {
                        "ip": dev.ip,
                        "mac": dev.mac_str,
                        "label": dev.label,
                        "product": dev.product_name,
                        "zones": dev.zone_count,
                    }
                    for dev in self.devices
                ],
            }

    def list_effects(self) -> dict[str, Any]:
        """Return available effects with their parameter definitions.

        Returns:
            A dict mapping effect names to their descriptions and
            parameter metadata.  Each entry has the form::

                {
                    "description": "...",
                    "params": {
                        "param_name": {
                            "default": ...,
                            "min": ...,
                            "max": ...,
                            "description": "...",
                            "type": "float",
                        }
                    }
                }
        """
        result: dict[str, Any] = {}
        for name, cls in get_registry().items():
            params: dict[str, Any] = {}
            for pname, pdef in cls.get_param_defs().items():
                params[pname] = {
                    "default": pdef.default,
                    "min": pdef.min,
                    "max": pdef.max,
                    "description": pdef.description,
                    "type": type(pdef.default).__name__,
                }
                if pdef.choices:
                    params[pname]["choices"] = pdef.choices
            result[name] = {
                "description": cls.description,
                "params": params,
            }
        return result
