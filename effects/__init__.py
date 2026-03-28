"""Effect framework for LIFX animations.

Effects are pure renderers: given a time and zone count, they produce a list
of HSBK color tuples. They carry no device or network knowledge.

To create a new effect:
    1. Create a file in ``effects/`` (e.g., ``effects/rainbow.py``).
    2. Subclass :class:`Effect`.
    3. Set ``affinity`` to the device types the effect is designed for.
    4. Define params as class-level :class:`Param` instances.
    5. Implement :meth:`Effect.render`.

The effect is automatically registered and available by name.

Example::

    class Rainbow(Effect):
        name = "rainbow"
        description = "Rotating rainbow across all zones"
        affinity = frozenset({DEVICE_TYPE_STRIP})

        speed = Param(2.0, min=0.1, max=30.0,
                      description="Seconds per full rotation")

        def render(self, t: float, zone_count: int) -> list[HSBK]:
            colors: list[HSBK] = []
            for i in range(zone_count):
                hue = int(((i / zone_count) + t / self.speed) % 1.0 * HSBK_MAX)
                colors.append((hue, HSBK_MAX, HSBK_MAX, KELVIN_DEFAULT))
            return colors
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "0.7"

from typing import Any, Optional, Union

from operators import Operator
from param import Param

# ---------------------------------------------------------------------------
# Constants — shared by all effects to eliminate magic numbers
# ---------------------------------------------------------------------------

# Maximum value for any HSBK component (hue, saturation, brightness).
# LIFX uses unsigned 16-bit integers for these fields.
HSBK_MAX: int = 65535

# Full circle in degrees — used when converting user-facing hue (0-360)
# to the LIFX 16-bit hue range (0-65535).
DEGREES_FULL: float = 360.0

# Percentage maximum — used when converting user-facing percent (0-100)
# to the LIFX 16-bit range.
PERCENT_MAX: int = 100

# Color temperature boundaries and default, in Kelvin.
KELVIN_MIN: int = 1500
KELVIN_MAX: int = 9000
KELVIN_DEFAULT: int = 3500

# Type alias for a single LIFX HSBK color value.
# (hue_u16, saturation_u16, brightness_u16, kelvin)
HSBK = tuple[int, int, int, int]

# ---------------------------------------------------------------------------
# Device form-factor constants — used by Effect.affinity to declare which
# device types an effect is designed for.  Matches the taxonomy in
# discover.py ("Bulb" / "Strip" / "Matrix").
# ---------------------------------------------------------------------------

# Single-zone devices (A19, BR30, etc.).
DEVICE_TYPE_BULB: str = "bulb"
# 1D multizone devices (Neon, String, Beam, Z strip).
DEVICE_TYPE_STRIP: str = "strip"
# 2D grid devices (Luna, Tile, Candle, Ceiling).
DEVICE_TYPE_MATRIX: str = "matrix"

# Convenience set: effect works on all device types (default for Effect).
ALL_DEVICE_TYPES: frozenset[str] = frozenset({
    DEVICE_TYPE_BULB,
    DEVICE_TYPE_STRIP,
    DEVICE_TYPE_MATRIX,
})

# Global registry mapping effect name -> Effect subclass.
_registry: dict[str, type["Effect"]] = {}


# Param is now in param.py (shared by effects, emitters, operators).
# Re-exported here so ``from effects import Param`` continues to work.


class EffectMeta(type):
    """Metaclass that auto-registers :class:`Effect` subclasses.

    Any concrete subclass with a non-``None`` :attr:`Effect.name` is added
    to the global :data:`_registry` at class-creation time.
    """

    def __init__(
        cls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
    ) -> None:
        """Register the new Effect subclass if it has a name.

        Args:
            name:      Class name (set by Python).
            bases:     Base classes tuple.
            namespace: Class body namespace dict.
        """
        super().__init__(name, bases, namespace)
        # Only register concrete subclasses that define a name, not the
        # abstract Effect base class itself.
        if bases and hasattr(cls, "name") and cls.name is not None:
            _registry[cls.name] = cls
        # Validate affinity at class-definition time so typos fail fast.
        if bases and hasattr(cls, "affinity"):
            if not cls.affinity:
                raise ValueError(
                    f"Effect {name} has empty affinity — must support "
                    f"at least one device type",
                )
            invalid: frozenset[str] = cls.affinity - ALL_DEVICE_TYPES
            if invalid:
                raise ValueError(
                    f"Effect {name} declares unknown affinity "
                    f"values: {invalid}",
                )


class Effect(Operator, metaclass=EffectMeta):
    """Base class for all effects — specialized Operators that render HSBK.

    Effects are operators that read inputs (time, signal bus data), transform
    them through rendering math, and produce HSBK frames for emitters.  They
    inherit from :class:`~operators.Operator` to participate in the unified
    SOE pipeline while retaining their specialized rendering lifecycle managed
    by the Engine.

    Subclasses **must** define:

    * ``name: str`` — unique identifier (used in CLI and API).
    * ``description: str`` — human-readable one-liner.

    Subclasses **should** define:

    * ``affinity: frozenset[str]`` — device types this effect is designed
      for.  Defaults to :data:`ALL_DEVICE_TYPES` (universal).  Use the
      ``DEVICE_TYPE_BULB``, ``DEVICE_TYPE_STRIP``, and/or
      ``DEVICE_TYPE_MATRIX`` constants.  Advisory only — the engine does
      not block execution on mismatched devices.

    Subclasses **must** implement:

    * :meth:`render` — produce one animation frame.

    Subclasses **may** set:

    * ``is_transient: bool = True`` — marks the effect as a one-shot
      command (e.g., "on" or "off").  Transient effects execute a
      single action via :meth:`execute` and then the play command
      sleeps until SIGTERM — no render loop, no Engine threads.

    Parameters are declared as class-level :class:`Param` instances.
    At runtime they become regular attributes with their current values.

    Note: Effects set ``operator_type = None`` so they are NOT registered
    in the operator registry (they use the separate effect registry via
    :class:`EffectMeta`).  Effects use ``tick_mode = "engine"`` so the
    :class:`~operators.OperatorManager` skips them — the Engine drives
    rendering at frame rate.
    """

    # Operator ABC attributes — Effects do not register as operators.
    operator_type = None
    tick_mode: str = "engine"

    name: Optional[str] = None
    description: str = ""
    affinity: frozenset[str] = ALL_DEVICE_TYPES
    is_transient: bool = False

    def __init__(self, **overrides: Any) -> None:
        """Initialize with default params, applying any *overrides*.

        Args:
            **overrides: Parameter names mapped to override values.
        """
        self._param_defs: dict[str, Param] = {}
        # Walk the class hierarchy to collect all Param declarations.
        for attr_name in dir(self.__class__):
            val = getattr(self.__class__, attr_name)
            if isinstance(val, Param):
                self._param_defs[attr_name] = val
                if attr_name in overrides:
                    setattr(self, attr_name, val.validate(overrides[attr_name]))
                else:
                    setattr(self, attr_name, val.default)

    def render(self, t: float, zone_count: int) -> list[HSBK]:
        """Produce one frame of colors.

        Args:
            t:          Seconds elapsed since effect started.
            zone_count: Number of zones on the target device.

        Returns:
            A list of *zone_count* HSBK tuples.

        Raises:
            NotImplementedError: If the subclass has not overridden this.
        """
        raise NotImplementedError

    def execute(self, emitter: Any) -> None:
        """Perform a one-shot action on the emitter (transient effects only).

        Transient effects override this instead of (or in addition to)
        :meth:`render`.  The play command calls ``execute`` once, then
        sleeps until SIGTERM — no render loop is started.

        The default implementation raises :class:`NotImplementedError`
        so that non-transient effects that accidentally set
        ``is_transient = True`` fail loudly.

        Args:
            emitter: The :class:`~emitters.lifx.LifxEmitter` (or any
                     emitter with ``send_color`` / ``power_on`` /
                     ``power_off`` methods).

        Raises:
            NotImplementedError: If the subclass has not overridden this.
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
    def get_param_defs(cls) -> dict[str, "Param"]:
        """Return parameter definitions as ``{name: Param}``."""
        defs: dict[str, Param] = {}
        for attr_name in dir(cls):
            val = getattr(cls, attr_name)
            if isinstance(val, Param):
                defs[attr_name] = val
        return defs

    def period(self) -> Optional[float]:
        """Return the animation period in seconds, or ``None`` if aperiodic.

        When a period is known, recording tools can capture exactly one
        cycle to produce a seamlessly looping animation.

        The default implementation returns the ``speed`` parameter if the
        effect declares one (most cyclic effects use *speed* as seconds
        per full cycle).  Aperiodic effects should override this to
        return ``None``.

        Returns:
            Period in seconds, or ``None`` if the effect does not loop.
        """
        if hasattr(self, "speed") and "speed" in self._param_defs:
            return float(self.speed)
        return None

    def on_start(self, zone_count: int) -> None:
        """Called when this effect becomes active.

        Override for one-time setup that depends on the device.

        Args:
            zone_count: Number of zones on the target device.
        """

    def on_stop(self) -> None:
        """Called when this effect is being replaced or stopped.

        Override to release resources or reset state.
        """


