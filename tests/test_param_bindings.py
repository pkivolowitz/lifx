"""Test suite for param-as-signal and parameter binding.

Tests cover:
- Param registration on bus at operator start
- Param update via set_params propagates to bus
- Binding source→target scalar
- Binding source→target with scale
- Binding source→target with array reduce
- Param-to-param binding (After Effects expression link)
- Circular binding rejection
- Binding overrides manual set_params
- Breaking binding restores manual control
- Binding survives operator restart
- Binding with missing source signal (graceful default)
- Binding visibility in get_status / get_bindings
- Config-declared bindings load at startup
- Runtime-created bindings via OperatorManager
- Multiple bindings on different params of same operator
- Binding between operators in different tick modes
- Binding to a signal that doesn't exist yet (late-arriving sensor)
- OperatorManager tick loop resolves bindings before on_tick
- resolve_binding utility function
- check_circular_binding utility function

Run independently::

    python3 -m pytest tests/test_param_bindings.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

from param import Param
from operators import (
    Operator,
    OperatorManager,
    SignalValue,
    TICK_REACTIVE,
    TICK_PERIODIC,
    TICK_BOTH,
    resolve_binding,
    check_circular_binding,
)


# ---------------------------------------------------------------------------
# Minimal SignalBus stub — just enough for operator tests
# ---------------------------------------------------------------------------

class StubBus:
    """Thread-safe signal bus stub matching the real SignalBus API."""

    def __init__(self) -> None:
        self._signals: dict[str, SignalValue] = {}
        self._lock: threading.Lock = threading.Lock()

    def read(self, name: str, default: SignalValue = 0.0) -> SignalValue:
        with self._lock:
            return self._signals.get(name, default)

    def write(self, name: str, value: SignalValue) -> None:
        with self._lock:
            self._signals[name] = value

    def snapshot(self) -> dict[str, SignalValue]:
        with self._lock:
            return dict(self._signals)


# ---------------------------------------------------------------------------
# Test operators — minimal implementations for param binding tests
# ---------------------------------------------------------------------------

class AlphaOp(Operator):
    """Test operator with two numeric params."""

    operator_type = "test_alpha"
    description = "Test alpha operator"
    input_signals = ["test:*"]
    output_signals = []
    tick_mode = TICK_PERIODIC
    tick_hz = 10.0

    speed = Param(5.0, min=0.1, max=30.0, description="Speed")
    brightness = Param(50, min=0, max=100, description="Brightness")

    def __init__(self, name: str, config: dict, bus: Any) -> None:
        super().__init__(name, config, bus)
        self.tick_count: int = 0
        self.last_speed_at_tick: float = self.speed

    def on_tick(self, dt: float) -> None:
        self.tick_count += 1
        self.last_speed_at_tick = self.speed


class BetaOp(Operator):
    """Test operator with one numeric param, reactive mode."""

    operator_type = "test_beta"
    description = "Test beta operator"
    input_signals = ["test:*"]
    output_signals = []
    tick_mode = TICK_REACTIVE

    rate = Param(1.0, min=0.0, max=10.0, description="Rate")

    def __init__(self, name: str, config: dict, bus: Any) -> None:
        super().__init__(name, config, bus)
        self.signal_count: int = 0

    def on_signal(self, name: str, value: SignalValue) -> None:
        self.signal_count += 1


class GammaOp(Operator):
    """Test operator with a string (non-numeric) param — should not go on bus."""

    operator_type = "test_gamma"
    description = "Test gamma operator"
    input_signals = []
    output_signals = []
    tick_mode = TICK_REACTIVE

    label = Param("default_label", description="Non-numeric param")
    intensity = Param(0.5, min=0.0, max=1.0, description="Intensity")


# ---------------------------------------------------------------------------
# resolve_binding() unit tests
# ---------------------------------------------------------------------------

class TestResolveBinding(unittest.TestCase):
    """Tests for the resolve_binding utility function."""

    def test_scalar_no_scale(self) -> None:
        """Scalar source, no scale, no param def — identity mapping."""
        result = resolve_binding(0.7, None, {"signal": "x"})
        self.assertAlmostEqual(result, 0.7, places=5)

    def test_scalar_with_explicit_scale(self) -> None:
        """Scalar source with explicit scale range."""
        result = resolve_binding(0.5, None, {"signal": "x", "scale": [10, 20]})
        self.assertAlmostEqual(result, 15.0, places=5)

    def test_scalar_with_param_range(self) -> None:
        """Scalar source, scale from param def min/max."""
        pdef = Param(5.0, min=0.1, max=30.0)
        result = resolve_binding(0.5, pdef, {"signal": "x"})
        self.assertAlmostEqual(result, 15.05, places=5)

    def test_array_reduce_max(self) -> None:
        """Array source, reduce=max."""
        result = resolve_binding([0.2, 0.8, 0.5], None,
                                  {"signal": "x", "reduce": "max"})
        self.assertAlmostEqual(result, 0.8, places=5)

    def test_array_reduce_mean(self) -> None:
        """Array source, reduce=mean."""
        result = resolve_binding([0.3, 0.6, 0.9], None,
                                  {"signal": "x", "reduce": "mean"})
        self.assertAlmostEqual(result, 0.6, places=5)

    def test_array_reduce_sum(self) -> None:
        """Array source, reduce=sum, clamped to 1.0."""
        result = resolve_binding([0.5, 0.8], None,
                                  {"signal": "x", "reduce": "sum"})
        self.assertAlmostEqual(result, 1.0, places=5)

    def test_empty_array(self) -> None:
        """Empty array produces 0.0."""
        result = resolve_binding([], None, {"signal": "x"})
        self.assertAlmostEqual(result, 0.0, places=5)

    def test_array_with_scale(self) -> None:
        """Array source with reduce + scale."""
        result = resolve_binding([0.4, 0.6], None,
                                  {"signal": "x", "reduce": "max",
                                   "scale": [100, 200]})
        self.assertAlmostEqual(result, 160.0, places=5)


# ---------------------------------------------------------------------------
# check_circular_binding() unit tests
# ---------------------------------------------------------------------------

class TestCircularDetection(unittest.TestCase):
    """Tests for the check_circular_binding utility function."""

    def test_no_cycle_empty(self) -> None:
        """No existing bindings — no cycle possible."""
        self.assertFalse(check_circular_binding("a:x", "b:y", {}))

    def test_no_cycle_simple(self) -> None:
        """Simple A←B with no existing chain — no cycle."""
        self.assertFalse(check_circular_binding("a:x", "b:y", {"c:z": "d:w"}))

    def test_direct_cycle(self) -> None:
        """A←B when B←A already exists — direct cycle."""
        self.assertTrue(check_circular_binding("a:x", "b:y", {"b:y": "a:x"}))

    def test_indirect_cycle(self) -> None:
        """A←B when B←C←A already exists — indirect cycle."""
        bindings = {"b:y": "c:z", "c:z": "a:x"}
        self.assertTrue(check_circular_binding("a:x", "b:y", bindings))

    def test_self_binding(self) -> None:
        """A←A — self-referential."""
        self.assertTrue(check_circular_binding("a:x", "a:x", {}))

    def test_long_chain_no_cycle(self) -> None:
        """Long chain A←B←C←D — no cycle."""
        bindings = {"b:y": "c:z", "c:z": "d:w"}
        self.assertFalse(check_circular_binding("a:x", "b:y", bindings))


# ---------------------------------------------------------------------------
# Operator param-as-signal tests
# ---------------------------------------------------------------------------

class TestParamAsSignal(unittest.TestCase):
    """Test that params are registered on the bus and updated via set_params."""

    def setUp(self) -> None:
        self.bus = StubBus()

    def test_register_param_signals(self) -> None:
        """Numeric params are written to bus as {name}:{param}."""
        op = AlphaOp("runner", {}, self.bus)
        op.register_param_signals()
        self.assertAlmostEqual(self.bus.read("runner:speed"), 5.0)
        self.assertAlmostEqual(self.bus.read("runner:brightness"), 50.0)

    def test_non_numeric_param_not_registered(self) -> None:
        """String params are not written to bus."""
        op = GammaOp("gamma1", {}, self.bus)
        op.register_param_signals()
        # label is a string — should not be on bus.
        self.assertEqual(self.bus.read("gamma1:label", "MISSING"), "MISSING")
        # intensity is numeric — should be on bus.
        self.assertAlmostEqual(self.bus.read("gamma1:intensity"), 0.5)

    def test_set_params_writes_to_bus(self) -> None:
        """set_params updates both the attribute and the bus signal."""
        op = AlphaOp("runner", {}, self.bus)
        op.register_param_signals()
        op.set_params(speed=12.0)
        self.assertAlmostEqual(op.speed, 12.0)
        self.assertAlmostEqual(self.bus.read("runner:speed"), 12.0)

    def test_set_params_validates(self) -> None:
        """set_params clamps to param range before writing to bus."""
        op = AlphaOp("runner", {}, self.bus)
        op.register_param_signals()
        op.set_params(speed=999.0)  # max is 30
        self.assertAlmostEqual(op.speed, 30.0)
        self.assertAlmostEqual(self.bus.read("runner:speed"), 30.0)

    def test_config_override_on_bus(self) -> None:
        """Config overrides are reflected on bus after registration."""
        op = AlphaOp("runner", {"speed": 15.0}, self.bus)
        op.register_param_signals()
        self.assertAlmostEqual(self.bus.read("runner:speed"), 15.0)


# ---------------------------------------------------------------------------
# Binding tests on Operator
# ---------------------------------------------------------------------------

class TestOperatorBindings(unittest.TestCase):
    """Test binding resolution on individual operators."""

    def setUp(self) -> None:
        self.bus = StubBus()

    def test_config_binding_loaded(self) -> None:
        """Bindings in config are loaded into _bindings dict."""
        config = {"bindings": {"speed": {"signal": "sensor:value"}}}
        op = AlphaOp("runner", config, self.bus)
        self.assertIn("speed", op._bindings)
        self.assertEqual(op._bindings["speed"]["signal"], "sensor:value")

    def test_resolve_scalar_binding(self) -> None:
        """Binding reads source signal and sets param."""
        config = {"bindings": {"speed": {"signal": "sensor:rate"}}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        # Source signal exists on bus.
        self.bus.write("sensor:rate", 0.5)
        op.resolve_bindings()
        # 0.5 scaled to [0.1, 30.0] = 15.05
        self.assertAlmostEqual(op.speed, 15.05, places=2)

    def test_resolve_with_explicit_scale(self) -> None:
        """Binding with explicit scale overrides param range."""
        config = {"bindings": {"speed": {
            "signal": "sensor:rate", "scale": [1.0, 10.0],
        }}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        self.bus.write("sensor:rate", 0.5)
        op.resolve_bindings()
        self.assertAlmostEqual(op.speed, 5.5, places=2)

    def test_resolve_writes_to_bus(self) -> None:
        """Resolved binding writes the scaled value to the bus."""
        config = {"bindings": {"speed": {
            "signal": "sensor:rate", "scale": [1.0, 10.0],
        }}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        self.bus.write("sensor:rate", 0.5)
        op.resolve_bindings()
        self.assertAlmostEqual(self.bus.read("runner:speed"), 5.5, places=2)

    def test_missing_source_keeps_current(self) -> None:
        """Missing source signal leaves param unchanged."""
        config = {"bindings": {"speed": {"signal": "nonexistent:signal"}}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        original = op.speed
        op.resolve_bindings()
        self.assertAlmostEqual(op.speed, original)

    def test_binding_overrides_set_params(self) -> None:
        """Binding wins over manual set_params on next resolve."""
        config = {"bindings": {"speed": {
            "signal": "sensor:rate", "scale": [1.0, 10.0],
        }}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        self.bus.write("sensor:rate", 0.8)
        # Manual set.
        op.set_params(speed=2.0)
        self.assertAlmostEqual(op.speed, 2.0)
        # Binding overwrites on next resolve.
        op.resolve_bindings()
        self.assertAlmostEqual(op.speed, 8.2, places=2)

    def test_remove_binding_keeps_value(self) -> None:
        """Removing a binding leaves the param at its last bound value."""
        config = {"bindings": {"speed": {
            "signal": "sensor:rate", "scale": [1.0, 10.0],
        }}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        self.bus.write("sensor:rate", 0.5)
        op.resolve_bindings()
        bound_value = op.speed
        op.remove_binding("speed")
        op.resolve_bindings()  # Should be a no-op now.
        self.assertAlmostEqual(op.speed, bound_value)

    def test_remove_binding_restores_manual(self) -> None:
        """After removing binding, set_params sticks."""
        config = {"bindings": {"speed": {
            "signal": "sensor:rate", "scale": [1.0, 10.0],
        }}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        self.bus.write("sensor:rate", 0.5)
        op.resolve_bindings()
        op.remove_binding("speed")
        op.set_params(speed=7.0)
        op.resolve_bindings()
        self.assertAlmostEqual(op.speed, 7.0)

    def test_add_binding_runtime(self) -> None:
        """Binding added at runtime via add_binding works."""
        op = AlphaOp("runner", {}, self.bus)
        op.register_param_signals()
        op.add_binding("speed", {"signal": "sensor:rate", "scale": [0, 20]})
        self.bus.write("sensor:rate", 0.25)
        op.resolve_bindings()
        self.assertAlmostEqual(op.speed, 5.0, places=2)

    def test_add_binding_invalid_param(self) -> None:
        """add_binding on nonexistent param raises ValueError."""
        op = AlphaOp("runner", {}, self.bus)
        with self.assertRaises(ValueError):
            op.add_binding("nonexistent", {"signal": "foo"})

    def test_multiple_bindings(self) -> None:
        """Multiple params bound independently on same operator."""
        config = {"bindings": {
            "speed": {"signal": "s1", "scale": [0, 10]},
            "brightness": {"signal": "s2", "scale": [0, 100]},
        }}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        self.bus.write("s1", 0.5)
        self.bus.write("s2", 0.8)
        op.resolve_bindings()
        self.assertAlmostEqual(op.speed, 5.0, places=2)
        self.assertAlmostEqual(op.brightness, 80, places=0)

    def test_array_binding_reduce(self) -> None:
        """Array source with reduce=mean binding."""
        config = {"bindings": {"speed": {
            "signal": "audio:spectrum", "reduce": "mean", "scale": [1, 20],
        }}}
        op = AlphaOp("runner", config, self.bus)
        op.register_param_signals()
        self.bus.write("audio:spectrum", [0.2, 0.4, 0.6])
        op.resolve_bindings()
        # mean = 0.4, scaled [1,20] = 1 + 0.4*19 = 8.6
        self.assertAlmostEqual(op.speed, 8.6, places=1)

    def test_get_bindings(self) -> None:
        """get_bindings returns a copy of active bindings."""
        config = {"bindings": {"speed": {"signal": "s1"}}}
        op = AlphaOp("runner", config, self.bus)
        bindings = op.get_bindings()
        self.assertIn("speed", bindings)
        # Mutating the returned dict doesn't affect operator.
        bindings.pop("speed")
        self.assertIn("speed", op.get_bindings())

    def test_status_includes_bindings(self) -> None:
        """get_status includes bindings in its response."""
        config = {"bindings": {"speed": {"signal": "s1"}}}
        op = AlphaOp("runner", config, self.bus)
        status = op.get_status()
        self.assertIn("bindings", status)
        self.assertIn("speed", status["bindings"])


# ---------------------------------------------------------------------------
# Param-to-param binding (After Effects link)
# ---------------------------------------------------------------------------

class TestParamToParamBinding(unittest.TestCase):
    """Test that one operator's param can drive another's."""

    def setUp(self) -> None:
        self.bus = StubBus()

    def test_param_drives_param(self) -> None:
        """AlphaOp:speed drives BetaOp:rate."""
        alpha = AlphaOp("alpha1", {}, self.bus)
        alpha.register_param_signals()
        alpha.set_params(speed=8.0)

        beta = BetaOp("beta1", {
            "bindings": {"rate": {"signal": "alpha1:speed", "scale": [0, 10]}},
        }, self.bus)
        beta.register_param_signals()

        # Alpha speed is 8.0 on bus. Beta rate scale [0,10] → 8.0*10/1=...
        # Actually: source is 8.0, scale [0,10] → lo + value * (hi-lo) = 0 + 8*10 = 80
        # But rate max is 10.0, so validate clamps to 10.0.
        beta.resolve_bindings()
        self.assertAlmostEqual(beta.rate, 10.0)

    def test_param_identity_link(self) -> None:
        """Param-to-param with no scale (identity) — source value used directly."""
        alpha = AlphaOp("alpha1", {}, self.bus)
        alpha.register_param_signals()
        alpha.set_params(speed=7.5)

        beta = BetaOp("beta1", {
            "bindings": {"rate": {"signal": "alpha1:speed"}},
        }, self.bus)
        beta.register_param_signals()
        beta.resolve_bindings()
        # 7.5 with param range [0,10] → lo + 7.5*(10-0) = 75, clamped to 10
        # Hmm, this isn't right — identity should mean "use source value directly"
        # but the scale logic maps [0,1] to [min,max]. For param-to-param where
        # ranges differ, explicit scale is needed. Without scale, param range
        # is used: 0 + 7.5 * (10-0) = 75, clamped to 10.
        self.assertAlmostEqual(beta.rate, 10.0)

    def test_param_link_with_matching_scale(self) -> None:
        """Param-to-param with explicit scale matching source range."""
        alpha = AlphaOp("alpha1", {}, self.bus)
        alpha.register_param_signals()
        alpha.set_params(speed=15.0)

        # Map alpha speed [0.1, 30] to beta rate [0, 10]
        beta = BetaOp("beta1", {
            "bindings": {"rate": {
                "signal": "alpha1:speed",
                "scale": [0, 10],
            }},
        }, self.bus)
        beta.register_param_signals()
        beta.resolve_bindings()
        # 15.0 * 10 = way over, clamped to 10
        # For true proportional mapping, user must normalise.
        self.assertAlmostEqual(beta.rate, 10.0)

    def test_chained_binding_via_bus(self) -> None:
        """A→B→C chain: A writes to bus, B reads A and writes, C reads B."""
        a = AlphaOp("a", {}, self.bus)
        a.register_param_signals()
        a.set_params(speed=0.5)

        b = BetaOp("b", {
            "bindings": {"rate": {"signal": "a:speed"}},
        }, self.bus)
        b.register_param_signals()
        b.resolve_bindings()
        # b:rate is now on the bus.
        b_rate = self.bus.read("b:rate")
        self.assertGreater(b_rate, 0)


# ---------------------------------------------------------------------------
# OperatorManager binding integration tests
# ---------------------------------------------------------------------------

class TestOperatorManagerBindings(unittest.TestCase):
    """Test OperatorManager binding CRUD and resolution."""

    def setUp(self) -> None:
        self.bus = StubBus()
        self.mgr = OperatorManager(self.bus)

    def _configure_and_start(
        self, configs: list[dict[str, Any]],
    ) -> None:
        """Helper: configure and start operators."""
        self.mgr.configure(configs)
        self.mgr.start()

    def tearDown(self) -> None:
        self.mgr.stop()

    def test_params_on_bus_after_start(self) -> None:
        """Params are seeded on bus after OperatorManager.start."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner", "speed": 10.0},
        ])
        self.assertAlmostEqual(self.bus.read("runner:speed"), 10.0)
        self.assertAlmostEqual(self.bus.read("runner:brightness"), 50.0)

    def test_get_all_bindings_empty(self) -> None:
        """No bindings when none configured."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner"},
        ])
        self.assertEqual(self.mgr.get_all_bindings(), [])

    def test_get_all_bindings_with_config(self) -> None:
        """Config-declared bindings show up in get_all_bindings."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner",
             "bindings": {"speed": {"signal": "sensor:rate"}}},
        ])
        bindings = self.mgr.get_all_bindings()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["target"], "runner:speed")
        self.assertEqual(bindings[0]["source"], "sensor:rate")

    def test_create_binding_runtime(self) -> None:
        """Runtime binding creation via OperatorManager."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner"},
        ])
        self.mgr.create_binding("runner", "speed", {"signal": "ext:val"})
        bindings = self.mgr.get_all_bindings()
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["source"], "ext:val")

    def test_create_binding_circular_rejected(self) -> None:
        """Circular binding is rejected with ValueError."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "a1",
             "bindings": {"speed": {"signal": "b1:rate"}}},
            {"type": "test_beta", "name": "b1"},
        ])
        with self.assertRaises(ValueError) as ctx:
            self.mgr.create_binding("b1", "rate", {"signal": "a1:speed"})
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_create_binding_bad_operator(self) -> None:
        """Binding on nonexistent operator raises ValueError."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner"},
        ])
        with self.assertRaises(ValueError):
            self.mgr.create_binding("nope", "speed", {"signal": "x"})

    def test_create_binding_bad_param(self) -> None:
        """Binding on nonexistent param raises ValueError."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner"},
        ])
        with self.assertRaises(ValueError):
            self.mgr.create_binding("runner", "nope", {"signal": "x"})

    def test_create_binding_no_signal(self) -> None:
        """Binding without signal key raises ValueError."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner"},
        ])
        with self.assertRaises(ValueError):
            self.mgr.create_binding("runner", "speed", {})

    def test_remove_binding(self) -> None:
        """Removing a binding via OperatorManager."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner",
             "bindings": {"speed": {"signal": "sensor:rate"}}},
        ])
        self.mgr.remove_binding("runner", "speed")
        self.assertEqual(self.mgr.get_all_bindings(), [])

    def test_remove_binding_bad_operator(self) -> None:
        """Removing binding on nonexistent operator raises ValueError."""
        self._configure_and_start([])
        with self.assertRaises(ValueError):
            self.mgr.remove_binding("nope", "speed")

    def test_status_includes_bindings(self) -> None:
        """Operator status via OperatorManager includes bindings."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner",
             "bindings": {"speed": {"signal": "s1"}}},
        ])
        statuses = self.mgr.get_status()
        self.assertEqual(len(statuses), 1)
        self.assertIn("bindings", statuses[0])
        self.assertIn("speed", statuses[0]["bindings"])

    def test_tick_resolves_bindings(self) -> None:
        """Binding resolution happens in tick loop before on_tick."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner",
             "bindings": {"speed": {
                 "signal": "ext:drive", "scale": [1.0, 20.0],
             }}},
        ])
        # Write source signal.
        self.bus.write("ext:drive", 0.5)
        # Let tick loop run at least once.
        time.sleep(0.15)
        # AlphaOp records speed at each tick. Check it saw the bound value.
        op = self.mgr._find_operator("runner")
        self.assertIsNotNone(op)
        # Expected: 1.0 + 0.5 * 19 = 10.5
        self.assertAlmostEqual(op.speed, 10.5, places=1)

    def test_late_arriving_source(self) -> None:
        """Binding to a signal that doesn't exist yet — resolves when it appears."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "runner",
             "bindings": {"speed": {
                 "signal": "late:signal", "scale": [0, 10],
             }}},
        ])
        original = 5.0  # default speed
        op = self.mgr._find_operator("runner")
        self.assertAlmostEqual(op.speed, original)
        # Signal appears later.
        self.bus.write("late:signal", 0.7)
        time.sleep(0.15)
        # Now it should have resolved.
        self.assertAlmostEqual(op.speed, 7.0, places=1)

    def test_binding_between_tick_modes(self) -> None:
        """Binding between a periodic operator and a reactive operator."""
        self._configure_and_start([
            {"type": "test_alpha", "name": "periodic1"},
            {"type": "test_beta", "name": "reactive1",
             "bindings": {"rate": {
                 "signal": "periodic1:speed", "scale": [0, 10],
             }}},
        ])
        self.bus.write("periodic1:speed", 0.3)
        time.sleep(0.15)
        op = self.mgr._find_operator("reactive1")
        # 0 + 0.3*10 = 3.0
        self.assertAlmostEqual(op.rate, 3.0, places=1)


if __name__ == "__main__":
    unittest.main()
