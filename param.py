"""Tunable parameter declaration — shared by Effects, Emitters, and Operators.

The :class:`Param` dataclass declares a named, typed, range-validated parameter.
It serves triple duty:

* **CLI** — auto-generates ``argparse`` arguments.
* **API** — provides metadata for the phone app (name, type, range, description).
* **Runtime** — stores the current value with validation and clamping.

Extracted to its own module to avoid circular imports between the ``effects``,
``emitters``, and ``operators`` packages that all use it.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Param:
    """Declare a tunable parameter with validation and clamping.

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
            # Coerce to the same numeric type as the default.
            # Guard against garbage input — fall back to default
            # rather than crashing the effect engine.
            try:
                value = type(self.default)(value)
            except (ValueError, TypeError, OverflowError):
                value = self.default
            # NaN defeats comparison operators — fall back to default.
            if isinstance(value, float) and value != value:
                value = self.default
            if self.min is not None and value < self.min:
                value = self.min
            if self.max is not None and value > self.max:
                value = self.max
        return value
