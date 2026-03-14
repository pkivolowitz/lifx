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

__version__ = "0.1"

from dataclasses import dataclass, field
from typing import Any, Optional

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

# Global registry mapping emitter_type -> Emitter subclass.
_registry: dict[str, type["Emitter"]] = {}


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
# Auto-import all emitter modules so they self-register.
# New emitters just need a .py file in this directory.
# ---------------------------------------------------------------------------
import importlib  # noqa: E402
import os         # noqa: E402
import pkgutil    # noqa: E402

_pkg_dir: str = os.path.dirname(__file__)
for _importer, _modname, _ispkg in pkgutil.iter_modules([_pkg_dir]):
    importlib.import_module(f".{_modname}", __package__)
