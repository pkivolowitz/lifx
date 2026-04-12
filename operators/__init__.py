"""Operator framework for the SOE (Sensors -> Operators -> Emitters) pipeline.

Operators are the transform/compute layer of the pipeline.  They read signals
from the :class:`~media.SignalBus`, apply computations (threshold, gate,
combine, derive), and write derived signals back.  An FFT, a Kalman filter,
an occupancy derivation from lock state, and an HSBK-rendering Effect are all
operators.

The Operator ABC is the **unified base** for every transform node — including
:class:`~effects.Effect`, which extends it with HSBK rendering.  This
unification reflects the original Loaders -> Operators -> Savers insight:
a sensor reading and a user-set parameter are both just signals on the bus.
An operator's :meth:`read` does not distinguish source.

To create a new operator:
    1. Create a file in ``operators/`` (e.g., ``operators/occupancy.py``).
    2. Subclass :class:`Operator`.
    3. Set ``operator_type`` to a unique identifier string.
    4. Declare ``input_signals`` and ``output_signals``.
    5. Implement :meth:`on_signal` and/or :meth:`on_tick`.

The operator is automatically registered and available by type.

Example::

    class OccupancyOperator(Operator):
        operator_type = "occupancy"
        description = "Derive HOME/AWAY from aggregate lock state"

        input_signals = ["*:*:lock_state"]
        output_signals = ["house:occupancy:state"]

        away_confirm_seconds = Param(120.0, min=30.0, max=600.0,
                                     description="Seconds before AWAY")

        def on_signal(self, name: str, value: float) -> None:
            if value == 0.0:  # unlocked
                self.write("house:occupancy:state", 1.0)  # HOME

        def on_tick(self, dt: float) -> None:
            # Check debounce timer for AWAY transition.
            ...

Tick modes (set via ``tick_mode`` class attribute or config):

    * ``"reactive"``   — :meth:`on_signal` fires on subscribed input changes.
    * ``"periodic"``   — :meth:`on_tick` fires at ``tick_hz`` rate.
    * ``"both"``       — reactive + periodic.
    * ``"engine"``     — Effects only; the Engine's send loop drives rendering.
      OperatorManager skips these.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import fnmatch
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Union

# Param lives in param.py — shared by effects, emitters, and operators.
from param import Param

logger: logging.Logger = logging.getLogger("glowup.operators")

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# A signal value: scalar float or list of floats (matching SignalBus).
SignalValue = Union[float, list[float]]

# A single binding specification: source signal → target param.
BindingSpec = dict[str, Any]  # {"signal": str, "scale": [lo, hi], "reduce": str}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid tick modes.
TICK_REACTIVE: str = "reactive"
TICK_PERIODIC: str = "periodic"
TICK_BOTH: str = "both"
TICK_ENGINE: str = "engine"
VALID_TICK_MODES: frozenset[str] = frozenset({
    TICK_REACTIVE, TICK_PERIODIC, TICK_BOTH, TICK_ENGINE,
})

# Default tick rate for periodic operators (Hz).
DEFAULT_TICK_HZ: float = 1.0

# Maximum consecutive on_signal/on_tick failures before auto-disabling
# an operator.  Mirrors EmitterManager's MAX_CONSECUTIVE_FAILURES.
MAX_CONSECUTIVE_FAILURES: int = 10

# Poll rate for the OperatorManager tick thread (Hz).  Sets the
# granularity of periodic dispatch — individual operators can run
# slower but not faster than this.
TICK_POLL_HZ: float = 50.0

# Derived poll interval (seconds).
TICK_POLL_INTERVAL: float = 1.0 / TICK_POLL_HZ

# Floor for operator tick_hz to prevent division by zero.
MIN_TICK_HZ: float = 0.01

# ---------------------------------------------------------------------------
# Binding resolution — shared by OperatorManager and Engine
# ---------------------------------------------------------------------------


def resolve_binding(
    source_value: SignalValue,
    param_def: Optional[Param],
    binding: BindingSpec,
) -> float:
    """Reduce, scale, and validate a bound signal value for a param.

    Args:
        source_value: Raw value read from the source signal on the bus.
        param_def:    The target Param definition (for range info).
        binding:      Binding spec with optional ``scale`` and ``reduce``.

    Returns:
        Scalar float suitable for ``setattr`` on the target operator.
    """
    value: float
    # Reduce array signals to scalar.
    if isinstance(source_value, list):
        reduce_fn: str = binding.get("reduce", "max")
        if not source_value:
            value = 0.0
        elif reduce_fn == "mean":
            value = sum(source_value) / len(source_value)
        elif reduce_fn == "sum":
            value = min(1.0, sum(source_value))
        else:  # "max" or unknown
            value = max(source_value)
    else:
        value = float(source_value)

    # Scale normalised [0, 1] to target range.
    scale = binding.get("scale")
    if scale and len(scale) >= 2:
        lo, hi = float(scale[0]), float(scale[1])
    elif param_def and param_def.min is not None:
        lo, hi = float(param_def.min), float(param_def.max)
    else:
        lo, hi = 0.0, 1.0
    return lo + value * (hi - lo)


def check_circular_binding(
    target: str,
    source: str,
    all_bindings: dict[str, str],
) -> bool:
    """Return True if adding target←source would create a cycle.

    Walks the binding chain from *source* — if it reaches *target*,
    the binding is circular.

    Args:
        target:       Signal name of the target param (e.g. ``"cylon:speed"``).
        source:       Signal name of the proposed source.
        all_bindings: Dict mapping ``target_signal → source_signal`` for
                      every active binding across all operators.

    Returns:
        ``True`` if a cycle would result.
    """
    visited: set[str] = {target}
    cursor: str = source
    while cursor in all_bindings:
        if cursor in visited:
            return True
        visited.add(cursor)
        cursor = all_bindings[cursor]
    return cursor in visited


# ---------------------------------------------------------------------------
# Operator registry
# ---------------------------------------------------------------------------

# Global registry mapping operator_type -> Operator subclass.
_registry: dict[str, type["Operator"]] = {}


def get_registry() -> dict[str, type["Operator"]]:
    """Return a copy of the operator registry.

    Returns:
        Dict mapping registered operator type strings to their classes.
    """
    return dict(_registry)


def get_operator_types() -> list[str]:
    """Return a sorted list of registered operator type strings.

    Returns:
        Sorted list of available operator type identifiers.
    """
    return sorted(_registry.keys())


def create_operator(
    operator_type: str,
    name: str,
    config: dict[str, Any],
    bus: Any,
) -> "Operator":
    """Instantiate an operator by type.

    Args:
        operator_type: Registered type (e.g., ``"occupancy"``).
        name:          Instance name (e.g., ``"house_occupancy"``).
        config:        Instance-specific configuration dict.
        bus:           The :class:`~media.SignalBus` instance.

    Returns:
        A fully-initialized :class:`Operator` instance.

    Raises:
        ValueError: If *operator_type* is not in the registry.
    """
    if operator_type not in _registry:
        available: str = ", ".join(get_operator_types())
        raise ValueError(
            f"Unknown operator type '{operator_type}'. Available: {available}"
        )
    return _registry[operator_type](name, config, bus)


# ---------------------------------------------------------------------------
# Operator ABC
# ---------------------------------------------------------------------------

class Operator:
    """Abstract base class for all transform/compute nodes in the SOE pipeline.

    This is the unified base for non-rendering operators (occupancy,
    motion gate, battery watch, future TriggerOperator) **and** for
    :class:`~effects.Effect` which extends it with HSBK rendering.

    Subclasses **must** define:

    * ``operator_type: str`` — unique type identifier (registry key).
    * ``description: str`` — human-readable one-liner.
    * ``input_signals: list[str]`` — signal name patterns to subscribe to
      (supports fnmatch wildcards, e.g. ``"*:*:lock_state"``).
    * ``output_signals: list[str]`` — signal names this operator writes.

    Subclasses **must** implement at least one of:

    * :meth:`on_signal` — reactive processing on input change.
    * :meth:`on_tick` — periodic processing (debounce, decay, time-window).

    Subclasses **may** override:

    * :meth:`on_configure` — deferred init after construction.
    * :meth:`on_start` — acquire resources, start background work.
    * :meth:`on_stop` — release resources.

    Parameters are declared as class-level :class:`~effects.Param` instances,
    following the same pattern as Effects and Emitters.  At runtime they
    become regular attributes with their current values.

    Registration uses ``__init_subclass__`` (not a metaclass) to avoid
    conflicts with :class:`~effects.EffectMeta` when Effect inherits
    from Operator.
    """

    operator_type: Optional[str] = None
    description: str = ""

    # Signal declarations — subclasses override these.
    input_signals: list[str] = []
    output_signals: list[str] = []

    # Dependency declaration — operator types that must be configured and
    # dispatched before this one.  OperatorManager topologically sorts
    # operators so dependencies are always evaluated first.
    depends_on: list[str] = []

    # Tick mode — how the OperatorManager dispatches this operator.
    tick_mode: str = TICK_REACTIVE

    # Tick rate for periodic operators (Hz).
    tick_hz: float = DEFAULT_TICK_HZ

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-register concrete Operator subclasses.

        Any subclass with a non-None ``operator_type`` is added to the
        global registry.  This fires for Effect subclasses too, but
        since they leave ``operator_type = None`` they are not registered
        as operators (they register via :class:`~effects.EffectMeta`
        in the effect registry instead).

        Validates tick_mode and warns on type collisions.
        """
        super().__init_subclass__(**kwargs)

        # Validate tick_mode if explicitly set.
        mode: str = getattr(cls, "tick_mode", TICK_REACTIVE)
        if mode not in VALID_TICK_MODES:
            logger.warning(
                "Operator %s has invalid tick_mode '%s' "
                "(valid: %s) — defaulting to '%s'",
                cls.__name__, mode,
                ", ".join(sorted(VALID_TICK_MODES)),
                TICK_REACTIVE,
            )
            cls.tick_mode = TICK_REACTIVE

        otype: Optional[str] = getattr(cls, "operator_type", None)
        if otype is not None:
            # Warn on type collision — second registration overwrites.
            if otype in _registry:
                prev: type = _registry[otype]
                if prev is not cls:
                    logger.warning(
                        "Operator type '%s' collision: %s overwrites %s",
                        otype, cls.__name__, prev.__name__,
                    )
            _registry[otype] = cls
            logger.debug("Registered operator type: %s -> %s", otype, cls.__name__)

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        """Initialize with instance name, config, and signal bus.

        Args:
            name:   Instance name (unique within the pipeline).
            config: Instance-specific configuration dict.  Keys matching
                    declared :class:`Param` names override their defaults.
            bus:    The :class:`~media.SignalBus` for read/write access.
        """
        self.name: str = name
        self._config: dict[str, Any] = config
        self._bus: Any = bus
        self._is_started: bool = False

        # Apply Param overrides from config (same pattern as Emitter).
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

        # Param-as-signal bindings — loaded from config ``"bindings"`` key.
        # Each entry maps a param name to a BindingSpec:
        #   {"signal": "source:signal", "scale": [lo, hi], "reduce": "max"}
        self._bindings: dict[str, BindingSpec] = dict(
            config.get("bindings", {}),
        )

        # Allow config to override tick_mode and tick_hz.
        if "tick_mode" in config:
            mode: str = config["tick_mode"]
            if mode in VALID_TICK_MODES:
                self.tick_mode = mode
        if "tick_hz" in config:
            try:
                self.tick_hz = float(config["tick_hz"])
            except (ValueError, TypeError):
                pass  # Keep class default.

    # --- Lifecycle (override in subclasses) --------------------------------

    def on_configure(self, config: dict[str, Any]) -> None:
        """Called once after construction with the full pipeline config.

        Use for deferred initialization that depends on external state
        (e.g., database connections, device discovery).

        Args:
            config: Full server/pipeline configuration for context.
        """

    def on_start(self) -> None:
        """Called when the pipeline starts.

        Acquire resources, start background threads, open connections.
        """

    def on_signal(self, name: str, value: SignalValue) -> None:
        """Called when a subscribed input signal changes.

        Override this for reactive processing.  The OperatorManager
        calls this whenever a signal matching ``input_signals`` patterns
        is written to the bus.

        Args:
            name:  The full signal name that changed.
            value: The new signal value.
        """

    def on_tick(self, dt: float) -> None:
        """Called periodically at the configured tick rate.

        Override this for time-based processing: debounce timers,
        decay functions, watchdog timeouts, sliding windows.

        Args:
            dt: Seconds elapsed since the last tick.
        """

    def on_stop(self) -> None:
        """Called when the pipeline stops.

        Release all resources.  The operator must be safe to discard
        after this call.
        """

    # --- Bus access --------------------------------------------------------

    def read(self, signal: str, default: SignalValue = 0.0) -> SignalValue:
        """Read a signal value from the bus.

        This is the universal data access point.  Sensor readings,
        user-set parameters, and operator-derived signals are all
        read the same way.

        Args:
            signal:  Signal name (e.g., ``"vivint:front_door:lock_state"``).
            default: Value returned if the signal does not exist.

        Returns:
            The current signal value, or *default*.
        """
        return self._bus.read(signal, default)

    def write(self, signal: str, value: SignalValue) -> None:
        """Write a derived signal to the bus.

        Args:
            signal: Signal name (e.g., ``"house:occupancy:state"``).
            value:  The computed value.
        """
        self._bus.write(signal, value)

    # --- Signal matching ---------------------------------------------------

    def matches_signal(self, signal_name: str) -> bool:
        """Test whether a signal name matches any input pattern.

        Uses :func:`fnmatch.fnmatch` for wildcard matching, consistent
        with the EmitterManager's signal pattern matching.

        Args:
            signal_name: The signal name to test.

        Returns:
            ``True`` if any ``input_signals`` pattern matches.
        """
        for pattern in self.input_signals:
            if fnmatch.fnmatch(signal_name, pattern):
                return True
        return False

    # --- Introspection -----------------------------------------------------

    def get_params(self) -> dict[str, Any]:
        """Return current parameter values as a dict.

        Returns:
            Dict mapping parameter names to their current values.
        """
        return {name: getattr(self, name) for name in self._param_defs}

    def set_params(self, **kwargs: Any) -> None:
        """Update parameters at runtime.

        Unknown parameter names are silently ignored.  Also writes the
        new value to the signal bus so bound consumers see it.

        Args:
            **kwargs: Parameter names mapped to new values.
        """
        for name, value in kwargs.items():
            if name in self._param_defs:
                validated = self._param_defs[name].validate(value)
                setattr(self, name, validated)
                # Publish to bus so other operators can bind to this param.
                if self._bus and isinstance(validated, (int, float)):
                    self._bus.write(f"{self.name}:{name}", float(validated))

    # --- Param-as-signal registration ------------------------------------

    def register_param_signals(self) -> None:
        """Write all numeric params to the bus as ``{name}:{param}`` signals.

        Called by :class:`OperatorManager` after ``on_start()``.  Seeds
        the bus with current param values so other operators can bind to
        them immediately.
        """
        if not self._bus:
            return
        for pname, pdef in self._param_defs.items():
            value = getattr(self, pname, pdef.default)
            if isinstance(value, (int, float)):
                signal_name: str = f"{self.name}:{pname}"
                self._bus.write(signal_name, float(value))

    # --- Binding management ----------------------------------------------

    def add_binding(self, param_name: str, spec: BindingSpec) -> None:
        """Add or replace a binding for a param.

        Args:
            param_name: Param name on this operator.
            spec:       Binding spec with at least ``"signal"`` key.

        Raises:
            ValueError: If *param_name* is not a declared Param.
        """
        if param_name not in self._param_defs:
            raise ValueError(
                f"'{param_name}' is not a declared param on operator "
                f"'{self.name}' (available: {list(self._param_defs)})"
            )
        self._bindings[param_name] = dict(spec)
        logger.info(
            "Binding added: %s:%s <- %s",
            self.name, param_name, spec.get("signal", "?"),
        )

    def remove_binding(self, param_name: str) -> None:
        """Remove a binding for a param.  Param keeps its last value.

        Args:
            param_name: Param name to unbind.
        """
        removed: Optional[BindingSpec] = self._bindings.pop(param_name, None)
        if removed:
            logger.info(
                "Binding removed: %s:%s (was <- %s)",
                self.name, param_name, removed.get("signal", "?"),
            )

    def get_bindings(self) -> dict[str, BindingSpec]:
        """Return a copy of all active bindings on this operator.

        Returns:
            Dict mapping param names to their binding specs.
        """
        return dict(self._bindings)

    def resolve_bindings(self) -> None:
        """Read bound source signals and apply to params.

        Called by :class:`OperatorManager` tick loop before
        ``on_tick()``.  Binding wins over manual ``set_params()`` —
        the bound value is reapplied every tick.  Missing source
        signals leave the param unchanged.
        """
        if not self._bus or not self._bindings:
            return
        for param_name, spec in self._bindings.items():
            source: str = spec.get("signal", "")
            if not source:
                continue
            source_value = self._bus.read(source, None)
            if source_value is None:
                continue  # Source not yet on bus — keep current value.
            pdef: Optional[Param] = self._param_defs.get(param_name)
            scaled: float = resolve_binding(source_value, pdef, spec)
            # Write to bus (so downstream bindings chain).
            param_signal: str = f"{self.name}:{param_name}"
            self._bus.write(param_signal, scaled)
            # Set the attribute.
            if pdef:
                setattr(self, param_name, pdef.validate(scaled))
            else:
                setattr(self, param_name, scaled)

    def get_status(self) -> dict[str, Any]:
        """Return JSON-serializable status for API responses.

        Returns:
            Dict with operator identity, state, params, signal info,
            and active bindings.
        """
        return {
            "name": self.name,
            "type": self.operator_type,
            "description": self.description,
            "started": self._is_started,
            "tick_mode": self.tick_mode,
            "tick_hz": self.tick_hz,
            "input_signals": list(self.input_signals),
            "output_signals": list(self.output_signals),
            "params": self.get_params(),
            "bindings": self.get_bindings(),
        }


