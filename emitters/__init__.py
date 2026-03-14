"""Emitter framework for the SOE (Sensors -> Operators -> Emitters) pipeline.

Emitters are the output terminus of the pipeline: they receive processed data
and express it in a specific medium.  A relay, an 8K display, a CSV file, a
text message, and a bank of LIFX bulbs are all emitters.

To create a new emitter:
    1. Create a file in ``emitters/`` (e.g., ``emitters/csv.py``).
    2. Subclass :class:`Emitter`.
    3. Define params as class-level :class:`Param` instances.
    4. Implement :meth:`Emitter.on_emit` and :meth:`Emitter.capabilities`.

The emitter is automatically registered and available by type.

Example::

    class RelayEmitter(Emitter):
        emitter_type = "relay"
        description = "GPIO relay on/off"

        gpio_pin = Param(17, min=0, max=27,
                         description="BCM GPIO pin number")

        def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
            gpio.output(self.gpio_pin, bool(frame))
            return True

        def capabilities(self) -> EmitterCapabilities:
            return EmitterCapabilities(
                accepted_frame_types=["binary"],
                max_rate_hz=10.0,
            )
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "0.2"

import fnmatch
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

# Reuse the Param dataclass from effects — same declaration semantics,
# same validate/clamp behavior.  No reason to duplicate it.
from effects import Param

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Metadata keys always present in the dict passed to on_emit().
META_KEY_TIME: str = "t"             # float — seconds since pipeline start
META_KEY_DELTA: str = "dt"           # float — seconds since last emit
META_KEY_FRAME_NUMBER: str = "frame_number"  # int — monotonic frame counter

# Maximum consecutive on_emit() failures before auto-disabling an emitter.
MAX_CONSECUTIVE_FAILURES: int = 10

# Poll rate for the periodic/event dispatch thread (Hz).
PERIODIC_POLL_HZ: float = 50.0

# Derived poll interval (seconds).
PERIODIC_POLL_INTERVAL: float = 1.0 / PERIODIC_POLL_HZ

# How often the periodic thread calls on_flush() on buffered emitters (seconds).
FLUSH_INTERVAL: float = 5.0

# Valid timing mode strings for EmitterManager dispatch.
TIMING_CONTINUOUS: str = "continuous"
TIMING_PERIODIC: str = "periodic"
TIMING_EVENT: str = "event"
VALID_TIMING_MODES: frozenset[str] = frozenset({
    TIMING_CONTINUOUS, TIMING_PERIODIC, TIMING_EVENT,
})

# Valid edge detection modes for event-driven emitters.
EDGE_RISING: str = "rising"
EDGE_FALLING: str = "falling"
EDGE_ANY: str = "any"
VALID_EDGE_MODES: frozenset[str] = frozenset({
    EDGE_RISING, EDGE_FALLING, EDGE_ANY,
})

# Keys consumed by EmitterManager from per-emitter config dicts.
# These are stripped before passing the remaining config to the emitter.
_MANAGER_KEYS: frozenset[str] = frozenset({
    "type", "timing", "rate_hz", "trigger_signal", "trigger_threshold",
    "trigger_edge", "cooldown_seconds", "signals",
})

# Global registry mapping emitter_type -> Emitter subclass.
_registry: dict[str, type["Emitter"]] = {}

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.emitters")


# ---------------------------------------------------------------------------
# EmitterCapabilities — what an emitter accepts
# ---------------------------------------------------------------------------

@dataclass
class EmitterCapabilities:
    """Declare what frame types and rates an emitter can handle.

    Used at pipeline construction time to validate that an Operator's
    output is compatible with the Emitter, and by the capability registry
    and iOS Surface picker to filter valid pairings.

    Attributes:
        accepted_frame_types: Frame type strings this emitter accepts
            (e.g., ``["strip", "single"]`` for LIFX, ``["scalar"]`` for
            a relay, ``["snapshot"]`` for a database logger).
        max_rate_hz: Maximum meaningful update rate.  The EmitterManager
            will not call :meth:`Emitter.on_emit` faster than this.
        variable_topology: Whether the zone/pixel count can change after
            configure (True for LIFX groups, False for fixed-width DMX).
        zones: 1D topology dimension (populated after configure).
        width: 2D topology width (populated after configure).
        height: 2D topology height (populated after configure).
        extra: Arbitrary metadata for capability advertisement (MQTT).
    """

    accepted_frame_types: list[str] = field(default_factory=list)
    max_rate_hz: float = 60.0
    variable_topology: bool = False
    zones: int = 0
    width: int = 0
    height: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API responses and MQTT capability messages.

        Returns:
            A JSON-serializable dict of this capability declaration.
        """
        d: dict[str, Any] = {
            "accepted_frame_types": self.accepted_frame_types,
            "max_rate_hz": self.max_rate_hz,
            "variable_topology": self.variable_topology,
        }
        if self.zones:
            d["zones"] = self.zones
        if self.width:
            d["width"] = self.width
            d["height"] = self.height
        if self.extra:
            d.update(self.extra)
        return d


