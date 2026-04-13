"""Tests for CombineOperator — RPN expression evaluation."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import unittest
from unittest.mock import MagicMock

from media import SignalBus
from operators.combine import CombineOperator


def make_op(expr, inputs=None, output="out:value"):
    """Build a CombineOperator with a live SignalBus for testing."""
    bus = SignalBus()
    cfg = {
        "expression": expr,
        "input_signals": inputs or [],
        "output_signals": [output],
    }
    op = CombineOperator("test", cfg, bus)
    return op, bus


class RPNBooleanTests(unittest.TestCase):

    def test_and_true(self):
        op, bus = make_op("a:x b:y AND",
                          inputs=["a:x", "b:y"])
        bus.write("a:x", 1.0)
        bus.write("b:y", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 1.0)

    def test_and_false(self):
        op, bus = make_op("a:x b:y AND",
                          inputs=["a:x", "b:y"])
        bus.write("a:x", 1.0)
        bus.write("b:y", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)

    def test_or(self):
        op, bus = make_op("a:x b:y OR", inputs=["a:x", "b:y"])
        bus.write("a:x", 0.0)
        bus.write("b:y", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 1.0)

    def test_not(self):
        op, bus = make_op("a:x NOT", inputs=["a:x"])
        bus.write("a:x", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 1.0)
        bus.write("a:x", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)

    def test_nand(self):
        op, bus = make_op("a:x b:y NAND", inputs=["a:x", "b:y"])
        bus.write("a:x", 1.0); bus.write("b:y", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)
        bus.write("b:y", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 1.0)

    def test_nor(self):
        op, bus = make_op("a:x b:y NOR", inputs=["a:x", "b:y"])
        bus.write("a:x", 0.0); bus.write("b:y", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 1.0)

    def test_xor(self):
        op, bus = make_op("a:x b:y XOR", inputs=["a:x", "b:y"])
        bus.write("a:x", 1.0); bus.write("b:y", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 1.0)
        bus.write("b:y", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)

    def test_and_not_pattern(self):
        """The exact pattern used by clock night_mode:
        time:is_night AND NOT main_bedroom:any_on."""
        op, bus = make_op(
            "time:is_night group:main_bedroom:any_on NOT AND",
            inputs=["time:is_night", "group:main_bedroom:any_on"],
        )
        # Night + lights off → night mode.
        bus.write("time:is_night", 1.0)
        bus.write("group:main_bedroom:any_on", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 1.0)
        # Night but lights on → day mode.
        bus.write("group:main_bedroom:any_on", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)
        # Day + lights off → day mode.
        bus.write("time:is_night", 0.0)
        bus.write("group:main_bedroom:any_on", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)
        # Day + lights on → day mode.
        bus.write("group:main_bedroom:any_on", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)


class RPNArithmeticTests(unittest.TestCase):

    def test_add(self):
        op, bus = make_op("a:x b:y +", inputs=["a:x", "b:y"])
        bus.write("a:x", 3.0); bus.write("b:y", 4.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 7.0)

    def test_subtract(self):
        op, bus = make_op("a:x b:y -", inputs=["a:x", "b:y"])
        bus.write("a:x", 10.0); bus.write("b:y", 4.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 6.0)

    def test_multiply(self):
        op, bus = make_op("a:x b:y *", inputs=["a:x", "b:y"])
        bus.write("a:x", 3.0); bus.write("b:y", 5.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 15.0)

    def test_divide(self):
        op, bus = make_op("a:x b:y /", inputs=["a:x", "b:y"])
        bus.write("a:x", 12.0); bus.write("b:y", 4.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 3.0)

    def test_divide_by_zero(self):
        op, bus = make_op("a:x b:y /", inputs=["a:x", "b:y"])
        bus.write("a:x", 12.0); bus.write("b:y", 0.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)

    def test_literal_mix(self):
        op, bus = make_op("a:x 2.0 *", inputs=["a:x"])
        bus.write("a:x", 3.5)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 7.0)

    def test_min_max(self):
        op, bus = make_op("a:x b:y MIN", inputs=["a:x", "b:y"])
        bus.write("a:x", 3.0); bus.write("b:y", 5.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 3.0)

        op, bus = make_op("a:x b:y MAX", inputs=["a:x", "b:y"])
        bus.write("a:x", 3.0); bus.write("b:y", 5.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 5.0)

    def test_dup_swap_drop(self):
        op, bus = make_op("a:x DUP *", inputs=["a:x"])
        bus.write("a:x", 6.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 36.0)

        op, bus = make_op("a:x b:y SWAP -", inputs=["a:x", "b:y"])
        bus.write("a:x", 10.0); bus.write("b:y", 3.0)
        op._evaluate_and_write()
        # After SWAP: stack is [b, a] = [3, 10]; then -: 3 - 10 = -7.
        self.assertEqual(bus.read("out:value"), -7.0)

        op, bus = make_op("a:x b:y DROP", inputs=["a:x", "b:y"])
        bus.write("a:x", 10.0); bus.write("b:y", 3.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 10.0)

    def test_neg_abs(self):
        op, bus = make_op("a:x NEG", inputs=["a:x"])
        bus.write("a:x", 5.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), -5.0)

        op, bus = make_op("a:x ABS", inputs=["a:x"])
        bus.write("a:x", -5.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 5.0)


class ConfigErrorTests(unittest.TestCase):

    def test_missing_expression(self):
        bus = SignalBus()
        with self.assertRaises(ValueError):
            CombineOperator("t", {"output_signals": ["o:x"]}, bus)

    def test_missing_output(self):
        bus = SignalBus()
        with self.assertRaises(ValueError):
            CombineOperator("t", {"expression": "1 2 +"}, bus)

    def test_stack_underflow_logged_not_raised(self):
        """Malformed expressions should log and leave output unchanged."""
        op, bus = make_op("a:x +", inputs=["a:x"])
        bus.write("out:value", 42.0)
        bus.write("a:x", 1.0)
        # Expression pops two for +, only has one → underflow.
        op._evaluate_and_write()  # Must not raise.
        # Output unchanged.
        self.assertEqual(bus.read("out:value"), 42.0)


class ReactiveTests(unittest.TestCase):

    def test_on_signal_triggers_evaluation(self):
        op, bus = make_op("a:x b:y AND", inputs=["a:x", "b:y"])
        bus.write("a:x", 1.0)
        bus.write("b:y", 1.0)
        op.on_signal("a:x", 1.0)
        self.assertEqual(bus.read("out:value"), 1.0)
        bus.write("b:y", 0.0)
        op.on_signal("b:y", 0.0)
        self.assertEqual(bus.read("out:value"), 0.0)

    def test_no_rewrite_on_stable_output(self):
        """Output only written when result actually changes."""
        op, bus = make_op("a:x NOT", inputs=["a:x"])
        bus.write("a:x", 1.0)
        op._evaluate_and_write()
        self.assertEqual(bus.read("out:value"), 0.0)
        # Simulate a bus-write tracker to verify no-op on unchanged result.
        writes = []
        orig_write = op.write
        def track(n, v):
            writes.append((n, v))
            orig_write(n, v)
        op.write = track  # type: ignore
        op._evaluate_and_write()  # Same inputs → no write.
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