# ---------------------------------------------------------------------------
# MediaEffect — base class for effects with direct signal bus access
# ---------------------------------------------------------------------------

class MediaEffect(Effect):
    """Effect subclass with direct access to the media signal bus.

    Use this as a base class for effects that need raw signal data
    (frequency bands, beat triggers, video features) rather than simple
    parameter modulation via bindings.

    The engine injects ``_signal_bus`` when starting a MediaEffect.
    Subclasses read signals via :meth:`signal` — if the bus is not
    available (e.g., running without media), the default value is
    returned silently so the effect still renders.

    Example::

        class Spectrum(MediaEffect):
            name = "spectrum"
            description = "Audio spectrum visualizer"
            source = Param("backyard", description="Signal source name")

            def render(self, t: float, zone_count: int) -> list[HSBK]:
                bands = self.signal(f"{self.source}:audio:bands", [0.0] * zone_count)
                # ... map bands to zone colors ...
    """

    # Set by Engine.start() when the effect is activated.  None when
    # running without a media pipeline (graceful degradation).
    _signal_bus: Any = None

    def signal(self, name: str,
               default: Union[float, list[float]] = 0.0
               ) -> Union[float, list[float]]:
        """Read a named signal from the media bus.

        This is the primary interface for MediaEffect subclasses to
        consume media-derived data each frame.

        Args:
            name:    Hierarchical signal name
                     (e.g., ``"backyard:audio:bass"``).
            default: Value returned when the signal is unavailable
                     or the bus is not connected.

        Returns:
            The current signal value (scalar or array), or *default*.
        """
        if self._signal_bus is not None:
            return self._signal_bus.read(name, default)
        return default

    def signal_names(self) -> list[str]:
        """Return available signal names, or an empty list if no bus.

        Useful for effects that adapt to whatever signals exist
        (e.g., auto-detecting available audio sources).

        Returns:
            Sorted list of registered signal names.
        """
        if self._signal_bus is not None:
            return self._signal_bus.signal_names()
        return []


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def get_registry() -> dict[str, type[Effect]]:
    """Return a copy of the effect registry ``{name: Effect class}``."""
    return dict(_registry)


