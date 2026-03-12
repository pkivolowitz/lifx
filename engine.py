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

__version__ = "1.6"

import queue
import threading
import time
from typing import Any, Callable, Optional

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

# Pipeline buffer capacity (high water mark).
# At 20 FPS this is 500 ms of lookahead — enough to absorb OS scheduling
# jitter and render-time variance without adding perceptible latency
# to parameter changes.  The render thread pauses when the buffer is full.
PIPELINE_HIGH_WATER: int = 10

# Pipeline low water mark.  The render thread is woken when the buffer
# drains to this level, ensuring it stays ahead of the send thread.
# Set to half the high water mark so the render thread re-fills in bursts
# rather than waking on every consumed frame.
PIPELINE_LOW_WATER: int = 5


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

    def __init__(
        self,
        devices: list[LifxDevice],
        name: str = "",
        owns_devices: bool = True,
    ) -> None:
        """Initialize with a list of connected, queried devices.

        Builds a zone map that records which physical device and zone index
        each virtual zone corresponds to.  Multizone devices expand to
        their full zone count; single-bulb devices occupy one zone.

        The order of *devices* determines the virtual zone layout — the
        first device's zones come first.  For grouped string lights this
        means the list order defines left-to-right position on the canvas.

        Args:
            devices:      :class:`LifxDevice` instances, each already
                          connected and queried via
                          :meth:`LifxDevice.query_all`.  The list order
                          determines the zone assignment.
            name:         Optional group name (used for display and as the
                          device identifier in the server API).
            owns_devices: If ``True`` (default), :meth:`close` closes all
                          member device sockets.  Set to ``False`` when
                          the caller manages device lifetimes separately
                          (e.g., the server stores devices individually).

        Raises:
            ValueError: If *devices* is empty.
        """
        if not devices:
            raise ValueError("VirtualMultizoneDevice requires at least one device.")

        self._devices: list[LifxDevice] = list(devices)
        self._owns_devices: bool = owns_devices
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
        if name:
            self.ip: str = f"group:{name}"
            self.label: str = name
        else:
            self.ip: str = f"group({len(devices)} devices)"
            self.label: str = "Virtual group"
        self.product_name: str = f"{self.zone_count}-zone virtual multizone"
        self.mac_str: str = "virtual"
        self.product: int = 0  # non-None so engine query checks pass
        self.group: str = name

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

    def clear_firmware_effect(self) -> None:
        """Clear firmware-level effects on all multizone member devices."""
        for dev in self._devices:
            if dev.is_multizone:
                dev.clear_firmware_effect()

    def close(self) -> None:
        """Close all wrapped device sockets.

        Only closes sockets if this instance owns the devices (see
        *owns_devices* constructor parameter).  When the server manages
        device lifetimes separately, this is a no-op.
        """
        if self._owns_devices:
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
        frame_callback: Optional[Callable] = None,
    ) -> None:
        """Initialize the engine.

        Args:
            devices:        List of :class:`LifxDevice` (must have
                            ``zone_count`` populated via
                            :meth:`LifxDevice.query_all`).
            fps:            Target frames per second.  Must be positive.
            frame_callback: Optional callable invoked after each frame
                            with the rendered color list.  Used by the
                            simulator to display a live preview.  Must
                            accept a single argument: ``list[HSBK]``.

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
        self._send_thread: Optional[threading.Thread] = None
        self._render_thread_handle: Optional[threading.Thread] = None
        self._lock: threading.Lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()
        self._effect_start_time: float = 0.0
        self._frame_callback: Optional[Callable] = frame_callback
        # Last rendered frame, stored for SSE streaming without UDP queries.
        self._last_frame: Optional[list[tuple[int, int, int, int]]] = None
        self._last_frame_lock: threading.Lock = threading.Lock()

        # --- Frame pipeline ---
        # Pre-rendered frames flow from the render thread (producer) through
        # a bounded queue to the send thread (consumer).  This decouples
        # render jitter from send timing, producing smoother animations.
        self._pipeline: queue.Queue = queue.Queue(maxsize=PIPELINE_HIGH_WATER)
        # Condition variable for water-mark signalling.  The render thread
        # waits when the queue is at high water and is notified when the
        # send thread drains it to low water.
        self._water_cond: threading.Condition = threading.Condition()
        # Generation counter: incremented on each effect swap so the send
        # thread can discard stale pre-rendered frames from the old effect.
        self._effect_generation: int = 0

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

        # Clear the persistent committed state to black on every device
        # before starting the render loop.  The extended multizone protocol
        # (type 510) writes to a temporary overlay; if a UDP frame is lost
        # the firmware briefly reveals the committed layer.  Ensuring it is
        # black makes those glitches invisible.
        for dev in self.devices:
            if dev.is_multizone and dev.zone_count:
                dev.set_color(0, 0, 0, KELVIN_DEFAULT, duration_ms=0)

        with self._lock:
            # Cleanly shut down the previous effect before swapping.
            if self.effect is not None:
                self.effect.on_stop()
            self.effect = effect
            self._effect_start_time = time.time()
            # Increment generation so the send thread discards stale frames
            # pre-rendered by the old effect.
            self._effect_generation += 1
            # Notify the new effect of each device's zone count so it can
            # perform any one-time setup (e.g., pre-allocating buffers).
            for dev in self.devices:
                if dev.zone_count:
                    effect.on_start(dev.zone_count)

        # Flush any stale pre-rendered frames from the pipeline.
        self._flush_pipeline()

        if not self.running:
            self.running = True
            self._stop_event.clear()
            # Spawn the render (producer) thread.
            self._render_thread_handle = threading.Thread(
                target=self._render_thread,
                daemon=True,
                name="glowup-render",
            )
            self._render_thread_handle.start()
            # Spawn the send (consumer) thread.
            self._send_thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="glowup-send",
            )
            self._send_thread.start()

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

        # Signal both threads to exit and wait for them.
        self.running = False
        self._stop_event.set()
        # Wake the render thread if it's blocked on the water-mark condition.
        with self._water_cond:
            self._water_cond.notify_all()
        if self._render_thread_handle is not None:
            self._render_thread_handle.join(timeout=THREAD_JOIN_TIMEOUT)
            self._render_thread_handle = None
        if self._send_thread is not None:
            self._send_thread.join(timeout=THREAD_JOIN_TIMEOUT)
            self._send_thread = None

        # Clean up the active effect under the lock.
        with self._lock:
            if self.effect is not None:
                self.effect.on_stop()
                self.effect = None

        # Snap the overlay to black immediately.  The caller (glowup.py)
        # handles the visual fade via set_power(on=False, duration_ms=...).
        # Using duration_ms=0 here prevents conflicts with any in-progress
        # transition from the render loop's non-zero duration.
        if fade_ms > 0:
            for dev in self.devices:
                if dev.zone_count:
                    if dev.is_multizone:
                        off = [(0, 0, 0, KELVIN_DEFAULT)] * dev.zone_count
                        dev.set_zones(off, duration_ms=0, rapid=False)
                    else:
                        # Single bulb (color or monochrome): fade to black.
                        dev.set_color(0, 0, 0, KELVIN_DEFAULT,
                                      duration_ms=fade_ms)

    def _render_thread(self) -> None:
        """Producer thread — pre-renders frames into the pipeline buffer.

        Runs ahead of the send thread, filling the bounded queue with
        ``(frame_dict, generation)`` tuples.  Uses water-mark flow
        control: pauses when the queue reaches high water, resumes when
        the send thread drains it to low water.  This lets rendering
        happen in efficient bursts rather than waking per frame.

        Each frame_dict maps ``id(dev)`` to the rendered color list for
        that device, so multi-device setups get per-device zone counts
        handled correctly.
        """
        interval: float = 1.0 / self.fps

        while self.running and not self._stop_event.is_set():
            # --- Water-mark flow control ---
            # Pause rendering when the pipeline is full.  The send thread
            # notifies us when it drains to low water.
            with self._water_cond:
                while (self._pipeline.qsize() >= PIPELINE_HIGH_WATER
                       and self.running
                       and not self._stop_event.is_set()):
                    self._water_cond.wait(timeout=interval)

            if not self.running or self._stop_event.is_set():
                break

            # Snapshot the current effect under the lock.
            with self._lock:
                effect: Optional[Effect] = self.effect
                start_time: float = self._effect_start_time
                gen: int = self._effect_generation

            if effect is None:
                self._stop_event.wait(interval)
                continue

            # Use wall-clock time.  The pipeline depth is small enough
            # (500 ms at high water) that the time skew between render
            # and send is imperceptible, and stochastic effects need
            # real elapsed time for their random processes.
            t: float = time.time() - start_time

            # Render for every device.
            frame: dict[int, list] = {}
            for dev in self.devices:
                if dev.zone_count is None:
                    continue
                try:
                    frame[id(dev)] = effect.render(t, dev.zone_count)
                except Exception:
                    pass

            if not frame:
                self._stop_event.wait(interval)
                continue

            # Push into the pipeline.  Use a short timeout so we can
            # re-check the stop event if the queue is still full.
            try:
                self._pipeline.put((frame, gen), timeout=interval)
            except queue.Full:
                pass

    def _run_loop(self) -> None:
        """Consumer thread — sends pre-rendered frames on a strict clock.

        Pops frames from the pipeline buffer and transmits them to
        devices at exact frame intervals.  Because rendering happened
        asynchronously in the producer thread, OS scheduling jitter
        and render-time variance do not affect send timing.

        If the pipeline is empty (render thread can't keep up), the
        last sent frame is held — no visual glitch, just a repeated
        frame.
        """
        # Pre-compute the target interval to avoid division every frame.
        interval: float = 1.0 / self.fps
        # Transition duration = 2× frame interval.  This keeps the firmware
        # mid-interpolation when the next frame arrives, so a single dropped
        # UDP packet never exposes the committed layer.
        transition_ms: int = int(2000.0 / self.fps)

        last_colors: dict[int, list] = {}

        while self.running and not self._stop_event.is_set():
            frame_start: float = time.time()

            # Try to pop a pre-rendered frame from the pipeline.
            frame: Optional[dict[int, list]] = None
            frame_gen: int = -1
            try:
                frame, frame_gen = self._pipeline.get_nowait()
            except queue.Empty:
                pass

            # Signal the render thread when we cross the low water mark.
            # This lets it re-fill in bursts rather than waking per frame.
            if self._pipeline.qsize() <= PIPELINE_LOW_WATER:
                with self._water_cond:
                    self._water_cond.notify()

            # If the pipeline is empty, hold the last frame.
            # If the frame is from a stale effect generation (hot-swap
            # happened), discard it and drain the queue.
            if frame is not None:
                with self._lock:
                    current_gen: int = self._effect_generation
                if frame_gen == current_gen:
                    last_colors = frame
                else:
                    # Stale frame — drain any remaining stale frames.
                    self._flush_pipeline()
                    frame = None

            # Send the frame to all devices.
            colors: list = []
            for dev in self.devices:
                if dev.zone_count is None:
                    continue
                dev_colors: Optional[list] = last_colors.get(id(dev))
                if dev_colors is None:
                    continue
                colors = dev_colors
                try:
                    if dev.is_multizone:
                        dev.set_zones(dev_colors, duration_ms=transition_ms,
                                      rapid=True)
                    elif dev.is_polychrome:
                        h, s, b, k = dev_colors[0]
                        dev.set_color(h, s, b, k, duration_ms=0)
                    else:
                        dev.set_color(*hsbk_to_luminance(*dev_colors[0]),
                                      duration_ms=0)
                except Exception:
                    pass

            # Store the last sent frame for SSE streaming.
            if colors:
                with self._last_frame_lock:
                    self._last_frame = colors

                # Notify the frame callback (e.g., simulator).
                if self._frame_callback is not None:
                    try:
                        self._frame_callback(colors)
                    except Exception:
                        pass

            # Frame pacing: sleep only the remaining time in this frame slot.
            elapsed: float = time.time() - frame_start
            sleep_time: float = interval - elapsed
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    def _flush_pipeline(self) -> None:
        """Drain all frames from the pipeline buffer.

        Called on effect hot-swap to discard stale pre-rendered frames
        so the new effect's output appears immediately.
        """
        while not self._pipeline.empty():
            try:
                self._pipeline.get_nowait()
            except queue.Empty:
                break


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
        frame_callback: Optional[Callable] = None,
    ) -> None:
        """Initialize the controller.

        Args:
            devices:        List of :class:`LifxDevice` to drive.
            fps:            Target frames per second.
            frame_callback: Optional callable forwarded to the
                            :class:`Engine` for per-frame notifications
                            (e.g., live simulator preview).

        Raises:
            ValueError: If *devices* is empty or *fps* is not positive
                        (propagated from :class:`Engine`).
        """
        self.engine: Engine = Engine(devices, fps,
                                     frame_callback=frame_callback)
        self.devices: list[LifxDevice] = list(devices)  # defensive copy
        self._current_effect_name: Optional[str] = None
        self._last_effect_name: Optional[str] = None
        self._last_params: dict[str, Any] = {}

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
        self._last_effect_name = effect_name
        self._last_params = dict(params)
        self.engine.start(effect)

    def stop(self, fade_ms: int = DEFAULT_FADE_MS) -> None:
        """Stop the current effect and optionally fade to black.

        Args:
            fade_ms: Transition time in milliseconds for the fade-to-black.
                     Pass 0 to skip the fade entirely.
        """
        self.engine.stop(fade_ms=fade_ms)
        self._current_effect_name = None

    def set_power(self, on: bool, duration_ms: int = 0) -> None:
        """Turn all devices on or off.

        Args:
            on:          ``True`` to power on, ``False`` to power off.
            duration_ms: Firmware transition duration in milliseconds.
        """
        for dev in self.devices:
            dev.set_power(on=on, duration_ms=duration_ms)

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
            # Report live params if running, otherwise recall the last
            # played effect so the client can restart it.
            if effect is not None:
                effect_name: Optional[str] = self._current_effect_name
                params: dict[str, Any] = effect.get_params()
            else:
                effect_name = self._last_effect_name
                params = self._last_params
            return {
                "running": self.engine.running,
                "effect": effect_name,
                "params": params,
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

    def get_last_frame(self) -> Optional[list[tuple[int, int, int, int]]]:
        """Return the most recently rendered frame of HSBK colors.

        Thread-safe: reads a snapshot stored by the engine's render loop.
        Returns ``None`` if no frame has been rendered yet.

        Returns:
            A list of ``(h, s, b, k)`` tuples, one per zone, or ``None``.
        """
        with self.engine._last_frame_lock:
            return self.engine._last_frame

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
