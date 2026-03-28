"""Shared condition evaluation for trigger-type operators.

Provides a single, well-tested condition evaluator used by
:class:`~operators.trigger.TriggerOperator` and any future
operator that needs threshold-based signal evaluation.

Condition operators:

    - ``eq``  — equal
    - ``gt``  — greater than
    - ``lt``  — less than
    - ``gte`` — greater than or equal
    - ``lte`` — less than or equal
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
import operator as op_module
from typing import Any, Callable, Optional

logger: logging.Logger = logging.getLogger("glowup.operators.conditions")

# ---------------------------------------------------------------------------
# Condition operator dispatch table
# ---------------------------------------------------------------------------

CONDITION_OPS: dict[str, Callable] = {
    "eq":  op_module.eq,
    "gt":  op_module.gt,
    "lt":  op_module.lt,
    "gte": op_module.ge,
    "lte": op_module.le,
}


def evaluate_condition(
    op_name: str,
    threshold: Any,
    value: Any,
) -> bool:
    """Evaluate a trigger condition.

    Looks up the condition operator by name and applies it to
    ``value`` and ``threshold``.  Unknown operators return ``False``
    and log a warning.  Type errors are caught and logged at debug
    level.

    Args:
        op_name:   Condition name (``"eq"``, ``"gt"``, etc.).
        threshold: The threshold value from config.
        value:     The signal value to test.

    Returns:
        ``True`` if the condition is satisfied, ``False`` otherwise.
    """
    op_fn: Optional[Callable] = CONDITION_OPS.get(op_name)
    if op_fn is None:
        logger.warning("Unknown condition operator: %s", op_name)
        return False
    try:
        return op_fn(value, threshold)
    except (TypeError, ValueError) as exc:
        logger.debug("Condition eval error (%s): %s", op_name, exc)
        return False