# ---------------------------------------------------------------------------
# EmitterMeta — auto-registration metaclass
# ---------------------------------------------------------------------------

class EmitterMeta(type):
    """Metaclass that auto-registers :class:`Emitter` subclasses.

    Any concrete subclass with a non-``None`` :attr:`Emitter.emitter_type`
    is added to the global :data:`_registry` at class-creation time.
    """

    def __init__(
        cls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
    ) -> None:
        """Register the new Emitter subclass if it declares an emitter_type.

        Args:
            name:      Class name (set by Python).
            bases:     Base classes tuple.
            namespace: Class body namespace dict.
        """
        super().__init__(name, bases, namespace)
        # Only register concrete subclasses that define an emitter_type,
        # not the abstract Emitter base class itself.
        if bases and hasattr(cls, "emitter_type") and cls.emitter_type is not None:
            _registry[cls.emitter_type] = cls


# ---------------------------------------------------------------------------
# Emitter ABC
# ---------------------------------------------------------------------------

class Emitter(metaclass=EmitterMeta):
    """Abstract base class for all output endpoints in the SOE pipeline.

    An emitter translates abstract frames produced by an Operator into
    a specific hardware protocol, file format, or communication channel.

    Subclasses **must** define:

    * ``emitter_type: str`` — unique type identifier (config key, registry key).
    * ``description: str`` — human-readable one-liner.

    Subclasses **must** implement:

    * :meth:`on_emit` — process one frame of output.
    * :meth:`capabilities` — declare accepted frame types and limits.

    Subclasses **may** override:

    * :meth:`on_configure` — deferred init (device discovery, connections).
    * :meth:`on_open` — acquire resources before first emit.
    * :meth:`on_flush` — flush buffered output.
    * :meth:`on_close` — release resources.

    Parameters are declared as class-level :class:`Param` instances.
    At runtime they become regular attributes with their current values.
    """

    emitter_type: Optional[str] = None
    description: str = ""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """Initialize with instance name and config, applying param overrides.

        Args:
            name:   Instance name (unique within the pipeline, from config).
            config: Instance-specific configuration dict.  Keys matching
                    declared :class:`Param` names override their defaults.
        """
        self.name: str = name
        self._config: dict[str, Any] = config
        self._is_open: bool = False

        # Walk the class hierarchy to collect all Param declarations,
        # mirroring the Effect.__init__ pattern exactly.
        self._param_defs: dict[str, Param] = {}
        for attr_name in dir(self.__class__):
            val = getattr(self.__class__, attr_name)
            if isinstance(val, Param):
                self._param_defs[attr_name] = val
                override = config.get(attr_name)
                if override is not None:
                    setattr(self, attr_name, val.validate(override))
                else:
                    setattr(self, attr_name, val.default)

    # --- Lifecycle (override in subclasses) --------------------------------

    def on_configure(self, config: dict[str, Any]) -> None:
        """Called once after construction with the full pipeline config.

        Use for deferred initialization that depends on external state
        (e.g., device discovery, file path expansion, connection setup).

        Args:
            config: Full server/pipeline configuration for context.
        """

    def on_open(self) -> None:
        """Called when the pipeline starts, before the first :meth:`on_emit`.

        Acquire resources here: open sockets, files, connections, GPIO pins.
        """

    def on_emit(self, frame: Any, metadata: dict[str, Any]) -> bool:
        """Process one frame of output.

        **This is the core method.**  Subclasses must implement this.

        The frame type depends on the Operator topology and is validated
        against :meth:`capabilities` at pipeline construction, not per-frame:

        * ``list[HSBK]`` — color strip (LIFX, LED tape, neon flex)
        * ``bool`` — binary (relay, valve, solenoid)
        * ``dict[str, float]`` — signal snapshot (database, CSV)
        * ``bytes`` — raw media (speaker, display framebuffer)
        * Anything else the pipeline defines.

        Args:
            frame:    The abstract frame from the Operator.
            metadata: Per-frame context dict.  Always contains:

                      * ``"t"`` (*float*) — seconds since pipeline start.
                      * ``"dt"`` (*float*) — seconds since last emit.
                      * ``"frame_number"`` (*int*) — monotonic counter.

                      May also contain:

                      * ``"signal_bus"`` — for emitters that read the bus
                        directly (loggers, persistence).
                      * ``"trigger_signal"`` / ``"trigger_value"`` — for
                        event-driven emitters.

        Returns:
            ``True`` if the frame was emitted successfully.
            ``False`` on transient failure (the manager will continue
            calling; consecutive failures are tracked for auto-disable).
        """
        raise NotImplementedError

    def on_flush(self) -> None:
        """Flush any buffered output.

        Called periodically by the EmitterManager and always called
        before :meth:`on_close`.  Override for emitters that batch
        writes (databases, file buffers, network queues).
        """

    def on_close(self) -> None:
        """Release all resources.

        Called when the pipeline stops.  The emitter must be safe to
        discard after this call.
        """

    # --- Introspection -----------------------------------------------------

    def capabilities(self) -> EmitterCapabilities:
        """Declare what this emitter accepts.

        Must return valid results after ``__init__`` (before
        :meth:`on_configure`).  Topology fields (zones, width, height)
        may be updated after :meth:`on_configure` when device discovery
        populates them.

        Returns:
            An :class:`EmitterCapabilities` describing accepted frame
            types, rate limits, and topology.
        """
        raise NotImplementedError

    def get_params(self) -> dict[str, Any]:
        """Return current parameter values as a dict."""
        return {name: getattr(self, name) for name in self._param_defs}

    def set_params(self, **kwargs: Any) -> None:
        """Update parameters at runtime (e.g., from an API call).

        Unknown parameter names are silently ignored so that callers
        can pass a superset of params safely.

        Args:
            **kwargs: Parameter names mapped to new values.
        """
        for name, value in kwargs.items():
            if name in self._param_defs:
                setattr(self, name, self._param_defs[name].validate(value))

    @classmethod
    def get_param_defs(cls) -> dict[str, Param]:
        """Return parameter definitions as ``{name: Param}``.

        Returns:
            Dict mapping parameter names to their :class:`Param`
            declarations.
        """
        defs: dict[str, Param] = {}
        for attr_name in dir(cls):
            val = getattr(cls, attr_name)
            if isinstance(val, Param):
                defs[attr_name] = val
        return defs

    def get_status(self) -> dict[str, Any]:
        """Return JSON-serializable status for API responses.

        Returns:
            Dict with emitter identity, state, params, and capabilities.
        """
        return {
            "name": self.name,
            "type": self.emitter_type,
            "description": self.description,
            "open": self._is_open,
            "params": self.get_params(),
            "capabilities": self.capabilities().to_dict(),
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def get_registry() -> dict[str, type[Emitter]]:
    """Return a copy of the emitter registry ``{emitter_type: Emitter class}``.

    Returns:
        Dict mapping registered emitter type strings to their classes.
    """
    return dict(_registry)


def get_emitter_types() -> list[str]:
    """Return a sorted list of registered emitter type strings.

    Returns:
        Sorted list of available emitter type identifiers.
    """
    return sorted(_registry.keys())


def create_emitter(emitter_type: str, name: str,
                   config: dict[str, Any]) -> Emitter:
    """Instantiate an emitter by type with a name and config.

    Args:
        emitter_type: Registered emitter type (e.g., ``"lifx"``).
        name:         Instance name (e.g., ``"porch"``).
        config:       Instance-specific configuration dict.

    Returns:
        A fully-initialized :class:`Emitter` instance.

    Raises:
        ValueError: If *emitter_type* is not in the registry.
    """
    if emitter_type not in _registry:
        available = ", ".join(get_emitter_types())
        raise ValueError(
            f"Unknown emitter type '{emitter_type}'. Available: {available}"
        )
    return _registry[emitter_type](name, config)


# ---------------------------------------------------------------------------
# _EmitterSlot — private runtime wrapper per managed emitter
# ---------------------------------------------------------------------------

@dataclass
class _EmitterSlot:
    """Runtime state for one emitter managed by :class:`EmitterManager`.

    Holds scheduling metadata, health counters, and trigger state.
    The manager reads and mutates these fields; emitters never see them.
    """

    emitter: Emitter
    timing: str                                     # TIMING_* constant
    rate_hz: float = 0.0                            # periodic: calls per second
    trigger_signal: Optional[str] = None            # event: bus signal name
    trigger_threshold: float = 0.5                  # event: fire threshold
    trigger_edge: str = EDGE_RISING                 # edge detection mode
    cooldown_seconds: float = 0.0                   # event: min gap between fires
    signals: list[str] = field(default_factory=list)  # glob patterns for snapshot
    enabled: bool = True
    consecutive_failures: int = 0
    total_emits: int = 0
    total_failures: int = 0
    last_emit_time: float = 0.0                     # monotonic
    last_trigger_value: float = 0.0                 # previous trigger reading
    last_trigger_fire: float = 0.0                  # monotonic, for cooldown
    last_flush_time: float = 0.0                    # monotonic


# ---------------------------------------------------------------------------
# EmitterManager — lifecycle and dispatch orchestrator
# ---------------------------------------------------------------------------

class EmitterManager:
    """Lifecycle manager and dispatch layer for all emitters.

    Owns every :class:`Emitter` instance in the pipeline.  Handles three
    timing modes:

    * **continuous** — dispatched from the Engine's send thread via
      :meth:`emit_frame`, once per rendered frame.
    * **periodic** — dispatched by the manager's own daemon thread at
      a configured rate, with a signal-bus snapshot as the frame.
    * **event** — dispatched by the daemon thread when a trigger signal
      crosses a threshold (edge-detected, with cooldown).

    Thread safety: all public methods acquire ``_lock`` for state access,
    then release it before calling into emitter methods (which may do I/O).
    """

    def __init__(self) -> None:
        """Initialize an empty emitter manager."""
        self._slots: dict[str, _EmitterSlot] = {}
        self._lock: threading.Lock = threading.Lock()
        self._signal_bus: Optional[Any] = None
        self._periodic_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()
        self._configured: bool = False
        self._frame_number: int = 0
        self._start_time: float = 0.0

    # --- Configuration -----------------------------------------------------

    def configure(self, config: dict[str, Any],
                  signal_bus: Optional[Any] = None) -> None:
        """Parse the ``emitters`` config section and create instances.

        Each entry in ``config["emitters"]`` maps an instance name to a
        dict with at least a ``"type"`` key.  Manager-level keys
        (``timing``, ``rate_hz``, ``trigger_*``, ``signals``) are
        consumed by the slot; the rest is passed to the emitter
        constructor.

        Individual emitter failures are logged but do not abort the
        remaining configuration (mirrors :class:`MediaManager`).

        Args:
            config:     Full server configuration dict (parsed from
                        ``server.json``).
            signal_bus: Optional :class:`SignalBus` reference for
                        periodic/event emitters to read.
        """
        self._signal_bus = signal_bus
        emitter_cfg: dict[str, Any] = config.get("emitters", {})

        for name, entry in emitter_cfg.items():
            try:
                emitter_type: str = entry.get("type", "")
                if not emitter_type:
                    logger.error(
                        "Emitter '%s' missing 'type' in config", name)
                    continue

                # Separate manager keys from emitter-specific config.
                emitter_config: dict[str, Any] = {
                    k: v for k, v in entry.items() if k not in _MANAGER_KEYS
                }

                # Validate timing mode.
                timing: str = entry.get("timing", TIMING_CONTINUOUS)
                if timing not in VALID_TIMING_MODES:
                    logger.error(
                        "Emitter '%s' has invalid timing '%s' "
                        "(expected one of %s)",
                        name, timing, ", ".join(sorted(VALID_TIMING_MODES)),
                    )
                    continue

                # Validate edge mode for event emitters.
                edge: str = entry.get("trigger_edge", EDGE_RISING)
                if edge not in VALID_EDGE_MODES:
                    logger.error(
                        "Emitter '%s' has invalid trigger_edge '%s' "
                        "(expected one of %s)",
                        name, edge, ", ".join(sorted(VALID_EDGE_MODES)),
                    )
                    continue

                # Create the emitter instance.
                emitter: Emitter = create_emitter(emitter_type, name,
                                                  emitter_config)

                # Deferred init with full config context.
                emitter.on_configure(config)

                # Build the slot with manager-level scheduling metadata.
                slot = _EmitterSlot(
                    emitter=emitter,
                    timing=timing,
                    rate_hz=float(entry.get("rate_hz", 0.0)),
                    trigger_signal=entry.get("trigger_signal"),
                    trigger_threshold=float(
                        entry.get("trigger_threshold", 0.5)),
                    trigger_edge=edge,
                    cooldown_seconds=float(
                        entry.get("cooldown_seconds", 0.0)),
                    signals=list(entry.get("signals", [])),
                )

                with self._lock:
                    self._slots[name] = slot

                logger.info(
                    "Configured emitter: %s (type=%s, timing=%s)",
                    name, emitter_type, timing,
                )

            except Exception as exc:
                logger.error(
                    "Failed to configure emitter '%s': %s", name, exc)

        self._configured = True

    # --- Lifecycle ----------------------------------------------------------

    def open_all(self) -> None:
        """Open all configured emitters and start the periodic thread.

        Called once during server startup, after :meth:`configure`.
        Calls :meth:`Emitter.on_open` on each instance (outside the
        lock).  Starts the daemon thread that services periodic and
        event-driven emitters.
        """
        self._start_time = time.monotonic()

        # Snapshot names under lock, open outside lock.
        with self._lock:
            names: list[str] = list(self._slots.keys())

        for name in names:
            slot: Optional[_EmitterSlot] = self._slots.get(name)
            if slot is None:
                continue
            try:
                slot.emitter.on_open()
                slot.emitter._is_open = True
                logger.info("Opened emitter: %s", name)
            except Exception as exc:
                logger.error(
                    "Failed to open emitter '%s': %s", name, exc)
                slot.enabled = False

        # Start periodic/event thread if any non-continuous emitters exist.
        has_periodic_or_event: bool = any(
            s.timing in (TIMING_PERIODIC, TIMING_EVENT)
            for s in self._slots.values()
        )
        if has_periodic_or_event:
            self._stop_event.clear()
            self._periodic_thread = threading.Thread(
                target=self._periodic_loop,
                name="glowup-emitter-dispatch",
                daemon=True,
            )
            self._periodic_thread.start()
            logger.info("Emitter periodic/event thread started")

    def emit_frame(self, frame: Any, t: float, dt: float) -> None:
        """Dispatch a rendered frame to all continuous emitters.

        Called by the Engine's send thread on every frame.  Must return
        quickly — continuous emitters must not do blocking I/O.

        Args:
            frame: The rendered frame from the Operator (e.g.,
                   ``list[HSBK]`` for light effects).
            t:     Seconds elapsed since pipeline start.
            dt:    Seconds since the previous frame.
        """
        self._frame_number += 1
        metadata: dict[str, Any] = {
            META_KEY_TIME: t,
            META_KEY_DELTA: dt,
            META_KEY_FRAME_NUMBER: self._frame_number,
        }

        # Snapshot continuous slots under lock.
        with self._lock:
            continuous: list[_EmitterSlot] = [
                s for s in self._slots.values()
                if s.enabled and s.timing == TIMING_CONTINUOUS
            ]

        for slot in continuous:
            self._dispatch(slot, frame, metadata)

    def flush_all(self) -> None:
        """Flush all open emitters.

        Safe to call from any thread.  Called periodically by the
        daemon thread and always called during :meth:`shutdown`.
        """
        with self._lock:
            slots: list[_EmitterSlot] = list(self._slots.values())

        for slot in slots:
            if slot.emitter._is_open:
                try:
                    slot.emitter.on_flush()
                except Exception as exc:
                    logger.warning(
                        "Flush failed for emitter '%s': %s",
                        slot.emitter.name, exc,
                    )

    def shutdown(self) -> None:
        """Stop the periodic thread, flush and close all emitters.

        Called during server shutdown.  Blocks until the periodic thread
        exits (with a timeout) and all emitters are closed.
        """
        # Signal the periodic thread to stop.
        self._stop_event.set()
        if self._periodic_thread is not None:
            self._periodic_thread.join(timeout=5.0)
            self._periodic_thread = None

        # Flush then close each emitter (outside lock).
        with self._lock:
            names: list[str] = list(self._slots.keys())

        for name in names:
            slot: Optional[_EmitterSlot] = self._slots.get(name)
            if slot is None or not slot.emitter._is_open:
                continue
            try:
                slot.emitter.on_flush()
            except Exception:
                pass
            try:
                slot.emitter.on_close()
                slot.emitter._is_open = False
            except Exception as exc:
                logger.error(
                    "Error closing emitter '%s': %s", name, exc)

        logger.info("EmitterManager shut down")

    # --- Introspection / control -------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return JSON-serializable status of all managed emitters.

        Returns:
            Dict with an ``"emitters"`` list and summary counts.
        """
        with self._lock:
            emitters: list[dict[str, Any]] = []
            for name in sorted(self._slots.keys()):
                slot: _EmitterSlot = self._slots[name]
                emitters.append({
                    "name": name,
                    "type": slot.emitter.emitter_type,
                    "timing": slot.timing,
                    "enabled": slot.enabled,
                    "open": slot.emitter._is_open,
                    "total_emits": slot.total_emits,
                    "total_failures": slot.total_failures,
                    "consecutive_failures": slot.consecutive_failures,
                })
            return {
                "emitters": emitters,
                "configured": self._configured,
                "frame_number": self._frame_number,
            }

    def get_emitter(self, name: str) -> Optional[Emitter]:
        """Return an emitter instance by name, or ``None`` if not found.

        Args:
            name: Instance name as declared in config.

        Returns:
            The :class:`Emitter` instance, or ``None``.
        """
        with self._lock:
            slot: Optional[_EmitterSlot] = self._slots.get(name)
            return slot.emitter if slot is not None else None

    def enable(self, name: str) -> bool:
        """Re-enable a disabled emitter and reset its failure counter.

        Args:
            name: Instance name.

        Returns:
            ``True`` if the emitter was found and enabled.
        """
        with self._lock:
            slot: Optional[_EmitterSlot] = self._slots.get(name)
            if slot is None:
                return False
            slot.enabled = True
            slot.consecutive_failures = 0
            logger.info("Emitter '%s' re-enabled", name)
            return True

    def disable(self, name: str) -> bool:
        """Manually disable an emitter.

        Args:
            name: Instance name.

        Returns:
            ``True`` if the emitter was found and disabled.
        """
        with self._lock:
            slot: Optional[_EmitterSlot] = self._slots.get(name)
            if slot is None:
                return False
            slot.enabled = False
            logger.info("Emitter '%s' disabled", name)
            return True

    # --- Private dispatch helpers ------------------------------------------

    def _dispatch(self, slot: _EmitterSlot, frame: Any,
                  metadata: dict[str, Any]) -> None:
        """Call on_emit() and track the result.

        Auto-disables the emitter after :data:`MAX_CONSECUTIVE_FAILURES`
        consecutive failures.

        Args:
            slot:     The emitter slot to dispatch to.
            frame:    The frame data to emit.
            metadata: Per-frame context dict.
        """
        try:
            success: bool = slot.emitter.on_emit(frame, metadata)
        except Exception:
            success = False
            logger.warning(
                "Emitter '%s' raised exception in on_emit",
                slot.emitter.name, exc_info=True,
            )

        slot.total_emits += 1
        if success:
            slot.consecutive_failures = 0
        else:
            slot.consecutive_failures += 1
            slot.total_failures += 1
            if slot.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                slot.enabled = False
                logger.error(
                    "Emitter '%s' auto-disabled after %d consecutive "
                    "failures",
                    slot.emitter.name, MAX_CONSECUTIVE_FAILURES,
                )
        slot.last_emit_time = time.monotonic()

    def _read_bus_snapshot(self, slot: _EmitterSlot) -> dict[str, Any]:
        """Read signal values from the bus for this emitter's signal list.

        Resolves glob patterns (e.g., ``"foyer:audio:*"``) against the
        current signal names on the bus.

        Args:
            slot: The emitter slot whose ``signals`` list to read.

        Returns:
            Dict mapping signal name to current value.  Empty if the
            bus is not available or no patterns match.
        """
        if self._signal_bus is None:
            return {}

        snapshot: dict[str, Any] = {}
        all_names: list[str] = self._signal_bus.signal_names()

        for pattern in slot.signals:
            # If the pattern contains glob characters, expand it.
            if any(c in pattern for c in ("*", "?", "[")):
                for sig_name in all_names:
                    if fnmatch.fnmatch(sig_name, pattern):
                        snapshot[sig_name] = self._signal_bus.read(
                            sig_name, 0.0)
            else:
                # Literal signal name — read directly.
                snapshot[pattern] = self._signal_bus.read(pattern, 0.0)

        return snapshot

    def _evaluate_trigger(self, slot: _EmitterSlot,
                          now: float) -> None:
        """Check an event-driven emitter's trigger condition and fire.

        Reads the trigger signal from the bus, applies edge detection
        and cooldown, and dispatches if the condition is met.

        Args:
            slot: The event-driven emitter slot.
            now:  Current monotonic time.
        """
        if self._signal_bus is None or slot.trigger_signal is None:
            return

        value: float = float(
            self._signal_bus.read(slot.trigger_signal, 0.0))
        prev: float = slot.last_trigger_value
        threshold: float = slot.trigger_threshold

        # Edge detection.
        fired: bool = False
        if slot.trigger_edge == EDGE_RISING:
            fired = prev < threshold <= value
        elif slot.trigger_edge == EDGE_FALLING:
            fired = prev >= threshold > value
        elif slot.trigger_edge == EDGE_ANY:
            crossed_up: bool = prev < threshold <= value
            crossed_down: bool = prev >= threshold > value
            fired = crossed_up or crossed_down

        slot.last_trigger_value = value

        if not fired:
            return

        # Cooldown check.
        if slot.cooldown_seconds > 0.0:
            if now - slot.last_trigger_fire < slot.cooldown_seconds:
                return

        # Build frame from bus snapshot and dispatch.
        frame: dict[str, Any] = self._read_bus_snapshot(slot)
        metadata: dict[str, Any] = self._build_metadata(now)
        metadata["trigger_signal"] = slot.trigger_signal
        metadata["trigger_value"] = value

        self._dispatch(slot, frame, metadata)
        slot.last_trigger_fire = now

    def _build_metadata(self, now: float) -> dict[str, Any]:
        """Build a standard metadata dict for periodic/event dispatch.

        Args:
            now: Current monotonic time.

        Returns:
            Metadata dict with time, delta, frame number, and bus ref.
        """
        self._frame_number += 1
        t: float = now - self._start_time
        return {
            META_KEY_TIME: t,
            META_KEY_DELTA: 0.0,  # No meaningful delta for polled dispatch.
            META_KEY_FRAME_NUMBER: self._frame_number,
            "signal_bus": self._signal_bus,
        }

    def _periodic_loop(self) -> None:
        """Daemon thread servicing periodic and event-driven emitters.

        Wakes at :data:`PERIODIC_POLL_HZ`, checks each periodic emitter's
        rate and each event emitter's trigger condition, and dispatches
        as appropriate.  Also calls :meth:`Emitter.on_flush` at
        :data:`FLUSH_INTERVAL`.
        """
        last_flush: float = time.monotonic()

        while not self._stop_event.is_set():
            now: float = time.monotonic()

            # Snapshot periodic/event slots under lock.
            with self._lock:
                slots: list[tuple[str, _EmitterSlot]] = [
                    (n, s) for n, s in self._slots.items()
                    if s.enabled and s.timing in (
                        TIMING_PERIODIC, TIMING_EVENT)
                ]

            for name, slot in slots:
                if slot.timing == TIMING_PERIODIC:
                    interval: float = (
                        1.0 / slot.rate_hz if slot.rate_hz > 0.0 else 1.0
                    )
                    if now - slot.last_emit_time >= interval:
                        frame: dict[str, Any] = self._read_bus_snapshot(
                            slot)
                        metadata: dict[str, Any] = self._build_metadata(
                            now)
                        self._dispatch(slot, frame, metadata)

                elif slot.timing == TIMING_EVENT:
                    self._evaluate_trigger(slot, now)

            # Periodic flush for all open emitters.
            if now - last_flush >= FLUSH_INTERVAL:
                self.flush_all()
                last_flush = now

            self._stop_event.wait(PERIODIC_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Auto-import all emitter modules so they self-register.
# New emitters just need a .py file in this directory.
# ---------------------------------------------------------------------------
import importlib  # noqa: E402
import os         # noqa: E402
import pkgutil    # noqa: E402

_pkg_dir: str = os.path.dirname(__file__)
for _importer, _modname, _ispkg in pkgutil.iter_modules([_pkg_dir]):
    try:
        importlib.import_module(f".{_modname}", __package__)
    except ImportError as _exc:
        # Optional-dependency emitters (e.g. audio_out needs sounddevice/numpy)
        # are silently skipped on machines that lack those packages.
        logging.getLogger(__name__).debug(
            "Skipping emitter module %s: %s", _modname, _exc,
        )
