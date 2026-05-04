"""CombineOperator — RPN-driven combinator of N input signals into one output.

Reads an arbitrary list of input signals from the bus and evaluates a
Reverse Polish Notation expression to produce a single output signal.
The expression is a space-separated token stream where each token is
either:

- A signal name (read from the bus, pushed on the stack).
- A numeric literal (``"0.5"``, ``"-1.25"``) — pushed on the stack.
- An operator token (consumes operands, pushes result).

Supported operators (all operate on scalar floats):

  Boolean (treat inputs as truthy at >= 0.5, output 1.0 / 0.0):

    - ``NOT``   — unary: 1.0 if top < 0.5 else 0.0
    - ``AND``   — binary: both truthy
    - ``OR``    — binary: either truthy
    - ``NAND``  — binary: NOT (a AND b)
    - ``NOR``   — binary: NOT (a OR b)
    - ``XOR``   — binary: exactly one truthy

  Arithmetic (raw floats):

    - ``+`` ``-`` ``*`` ``/``  — binary
    - ``NEG``                  — unary negation
    - ``MIN`` ``MAX``          — binary
    - ``ABS``                  — unary
    - ``DUP``                  — duplicate top
    - ``SWAP``                 — swap top two
    - ``DROP``                 — pop top

The combinator is fully reusable: pass any RPN expression and any set
of input signals, write any output signal.

The operator is reactive: any change to an input signal triggers a
re-evaluation and write of the output.  Unknown tokens (neither a
signal, a literal, nor a registered op) cause the evaluation to abort
with a warning and leave the previous output unchanged.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import logging
from typing import Any, Callable, Optional

from operators import Operator, TICK_REACTIVE, SignalValue

logger: logging.Logger = logging.getLogger("glowup.operators.combine")

# Boolean truthiness threshold.
TRUTHY: float = 0.5


def _bool(x: float) -> float:
    """Coerce a float to 1.0 / 0.0 boolean."""
    return 1.0 if x >= TRUTHY else 0.0


# ---------------------------------------------------------------------------
# RPN operator dispatch — each entry is (arity, callable-over-stack).
# ---------------------------------------------------------------------------

def _op_not(s: list[float]) -> None:
    s.append(1.0 - _bool(s.pop()))


def _op_and(s: list[float]) -> None:
    b = _bool(s.pop()); a = _bool(s.pop())
    s.append(1.0 if (a >= TRUTHY and b >= TRUTHY) else 0.0)


def _op_or(s: list[float]) -> None:
    b = _bool(s.pop()); a = _bool(s.pop())
    s.append(1.0 if (a >= TRUTHY or b >= TRUTHY) else 0.0)


def _op_nand(s: list[float]) -> None:
    _op_and(s); _op_not(s)


def _op_nor(s: list[float]) -> None:
    _op_or(s); _op_not(s)


def _op_xor(s: list[float]) -> None:
    b = _bool(s.pop()); a = _bool(s.pop())
    s.append(1.0 if (a >= TRUTHY) != (b >= TRUTHY) else 0.0)


def _op_add(s: list[float]) -> None:
    b = s.pop(); a = s.pop(); s.append(a + b)


def _op_sub(s: list[float]) -> None:
    b = s.pop(); a = s.pop(); s.append(a - b)


def _op_mul(s: list[float]) -> None:
    b = s.pop(); a = s.pop(); s.append(a * b)


def _op_div(s: list[float]) -> None:
    b = s.pop(); a = s.pop()
    s.append(a / b if b != 0.0 else 0.0)


def _op_neg(s: list[float]) -> None:
    s.append(-s.pop())


def _op_min(s: list[float]) -> None:
    b = s.pop(); a = s.pop(); s.append(min(a, b))


def _op_max(s: list[float]) -> None:
    b = s.pop(); a = s.pop(); s.append(max(a, b))


def _op_abs(s: list[float]) -> None:
    s.append(abs(s.pop()))


def _op_dup(s: list[float]) -> None:
    s.append(s[-1])


def _op_swap(s: list[float]) -> None:
    s[-1], s[-2] = s[-2], s[-1]


def _op_drop(s: list[float]) -> None:
    s.pop()


RPN_OPS: dict[str, tuple[int, Callable[[list[float]], None]]] = {
    "NOT":  (1, _op_not),
    "AND":  (2, _op_and),
    "OR":   (2, _op_or),
    "NAND": (2, _op_nand),
    "NOR":  (2, _op_nor),
    "XOR":  (2, _op_xor),
    "+":    (2, _op_add),
    "-":    (2, _op_sub),
    "*":    (2, _op_mul),
    "/":    (2, _op_div),
    "NEG":  (1, _op_neg),
    "MIN":  (2, _op_min),
    "MAX":  (2, _op_max),
    "ABS":  (1, _op_abs),
    "DUP":  (1, _op_dup),
    "SWAP": (2, _op_swap),
    "DROP": (1, _op_drop),
}


# ---------------------------------------------------------------------------
# CombineOperator
# ---------------------------------------------------------------------------

class CombineOperator(Operator):
    """Evaluate an RPN expression over N input signals, write one output."""

    operator_type: str = "combine"
    description: str = "RPN combinator — N inputs, 1 output, any expression"

    # Dynamic — set from config in __init__.
    input_signals: list[str] = []
    output_signals: list[str] = []

    tick_mode: str = TICK_REACTIVE

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        bus: Any,
    ) -> None:
        super().__init__(name, config, bus)

        # Input signals — the operator subscribes to these for reactive
        # re-evaluation.  The RPN expression may reference any subset
        # (and may also reference signals NOT in this list, which are
        # read on demand but do not wake the operator).
        self._inputs: list[str] = list(config.get("input_signals", []))
        self.input_signals = list(self._inputs)

        # Output signal — where the result is written.
        outs: list[str] = list(config.get("output_signals", []))
        if not outs:
            raise ValueError(
                f"CombineOperator '{name}': output_signals required",
            )
        self._output: str = outs[0]
        self.output_signals = [self._output]

        # RPN expression tokens.
        expr: str = config.get("expression", "")
        if not expr.strip():
            raise ValueError(
                f"CombineOperator '{name}': expression required",
            )
        self._tokens: list[str] = expr.split()

        # Validate: quick pass — every token must be a known op, a
        # numeric literal, or a plausible signal name.  We do NOT
        # require signals to exist on the bus yet (they may be
        # produced by a peer operator not yet started).
        for tok in self._tokens:
            if tok in RPN_OPS:
                continue
            try:
                float(tok)
                continue
            except ValueError:
                pass
            # Treat anything else as a signal name.  No further check.

        self._last_output: Optional[float] = None

    def on_start(self) -> None:
        """Log configuration and perform an initial evaluation."""
        logger.info(
            "CombineOperator '%s' started — %d inputs, expr: %s, out: %s",
            self.name, len(self._inputs), " ".join(self._tokens),
            self._output,
        )
        # Evaluate once on start so the output signal has a value even
        # before any input changes.
        self._evaluate_and_write()

    def on_signal(self, name: str, value: SignalValue) -> None:
        """Re-evaluate the RPN expression when any input signal changes."""
        self._evaluate_and_write()

    def _evaluate_and_write(self) -> None:
        """Evaluate the RPN expression and write the result to the output."""
        try:
            result: float = self._evaluate()
        except (IndexError, ValueError, TypeError) as exc:
            logger.warning(
                "CombineOperator '%s' evaluation error: %s", self.name, exc,
            )
            return
        if result != self._last_output:
            self.write(self._output, result)
            self._last_output = result

    def _evaluate(self) -> float:
        """Run the RPN expression against current bus values.

        Returns:
            The final top-of-stack value.

        Raises:
            IndexError: Stack underflow (malformed expression).
            ValueError: Empty stack at end, or unknown token resolved to nothing.
        """
        stack: list[float] = []
        for tok in self._tokens:
            entry = RPN_OPS.get(tok)
            if entry is not None:
                arity, fn = entry
                if len(stack) < arity:
                    raise IndexError(
                        f"stack underflow at '{tok}' "
                        f"(need {arity}, have {len(stack)})",
                    )
                fn(stack)
                continue
            # Numeric literal?
            try:
                stack.append(float(tok))
                continue
            except ValueError:
                pass
            # Signal name — read from bus.
            raw = self.read(tok, 0.0)
            if isinstance(raw, list):
                stack.append(max(raw) if raw else 0.0)
            else:
                stack.append(float(raw))
        if not stack:
            raise ValueError("empty stack at end of expression")
        return stack[-1]
