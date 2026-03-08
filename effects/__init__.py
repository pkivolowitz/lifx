"""Effect framework for LIFX animations.

Effects are pure renderers: given a time and zone count, they produce a list
of HSBK color tuples. They carry no device or network knowledge.

To create a new effect:
    1. Create a file in ``effects/`` (e.g., ``effects/rainbow.py``).
    2. Subclass :class:`Effect`.
    3. Define params as class-level :class:`Param` instances.
    4. Implement :meth:`Effect.render`.

The effect is automatically registered and available by name.

Example::

    class Rainbow(Effect):
        name = "rainbow"
        description = "Rotating rainbow across all zones"

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

__version__ = "0.4"

from dataclasses import dataclass
from typing import Any, Optional

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

# Global registry mapping effect name -> Effect subclass.
_registry: dict[str, type["Effect"]] = {}


@dataclass
class Param:
    """Declare a tunable effect parameter.

    These declarations serve triple duty:

    * **CLI** — auto-generates ``argparse`` arguments.
    * **API** — provides metadata for a future phone app
      (name, type, range, description).
    * **Runtime** — stores the current value with validation.

    Attributes:
        default: The default value (also determines the parameter type).
        min:     Minimum allowed value (numeric params only).
        max:     Maximum allowed value (numeric params only).
        description: Human-readable help text.
        choices: If set, value must be one of these options.
    """

    default: Any
    min: Optional[Any] = None
    max: Optional[Any] = None
    description: str = ""
    choices: Optional[list] = None

    def validate(self, value: Any) -> Any:
        """Validate and clamp *value* to the declared range.

        Args:
            value: The raw value to validate.

        Returns:
            The validated (and possibly clamped) value.

        Raises:
            ValueError: If *value* is not in :attr:`choices`.
        """
        if self.choices is not None:
            if value not in self.choices:
                raise ValueError(f"Must be one of {self.choices}, got {value}")
            return value
        if isinstance(self.default, (int, float)):
            # Coerce to the same numeric type as the default
            value = type(self.default)(value)
            if self.min is not None and value < self.min:
                value = self.min
            if self.max is not None and value > self.max:
                value = self.max
        return value


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


class Effect(metaclass=EffectMeta):
    """Base class for all effects.

    Subclasses **must** define:

    * ``name: str`` — unique identifier (used in CLI and API).
    * ``description: str`` — human-readable one-liner.

    Subclasses **must** implement:

    * :meth:`render` — produce one animation frame.

    Parameters are declared as class-level :class:`Param` instances.
    At runtime they become regular attributes with their current values.
    """

    name: Optional[str] = None
    description: str = ""

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