# ---------------------------------------------------------------------------
# _OperatorSlot — private runtime wrapper per managed operator
# ---------------------------------------------------------------------------

@dataclass
class _OperatorSlot:
    """Runtime state for a single managed operator instance.

    Attributes:
        operator:              The :class:`Operator` instance.
        tick_interval:         Seconds between periodic ticks (1 / tick_hz).
        last_tick:             Timestamp of the last on_tick() call.
        consecutive_failures:  Failure count for auto-disable.
        enabled:               Whether the operator is active.
    """

    operator: Operator
    tick_interval: float = 1.0
    last_tick: float = 0.0
    consecutive_failures: int = 0
    enabled: bool = True


# ---------------------------------------------------------------------------
# OperatorManager — lifecycle and dispatch
# ---------------------------------------------------------------------------

class OperatorManager:
    """Manages operator lifecycles and dispatches signals and ticks.

    Parallels :class:`~emitters.EmitterManager` in design.  Instantiates
    operators from config, routes signal changes to reactive operators,
    and runs a tick thread for periodic operators.

    Args:
        bus: The :class:`~media.SignalBus` instance shared with sensors
             and emitters.
    """

    def __init__(self, bus: Any) -> None:
        """Initialize with a signal bus reference.

        Args:
            bus: The shared :class:`~media.SignalBus`.
        """
        self._bus: Any = bus
        self._slots: list[_OperatorSlot] = []
        self._lock: threading.Lock = threading.Lock()
        self._running: bool = False
        self._tick_thread: Optional[threading.Thread] = None

        # Signal change callback — wired into the bus after configure.
        self._prev_signals: dict[str, SignalValue] = {}

    def configure(self, config_list: list[dict[str, Any]]) -> None:
        """Instantiate operators from a config list.

        Each entry must have ``"type"`` and ``"name"`` keys.  Additional
        keys are passed as operator-specific config.

        Operators are topologically sorted by ``depends_on`` so that
        dependencies are always dispatched before dependents.  This
        ensures operator composition (occupancy → motion_gate → trigger)
        evaluates in the correct order regardless of config file ordering.

        Args:
            config_list: List of operator config dicts from server.json.
        """
        # Phase 1: instantiate all operators (unsorted).
        unsorted: list[_OperatorSlot] = []
        for entry in config_list:
            otype: str = entry.get("type", "")
            name: str = entry.get("name", "")
            if not otype or not name:
                logger.warning(
                    "Skipping operator config with missing type/name: %s",
                    entry,
                )
                continue
            try:
                op: Operator = create_operator(otype, name, entry, self._bus)
                tick_interval: float = 1.0 / max(op.tick_hz, MIN_TICK_HZ)
                slot = _OperatorSlot(
                    operator=op,
                    tick_interval=tick_interval,
                )
                unsorted.append(slot)
                logger.info(
                    "Configured operator: %s (%s)", name, otype,
                )
            except (ValueError, Exception) as exc:
                logger.error(
                    "Failed to create operator '%s' type '%s': %s",
                    name, otype, exc,
                )

        # Phase 2: topological sort by depends_on.
        self._slots = self._topo_sort(unsorted)

    @staticmethod
    def _topo_sort(slots: list["_OperatorSlot"]) -> list["_OperatorSlot"]:
        """Sort operator slots so dependencies come first.

        Uses Kahn's algorithm.  Operators with no dependencies come
        first; operators that depend on others come after their
        dependencies.  Ties preserve config order (stable sort).

        Cycles are detected and logged — cyclic operators are appended
        at the end rather than silently dropped.

        Args:
            slots: Unsorted list of operator slots.

        Returns:
            Sorted list with dependencies before dependents.
        """
        if not slots:
            return slots

        # Build type→slot and dependency graph.
        by_type: dict[str, list[_OperatorSlot]] = {}
        for s in slots:
            otype: str = s.operator.operator_type or ""
            by_type.setdefault(otype, []).append(s)

        # In-degree count per slot index.
        idx: dict[int, int] = {id(s): 0 for s in slots}
        # Adjacency: dependency type → list of dependent slot ids.
        dependents: dict[str, list[int]] = {}
        for s in slots:
            for dep_type in s.operator.depends_on:
                dependents.setdefault(dep_type, []).append(id(s))
                idx[id(s)] += 1

        # Kahn's algorithm.
        ready: list[_OperatorSlot] = [s for s in slots if idx[id(s)] == 0]
        result: list[_OperatorSlot] = []
        while ready:
            s = ready.pop(0)
            result.append(s)
            otype = s.operator.operator_type or ""
            for dep_id in dependents.get(otype, []):
                idx[dep_id] -= 1
                if idx[dep_id] == 0:
                    # Find the slot by id.
                    for candidate in slots:
                        if id(candidate) == dep_id:
                            ready.append(candidate)
                            break

        # Cycle detection: anything not in result has unresolved deps.
        if len(result) < len(slots):
            remaining: list[_OperatorSlot] = [
                s for s in slots if s not in result
            ]
            for s in remaining:
                logger.warning(
                    "Operator '%s' has unresolved depends_on — "
                    "appended after sort (possible cycle)",
                    s.operator.name,
                )
            result.extend(remaining)

        return result

    def start(self, full_config: Optional[dict[str, Any]] = None) -> None:
        """Start all configured operators and the tick thread.

        Args:
            full_config: Optional full server config passed to
                         :meth:`Operator.on_configure`.
        """
        if not self._slots:
            logger.info("No operators configured — manager idle")
            return

        self._running = True
        now: float = time.monotonic()

        for slot in self._slots:
            try:
                if full_config:
                    slot.operator.on_configure(full_config)
                slot.operator.on_start()
                slot.operator._is_started = True
                # Seed bus with param signals so bindings can resolve.
                slot.operator.register_param_signals()
                slot.last_tick = now
                logger.info("Started operator: %s", slot.operator.name)
            except Exception as exc:
                logger.error(
                    "Failed to start operator '%s': %s",
                    slot.operator.name, exc,
                )
                slot.enabled = False

        # Snapshot current bus state for change detection.
        self._prev_signals = dict(self._bus._signals) if hasattr(self._bus, '_signals') else {}

        # Start the tick/poll thread.
        self._tick_thread = threading.Thread(
            target=self._tick_loop,
            daemon=True,
            name="operator-tick",
        )
        self._tick_thread.start()

        logger.info(
            "OperatorManager started — %d operator(s)", len(self._slots),
        )

    def stop(self) -> None:
        """Stop all operators and the tick thread."""
        self._running = False
        if self._tick_thread:
            self._tick_thread.join(timeout=5.0)

        for slot in self._slots:
            try:
                slot.operator.on_stop()
                slot.operator._is_started = False
            except Exception as exc:
                logger.error(
                    "Error stopping operator '%s': %s",
                    slot.operator.name, exc,
                )

        logger.info("OperatorManager stopped")

    def dispatch_signal(self, name: str, value: SignalValue) -> None:
        """Route a signal change to all matching reactive operators.

        Called by the tick loop on detected bus changes, or externally
        by adapters that want immediate dispatch.

        Args:
            name:  The signal name that changed.
            value: The new value.
        """
        with self._lock:
            for slot in self._slots:
                if not slot.enabled:
                    continue
                op: Operator = slot.operator
                # Skip engine-driven operators (Effects).
                if op.tick_mode == TICK_ENGINE:
                    continue
                # Only dispatch to reactive or both-mode operators.
                if op.tick_mode not in (TICK_REACTIVE, TICK_BOTH):
                    continue
                if not op.matches_signal(name):
                    continue
                try:
                    op.on_signal(name, value)
                    slot.consecutive_failures = 0
                except Exception as exc:
                    slot.consecutive_failures += 1
                    logger.error(
                        "Operator '%s' on_signal error (%d/%d): %s",
                        op.name, slot.consecutive_failures,
                        MAX_CONSECUTIVE_FAILURES, exc,
                    )
                    if slot.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        slot.enabled = False
                        logger.error(
                            "Operator '%s' auto-disabled after %d failures",
                            op.name, MAX_CONSECUTIVE_FAILURES,
                        )
                        # Publish disable event to the bus so dashboards
                        # and monitoring operators can react.
                        self._notify_disabled(op.name)

    def notify_group_override(self, device_id: str) -> None:
        """Notify operators that a group was manually overridden.

        Called by API handlers when a user manually controls a group
        (power on/off, play, stop).  TriggerOperators watching that
        group reset their ``_active`` state so the next sensor event
        can re-fire.

        Without this, a manual power-off while a trigger is active
        leaves the trigger stuck: it thinks lights are on, the
        watchdog keeps resetting on new motion, and lights never
        come back.

        Args:
            device_id: Device identifier — may be ``"group:Living Room"``
                       or a bare IP.  Group prefix is stripped before
                       comparison.
        """
        # Strip "group:" prefix if present so comparison matches
        # TriggerOperator._group (stored without prefix).
        if device_id.startswith("group:"):
            group_name: str = device_id[len("group:"):]
        else:
            group_name = device_id

        with self._lock:
            for slot in self._slots:
                op: Operator = slot.operator
                if hasattr(op, "on_group_override"):
                    try:
                        op.on_group_override(group_name)
                    except Exception as exc:
                        logger.debug(
                            "on_group_override error in '%s': %s",
                            op.name, exc,
                        )

    def _notify_disabled(self, operator_name: str) -> None:
        """Write a disable notification signal to the bus.

        Signal: ``system:operator_disabled`` with operator name as value.
        This allows dashboards and monitoring operators to detect
        failures without polling the status API.

        Args:
            operator_name: The name of the disabled operator.
        """
        try:
            self._bus.write(
                "system:operator_disabled",
                operator_name,
            )
        except Exception:
            pass  # Bus might not accept string values; best-effort.

    def get_status(self) -> list[dict[str, Any]]:
        """Return status for all managed operators.

        Returns:
            List of status dicts, one per operator.
        """
        result: list[dict[str, Any]] = []
        with self._lock:
            for slot in self._slots:
                status: dict[str, Any] = slot.operator.get_status()
                status["enabled"] = slot.enabled
                status["consecutive_failures"] = slot.consecutive_failures
                result.append(status)
        return result

    # --- Binding CRUD (runtime API) ----------------------------------------

    def _find_operator(self, operator_name: str) -> Optional[Operator]:
        """Find an operator by instance name.

        Args:
            operator_name: The ``name`` field of the operator.

        Returns:
            The :class:`Operator` instance, or ``None``.
        """
        for slot in self._slots:
            if slot.operator.name == operator_name:
                return slot.operator
        return None

    def get_all_bindings(self) -> list[dict[str, Any]]:
        """Return all active bindings across all operators.

        Returns:
            List of dicts, each with ``"target"``, ``"source"``,
            ``"operator"``, ``"param"``, and optional ``"scale"``/``"reduce"``.
        """
        result: list[dict[str, Any]] = []
        with self._lock:
            for slot in self._slots:
                op: Operator = slot.operator
                for pname, spec in op.get_bindings().items():
                    entry: dict[str, Any] = {
                        "operator": op.name,
                        "param": pname,
                        "target": f"{op.name}:{pname}",
                        "source": spec.get("signal", ""),
                    }
                    if "scale" in spec:
                        entry["scale"] = spec["scale"]
                    if "reduce" in spec:
                        entry["reduce"] = spec["reduce"]
                    result.append(entry)
        return result

    def _all_binding_map(self) -> dict[str, str]:
        """Build a flat target→source map for circular detection.

        Returns:
            Dict mapping ``"op:param"`` → ``"source_signal"`` for every
            active binding.
        """
        result: dict[str, str] = {}
        for slot in self._slots:
            for pname, spec in slot.operator.get_bindings().items():
                source: str = spec.get("signal", "")
                if source:
                    result[f"{slot.operator.name}:{pname}"] = source
        return result

    def create_binding(
        self,
        operator_name: str,
        param_name: str,
        spec: BindingSpec,
    ) -> None:
        """Create or replace a binding at runtime.

        Args:
            operator_name: Target operator instance name.
            param_name:    Target param name.
            spec:          Binding spec with ``"signal"`` key.

        Raises:
            ValueError: If operator not found, param not found, or
                        binding would create a cycle.
        """
        with self._lock:
            op: Optional[Operator] = self._find_operator(operator_name)
            if op is None:
                raise ValueError(f"Operator '{operator_name}' not found")
            if param_name not in op._param_defs:
                raise ValueError(
                    f"Param '{param_name}' not found on '{operator_name}'"
                )
            source: str = spec.get("signal", "")
            if not source:
                raise ValueError("Binding spec must include 'signal' key")
            # Circular detection.
            target_signal: str = f"{operator_name}:{param_name}"
            binding_map: dict[str, str] = self._all_binding_map()
            if check_circular_binding(target_signal, source, binding_map):
                raise ValueError(
                    f"Circular binding: {target_signal} <- {source} "
                    f"would create a cycle"
                )
            op.add_binding(param_name, spec)

    def remove_binding(
        self,
        operator_name: str,
        param_name: str,
    ) -> None:
        """Remove a binding at runtime.  Param keeps its last value.

        Args:
            operator_name: Target operator instance name.
            param_name:    Target param name.

        Raises:
            ValueError: If operator not found.
        """
        with self._lock:
            op: Optional[Operator] = self._find_operator(operator_name)
            if op is None:
                raise ValueError(f"Operator '{operator_name}' not found")
            op.remove_binding(param_name)

    # --- Internal tick loop ------------------------------------------------

    def _tick_loop(self) -> None:
        """Background thread: poll for signal changes and dispatch ticks.

        Detects bus signal changes by comparing snapshots and dispatches
        to reactive operators.  Also fires :meth:`on_tick` for periodic
        operators at their configured rate.
        """
        while self._running:
            now: float = time.monotonic()

            # --- Detect signal changes and dispatch to reactive operators ---
            try:
                current: dict[str, SignalValue] = {}
                if hasattr(self._bus, '_signals'):
                    with self._bus._lock:
                        current = dict(self._bus._signals)

                for name, value in current.items():
                    prev: Optional[SignalValue] = self._prev_signals.get(name)
                    if prev != value:
                        self.dispatch_signal(name, value)

                self._prev_signals = current
            except Exception as exc:
                logger.debug("Signal poll error: %s", exc)

            # --- Resolve param-as-signal bindings ---
            # Bindings are resolved BEFORE periodic ticks so on_tick()
            # sees the bound param values.  Resolution order follows
            # topological sort — chained bindings (A→B→C) resolve in
            # dependency order.
            with self._lock:
                for slot in self._slots:
                    if not slot.enabled:
                        continue
                    if slot.operator.tick_mode == TICK_ENGINE:
                        continue  # Engine handles effect bindings.
                    try:
                        slot.operator.resolve_bindings()
                    except Exception as exc:
                        logger.debug(
                            "Binding resolution error in '%s': %s",
                            slot.operator.name, exc,
                        )

            # --- Dispatch periodic ticks ---
            with self._lock:
                for slot in self._slots:
                    if not slot.enabled:
                        continue
                    op: Operator = slot.operator
                    # Skip engine-driven and reactive-only operators.
                    if op.tick_mode not in (TICK_PERIODIC, TICK_BOTH):
                        continue
                    elapsed: float = now - slot.last_tick
                    if elapsed < slot.tick_interval:
                        continue
                    slot.last_tick = now
                    try:
                        op.on_tick(elapsed)
                        slot.consecutive_failures = 0
                    except Exception as exc:
                        slot.consecutive_failures += 1
                        logger.error(
                            "Operator '%s' on_tick error (%d/%d): %s",
                            op.name, slot.consecutive_failures,
                            MAX_CONSECUTIVE_FAILURES, exc,
                        )
                        if slot.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            slot.enabled = False
                            logger.error(
                                "Operator '%s' auto-disabled after %d failures",
                                op.name, MAX_CONSECUTIVE_FAILURES,
                            )
                            self._notify_disabled(op.name)

            time.sleep(TICK_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Auto-import concrete operator modules so they register via __init_subclass__.
# ---------------------------------------------------------------------------

# Auto-import concrete operators so they register via __init_subclass__.
# Each import is guarded — a missing dependency in one operator must not
# prevent the others (or the Operator ABC itself) from loading.
for _mod_name in ("occupancy", "motion_gate", "trigger", "tts_announce"):
    try:
        __import__(f"operators.{_mod_name}")
    except Exception as _exc:  # noqa: F841
        logger.warning(
            "Could not load operator module '%s': %s", _mod_name, _exc,
        )