def get_effect_names() -> list[str]:
    """Return a sorted list of available effect names."""
    return sorted(_registry.keys())


def create_effect(name: str, **params: Any) -> Effect:
    """Instantiate an effect by name with optional parameter overrides.

    Args:
        name:     Registered effect name (e.g., ``"cylon"``).
        **params: Parameter overrides forwarded to the constructor.

    Returns:
        A fully-initialized :class:`Effect` instance.

    Raises:
        ValueError: If *name* is not in the registry.
    """
    if name not in _registry:
        available = ", ".join(get_effect_names())
        raise ValueError(f"Unknown effect '{name}'. Available: {available}")
    return _registry[name](**params)


# ---------------------------------------------------------------------------
# Utility helpers for effects — convert user-facing values to LIFX u16.
# ---------------------------------------------------------------------------

def hue_to_u16(degrees: float) -> int:
    """Convert a hue in degrees (0-360) to LIFX u16 (0-65535).

    Args:
        degrees: Hue angle in degrees.

    Returns:
        The equivalent 16-bit hue value, wrapped to [0, 65535].
    """
    return int(degrees * HSBK_MAX / DEGREES_FULL) % (HSBK_MAX + 1)


def pct_to_u16(percent: int | float) -> int:
    """Convert a percentage (0-100) to LIFX u16 (0-65535).

    Args:
        percent: Value as a percentage (0-100).

    Returns:
        The equivalent 16-bit value.
    """
    return int(percent * HSBK_MAX / PERCENT_MAX)


