"""Animation engine for GlowUp effects.

The :class:`Engine` runs in a background thread, rendering the current effect
at a target frame rate and pushing frames to one or more :class:`Emitter`
instances.

The :class:`Controller` is the public interface -- it wraps the engine and
provides methods that are safe to call from any thread: CLI, REST API,
scheduler, etc.

Typical usage::

    from emitters.lifx import LifxEmitter
    from transport import LifxDevice
    from engine import Controller

    device = LifxDevice("<device-ip>")
    device.query_all()

    emitter = LifxEmitter.from_device(device)
    ctrl = Controller([emitter])
    ctrl.play("cylon", speed=1.5, width=12)
    # ... later ...
    ctrl.update_params(speed=3.0, hue=240)
    # ... later ...
    ctrl.stop()
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import collections

__version__ = "2.1"

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from effects import Effect, create_effect, get_registry, KELVIN_DEFAULT
from emitters import Emitter
from transport import SendMode

# ---------------------------------------------------------------------------
# Backward compatibility — VirtualMultizoneDevice moved to emitters/virtual.py
# ---------------------------------------------------------------------------
# Existing code that imports VirtualMultizoneDevice from engine will continue
# to work.  The class is now VirtualMultizoneEmitter in its canonical home.
from emitters.virtual import VirtualMultizoneEmitter as VirtualMultizoneDevice  # noqa: F401

_log: logging.Logger = logging.getLogger("glowup.engine")


def _exc_oneliner() -> str:
    """Return a compact 'ExceptionType: message' string for the current exception."""
    import sys
    exc = sys.exc_info()[1]
    if exc is None:
        return "unknown error"
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Named constants — no magic numbers
# ---------------------------------------------------------------------------

# Default rendering frame rate in frames per second.
DEFAULT_FPS: int = 20

# Default zones per bulb.  LIFX string lights use 3 zones per
# physical bulb.  Effects render to logical bulbs (zone_count // zpb)
# and the engine replicates each color zpb times.
DEFAULT_ZPB: int = 3

# Transition time multiplier.  The firmware interpolates between
# frames over this duration.  2x the frame interval ensures smooth
# crossfading — the device is always mid-interpolation when the
# next frame arrives, hiding frame boundaries.
TRANSITION_FACTOR: float = 2.0

# How long to wait (seconds) for the render thread to finish on stop().
THREAD_JOIN_TIMEOUT: float = 5.0

# Default transition time (ms) for fade-to-black when stopping an effect.
DEFAULT_FADE_MS: int = 500

# Minimum allowed fade duration in milliseconds (0 disables fade).
MIN_FADE_MS: int = 0

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

# Pipeline size for audio-reactive / media effects.  Smaller buffer
# trades smoothness for lower latency — at 20 FPS, 2 frames = ~100ms.
PIPELINE_LOW_LATENCY: int = 2


class Engine:
    """Low-level animation engine that runs in a background thread.

    Renders the active :class:`Effect` at a target frame rate and pushes
    each frame to every attached :class:`Emitter`.

    Attributes:
        emitters: List of :class:`Emitter` to drive.
        fps:      Target frames per second.
        effect:   Currently active effect (or ``None``).
        running:  Whether the render loop is active.
    """

    def __init__(
        self,
        emitters: list[Emitter],
        fps: int = DEFAULT_FPS,
        frame_callback: Optional[Callable] = None,
        transition_ms: Optional[int] = None,
        fps_explicit: bool = False,
        zones_per_bulb: int = DEFAULT_ZPB,
    ) -> None:
        """Initialize the engine.

        Args:
            emitters:       List of :class:`Emitter` instances to drive.
            fps:            Target frames per second.  Must be positive.
            frame_callback: Optional callable invoked after each frame
                            with the rendered color list.  Used by the
                            simulator to display a live preview.  Must
                            accept a single argument: ``list[HSBK]``.
            transition_ms:  Firmware transition time per frame in ms.
                            ``None`` uses the default (``2000 / fps``).
                            Set to 0 for instant snap, higher for smoother
                            interpolation at the cost of latency.
            fps_explicit:   ``True`` if the caller explicitly set FPS
                            (e.g. via ``--fps``).  When ``False``, the
                            engine may auto-tune FPS for Neon-class
                            devices.
            zones_per_bulb: Number of zones per physical bulb.  The
                            effect renders ``zone_count // zpb`` logical
                            bulbs and each color is replicated ``zpb``
                            times.  Default is 3 (LIFX string lights).

        Raises:
            ValueError: If *emitters* is empty or *fps* is not positive.
        """
        if not emitters:
            raise ValueError("At least one emitter is required.")
        if fps <= 0:
            raise ValueError(f"fps must be positive, got {fps}.")

        self.emitters: list[Emitter] = list(emitters)  # defensive copy
        self.fps: int = fps
        self.zones_per_bulb: int = max(1, zones_per_bulb)
        self._transition_ms_override: Optional[int] = transition_ms
        self._fps_explicit: bool = fps_explicit

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

        # --- Signal bindings (media pipeline integration) ---
        # When an effect is played with bindings, the render thread reads
        # signal values from the bus and overwrites the bound effect params
        # each frame before calling render().  This lets any existing effect
        # respond to audio/video without modification.
        self._bindings: Optional[dict[str, dict]] = None
        self._signal_bus: Optional[Any] = None

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

        # --- Audio sync delay buffer ---
        # When calibration determines that audio arrives N frames later
        # than the lights, we buffer N frames and send the oldest,
        # effectively delaying the lights to match the audio.
        self._audio_delay_frames: int = 0
        self._delay_buffer: collections.deque = collections.deque()

    def start(self, effect: Effect,
              bindings: Optional[dict[str, dict]] = None,
              signal_bus: Optional[Any] = None) -> None:
        """Start or hot-swap the current effect.

        If the engine thread is not yet running it is spawned automatically.
        If an effect is already running, its :meth:`Effect.on_stop` is called
        before the new effect takes over.

        Args:
            effect:     The new :class:`Effect` instance to render.
            bindings:   Optional dict mapping param names to binding dicts.
                        Each binding has ``"signal"`` (signal bus name) and
                        optional ``"scale"`` ([lo, hi]) and ``"reduce"``
                        (for array signals: ``"max"``, ``"mean"``, ``"sum"``).
            signal_bus: Optional :class:`SignalBus` for reading bound signals.

        Raises:
            TypeError: If *effect* is not an :class:`Effect` instance.
        """
        if not isinstance(effect, Effect):
            raise TypeError(
                f"Expected an Effect instance, got {type(effect).__name__}."
            )

        # Let each emitter perform its own startup ritual (e.g., clearing
        # the LIFX firmware committed state to black).
        for em in self.emitters:
            em.prepare_for_rendering()

        with self._lock:
            # Cleanly shut down the previous effect before swapping.
            if self.effect is not None:
                self.effect.on_stop()
            self.effect = effect
            self._effect_start_time = time.time()
            # Store signal bindings for the render thread.
            self._bindings = bindings
            self._signal_bus = signal_bus
            # If this is a MediaEffect, give it direct bus access.
            if signal_bus is not None and hasattr(effect, '_signal_bus'):
                effect._signal_bus = signal_bus
            # When a signal bus is active (media/audio-reactive effects),
            # shrink the pipeline to minimize latency.  The standard 10-frame
            # buffer adds 500ms at 20 FPS — unacceptable for real-time audio.
            # Two frames (~100ms) is enough to absorb scheduling jitter while
            # keeping the response perceptually instantaneous.
            if signal_bus is not None:
                self._pipeline = queue.Queue(maxsize=PIPELINE_LOW_LATENCY)
            else:
                self._pipeline = queue.Queue(maxsize=PIPELINE_HIGH_WATER)

            # Increment generation so the send thread discards stale frames
            # pre-rendered by the old effect.
            self._effect_generation += 1
            # Notify the new effect of each emitter's zone count so it can
            # perform any one-time setup (e.g., pre-allocating buffers).
            for em in self.emitters:
                if em.zone_count is not None:
                    effect.on_start(em.zone_count)

        # Flush any stale pre-rendered frames from the pipeline,
        # including the audio delay buffer — old effect's frames must
        # not leak into the new effect's output.
        self._flush_pipeline()
        self._delay_buffer.clear()

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

    def set_audio_delay(self, delay_seconds: float) -> None:
        """Set the audio synchronization delay.

        Delays light frames by the specified duration so they arrive
        at the same perceptual moment as the audio stream.  The delay
        is implemented as a FIFO buffer in the send thread.

        Args:
            delay_seconds: Delay in seconds.  0 disables the delay.
        """
        n_frames: int = max(0, round(delay_seconds * self.fps))
        _log.info(
            "Audio sync delay set: %.3fs = %d frames @ %d fps",
            delay_seconds, n_frames, self.fps,
        )
        self._audio_delay_frames = n_frames
        self._delay_buffer.clear()

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
        # handles the visual fade via power_off(duration_ms=...).
        # Using duration_ms=0 here prevents conflicts with any in-progress
        # transition from the render loop's non-zero duration.
        if fade_ms > 0:
            for em in self.emitters:
                if em.zone_count is not None:
                    if hasattr(em, 'is_matrix') and em.is_matrix:
                        off = [(0, 0, 0, KELVIN_DEFAULT)] * em.zone_count
                        em.send_tile_zones(off, duration_ms=0)
                    elif em.is_multizone:
                        off = [(0, 0, 0, KELVIN_DEFAULT)] * em.zone_count
                        em.send_zones(off, duration_ms=0,
                                      mode=SendMode.GUARANTEED)
                    else:
                        # Single-zone emitter: fade to black.
                        em.send_color(0, 0, 0, KELVIN_DEFAULT,
                                      duration_ms=fade_ms)

    def _render_thread(self) -> None:
        """Producer thread — pre-renders frames into the pipeline buffer.

        Runs ahead of the send thread, filling the bounded queue with
        ``(frame_dict, generation)`` tuples.  Uses water-mark flow
        control: pauses when the queue reaches high water, resumes when
        the send thread drains it to low water.  This lets rendering
        happen in efficient bursts rather than waking per frame.

        Each frame_dict maps ``id(em)`` to the rendered color list for
        that emitter, so multi-emitter setups get per-emitter zone counts
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
                bindings: Optional[dict] = self._bindings
                signal_bus = self._signal_bus

            if effect is None:
                self._stop_event.wait(interval)
                continue

            # --- Signal binding resolution ---
            # Before rendering, overwrite bound effect params with live
            # signal values from the bus.  This lets any existing effect
            # respond to audio/video/sensor data without modification.
            if bindings and signal_bus:
                self._resolve_bindings(effect, bindings, signal_bus)

            # Use wall-clock time.  The pipeline depth is small enough
            # (500 ms at high water) that the time skew between render
            # and send is imperceptible, and stochastic effects need
            # real elapsed time for their random processes.
            t: float = time.time() - start_time

            # Render for every emitter.
            # If zpb > 1, tell the effect to render fewer zones (one
            # per bulb) and replicate each color zpb times.  This
            # gives every effect uniform bulb grouping without needing
            # per-effect zpb awareness.
            zpb: int = self.zones_per_bulb
            frame: dict[int, list] = {}
            for em in self.emitters:
                if em.zone_count is None:
                    continue
                try:
                    # Matrix devices use 1:1 pixel mapping — no bulb
                    # grouping.  zpb only applies to 1D multizone strips
                    # where N adjacent zones form one physical bulb.
                    em_is_matrix: bool = (
                        hasattr(em, 'is_matrix') and em.is_matrix
                    )
                    em_zpb: int = 1 if em_is_matrix else zpb
                    logical_zones: int = max(1, em.zone_count // em_zpb)
                    colors: list = effect.render(t, logical_zones)
                    if em_zpb > 1:
                        # Replicate each color zpb times.
                        expanded: list = []
                        for c in colors:
                            expanded.extend([c] * zpb)
                        # Trim or pad to exact zone count.
                        colors = expanded[:em.zone_count]
                        # Pad to exact zone count.  Guard against empty
                        # list — effect returned [] or zone_count is 0.
                        _pad: tuple = colors[-1] if colors else (0, 0, 0, KELVIN_DEFAULT)
                        while len(colors) < em.zone_count:
                            colors.append(_pad)
                    frame[id(em)] = colors
                except Exception:
                    import traceback as _tb
                    _log.warning(
                        "Render failed for emitter %s: %s\n%s",
                        getattr(em, 'label', id(em)),
                        _exc_oneliner(),
                        _tb.format_exc(),
                    )

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
        emitters at exact frame intervals.  Because rendering happened
        asynchronously in the producer thread, OS scheduling jitter
        and render-time variance do not affect send timing.

        If the pipeline is empty (render thread can't keep up), the
        last sent frame is held — no visual glitch, just a repeated
        frame.
        """
        # Pre-compute the target interval to avoid division every frame.
        interval: float = 1.0 / self.fps
        # Transition duration = 1x frame interval.  With ack-paced sends
        # every frame is confirmed delivered, so the old 2x overlap that
        # compensated for dropped UDP packets is no longer needed.
        # The CLI --transition flag overrides this for fine-tuning.
        if self._transition_ms_override is not None:
            transition_ms: int = self._transition_ms_override
        else:
            transition_ms = int(TRANSITION_FACTOR * 1000.0 / self.fps)

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

            # --- Audio sync delay buffer ---
            # When calibrated, buffer N frames and send the oldest,
            # effectively delaying the lights to match audio latency.
            if self._audio_delay_frames > 0:
                self._delay_buffer.append(last_colors)
                if len(self._delay_buffer) > self._audio_delay_frames:
                    last_colors = self._delay_buffer.popleft()
                else:
                    # Buffer is filling — send black (silence).
                    last_colors = {}

            # Send the frame to all emitters.
            colors: list = []
            for em in self.emitters:
                if em.zone_count is None:
                    continue
                em_colors: Optional[list] = last_colors.get(id(em))
                if em_colors is None:
                    continue
                colors = em_colors
                try:
                    if hasattr(em, 'is_matrix') and em.is_matrix:
                        em.send_tile_zones(em_colors,
                                           duration_ms=transition_ms)
                    elif em.is_multizone:
                        em.send_zones(em_colors,
                                      duration_ms=transition_ms)
                    else:
                        h, s, b, k = em_colors[0]
                        em.send_color(h, s, b, k,
                                      duration_ms=transition_ms)
                except Exception:
                    _log.warning(
                        "Send failed for emitter %s: %s",
                        getattr(em, 'label', id(em)),
                        _exc_oneliner(),
                    )

            # Store the last sent frame for SSE streaming.
            if colors:
                with self._last_frame_lock:
                    self._last_frame = colors

                # Notify the frame callback (e.g., simulator).
                if self._frame_callback is not None:
                    try:
                        self._frame_callback(colors)
                    except Exception:
                        _log.warning(
                            "Frame callback failed: %s", _exc_oneliner(),
                        )

            # Frame pacing: sleep only the remaining time in this frame slot.
            elapsed: float = time.time() - frame_start
            sleep_time: float = interval - elapsed
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

    @staticmethod
    def _resolve_bindings(effect: Effect, bindings: dict[str, dict],
                          signal_bus: Any) -> None:
        """Overwrite effect params with live signal values from the bus.

        For each binding, reads the named signal, applies optional array
        reduction and scaling, then sets the param on the effect.

        Args:
            effect:     The active effect instance.
            bindings:   Dict mapping param names to binding specifications.
            signal_bus: The :class:`SignalBus` to read from.
        """
        for param_name, binding in bindings.items():
            signal_name: str = binding.get("signal", "")
            if not signal_name:
                continue

            value = signal_bus.read(signal_name, 0.0)

            # Reduce array signals to a scalar.
            if isinstance(value, list):
                reduce_fn: str = binding.get("reduce", "max")
                if not value:
                    value = 0.0
                elif reduce_fn == "max":
                    value = max(value)
                elif reduce_fn == "mean":
                    value = sum(value) / len(value)
                elif reduce_fn == "sum":
                    value = min(1.0, sum(value))
                else:
                    value = max(value)

            # Scale the normalized [0, 1] value to the param's range.
            scale = binding.get("scale")
            if scale and len(scale) >= 2:
                lo, hi = float(scale[0]), float(scale[1])
            else:
                # Use the param's own min/max from the Param definition.
                param_def = effect._param_defs.get(param_name)
                if param_def:
                    lo, hi = param_def.min, param_def.max
                else:
                    lo, hi = 0.0, 1.0

            scaled: float = lo + float(value) * (hi - lo)
            try:
                setattr(effect, param_name, scaled)
            except Exception:
                _log.warning(
                    "Failed to set param %s on effect: %s",
                    param_name, _exc_oneliner(),
                )

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
        engine:   The underlying :class:`Engine`.
        emitters: List of :class:`Emitter` being driven.
    """

    def __init__(
        self,
        emitters: list[Emitter],
        fps: int = DEFAULT_FPS,
        frame_callback: Optional[Callable] = None,
        transition_ms: Optional[int] = None,
        fps_explicit: bool = False,
        zones_per_bulb: int = DEFAULT_ZPB,
    ) -> None:
        """Initialize the controller.

        Args:
            emitters:       List of :class:`Emitter` to drive.
            fps:            Target frames per second.
            frame_callback: Optional callable forwarded to the
                            :class:`Engine` for per-frame notifications
                            (e.g., live simulator preview).
            transition_ms:  Override firmware transition time per frame (ms).
                            ``None`` uses the default (``2000 / fps``).
            fps_explicit:   ``True`` if the caller explicitly set ``--fps``.
            zones_per_bulb: Zones per physical bulb (default 3).

        Raises:
            ValueError: If *emitters* is empty or *fps* is not positive
                        (propagated from :class:`Engine`).
        """
        self.engine: Engine = Engine(emitters, fps,
                                     frame_callback=frame_callback,
                                     transition_ms=transition_ms,
                                     fps_explicit=fps_explicit,
                                     zones_per_bulb=zones_per_bulb)
        self.emitters: list[Emitter] = list(emitters)  # defensive copy
        self._current_effect_name: Optional[str] = None
        self._last_effect_name: Optional[str] = None
        self._last_params: dict[str, Any] = {}
        self._bindings: Optional[dict[str, dict]] = None

    def play(self, effect_name: str,
             bindings: Optional[dict[str, dict]] = None,
             signal_bus: Optional[Any] = None,
             **params: Any) -> None:
        """Start playing an effect by name.

        Args:
            effect_name: Registered effect name (e.g., ``"cylon"``).
            bindings:    Optional dict mapping param names to signal bindings.
                         Each binding has ``"signal"`` (bus signal name) and
                         optional ``"scale"`` ([lo, hi]) and ``"reduce"``
                         (``"max"``, ``"mean"``, ``"sum"``).
            signal_bus:  Optional :class:`SignalBus` for reading signals.
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
        self._bindings = bindings
        self.engine.start(effect, bindings=bindings, signal_bus=signal_bus)

    def set_audio_delay(self, delay_seconds: float) -> None:
        """Set the audio synchronization delay on the engine.

        See :meth:`Engine.set_audio_delay` for details.

        Args:
            delay_seconds: Delay in seconds.  0 disables.
        """
        self.engine.set_audio_delay(delay_seconds)

    def stop(self, fade_ms: int = DEFAULT_FADE_MS) -> None:
        """Stop the current effect and optionally fade to black.

        Args:
            fade_ms: Transition time in milliseconds for the fade-to-black.
                     Pass 0 to skip the fade entirely.
        """
        self.engine.stop(fade_ms=fade_ms)
        self._current_effect_name = None

    def set_power(self, on: bool, duration_ms: int = 0) -> None:
        """Turn all emitters on or off.

        Args:
            on:          ``True`` to power on, ``False`` to power off.
            duration_ms: Transition duration in milliseconds.
        """
        for em in self.emitters:
            if on:
                em.power_on(duration_ms=duration_ms)
            else:
                em.power_off(duration_ms=duration_ms)

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
            and ``emitters``.
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
            status: dict[str, Any] = {
                "running": self.engine.running,
                "effect": effect_name,
                "params": params,
                "fps": self.engine.fps,
                "devices": [em.get_info() for em in self.emitters],
            }
            if self._bindings:
                status["bindings"] = self._bindings
            return status

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