# ---------------------------------------------------------------------------
# BT.709 luma coefficients for perceptual luminance conversion.
# Standard ITU-R BT.709 used in HDTV.
# ---------------------------------------------------------------------------

_LUMA_R: float = 0.2126
_LUMA_G: float = 0.7152
_LUMA_B: float = 0.0722

# Number of sextants in the HSB color wheel.
_HUE_SEXTANTS: int = 6


def hsbk_to_luminance(hue: int, sat: int, bri: int, kelvin: int) -> HSBK:
    """Convert an HSBK color to a monochrome HSBK using BT.709 luma.

    Performs HSB → RGB → BT.709 luminance conversion so that color
    effects produce perceptually correct brightness on monochrome
    (white-only) LIFX bulbs.  The result has saturation zero and the
    original kelvin preserved.

    Args:
        hue:    LIFX hue (0-65535).
        sat:    LIFX saturation (0-65535).
        bri:    LIFX brightness (0-65535).
        kelvin: Color temperature (1500-9000).

    Returns:
        An HSBK tuple ``(0, 0, luminance, kelvin)`` suitable for
        :meth:`LifxDevice.set_color`.
    """
    # Normalize to [0, 1].
    h: float = (hue / HSBK_MAX) * _HUE_SEXTANTS  # 0-6 range for sextant math
    s: float = sat / HSBK_MAX
    b: float = bri / HSBK_MAX

    # HSB to RGB (standard algorithm).
    c: float = b * s           # chroma
    x: float = c * (1.0 - abs(h % 2.0 - 1.0))  # secondary component
    m: float = b - c           # brightness offset

    sextant: int = int(h) % _HUE_SEXTANTS
    if sextant == 0:
        r, g, bl = c + m, x + m, m
    elif sextant == 1:
        r, g, bl = x + m, c + m, m
    elif sextant == 2:
        r, g, bl = m, c + m, x + m
    elif sextant == 3:
        r, g, bl = m, x + m, c + m
    elif sextant == 4:
        r, g, bl = x + m, m, c + m
    else:
        r, g, bl = c + m, m, x + m

    # BT.709 perceptual luminance.
    y: float = _LUMA_R * r + _LUMA_G * g + _LUMA_B * bl

    luminance: int = min(int(y * HSBK_MAX), HSBK_MAX)
    return (0, 0, luminance, kelvin)


# ---------------------------------------------------------------------------
# Auto-import all effect modules so they self-register.
# New effects just need a .py file in this directory.
# ---------------------------------------------------------------------------
import importlib  # noqa: E402
import os         # noqa: E402
import pkgutil    # noqa: E402

_pkg_dir: str = os.path.dirname(__file__)
for _importer, _modname, _ispkg in pkgutil.iter_modules([_pkg_dir]):
    importlib.import_module(f".{_modname}", __package__)
