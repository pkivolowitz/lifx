#!/usr/bin/env python3
"""Concurrency stress tests — verify thread safety under contention.

The C/H/M audit fixed ~20 threading bugs (locks, copy-before-mutate,
TOCTOU races, etc.).  These tests verify those fixes hold under real
multi-thread contention.  Each test spawns multiple worker threads that
hammer shared state simultaneously and asserts no deadlocks, crashes,
data corruption, or lost updates.

**When to run:**
    Before any public push or release.  These tests use real threads
    and real locks — they are slower than pure unit tests (~5-15 seconds)
    and are NOT part of the pre-commit hook.

**Run:**
    python3 -m pytest test_concurrency.py -v --tb=short

**Configuration:**
    Override iteration counts with ``GLOWUP_STRESS_ITERATIONS``::

        GLOWUP_STRESS_ITERATIONS=5000 python3 -m pytest test_concurrency.py -v

Targets:
    - DeviceManager: overrides, nicknames, power states, group config
    - _save_config_field: concurrent config saves on different keys
    - Param.validate: concurrent validation from multiple threads
    - PulseDetector: concurrent feed() calls
    - device_registry: concurrent load/save
    - automation.validate_automation: concurrent validation
    - scheduler GroupState: concurrent proc tracking
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import os
import random
import string
import struct
import tempfile
import threading
import time
import unittest
from typing import Any, Optional
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default iterations per stress loop.
STRESS_ITERATIONS: int = int(
    os.environ.get("GLOWUP_STRESS_ITERATIONS", "1000")
)

# Number of worker threads per test.
NUM_WORKERS: int = 10

# Maximum time (seconds) to wait for all workers to finish.
# If exceeded, the test fails with a deadlock diagnosis.
DEADLOCK_TIMEOUT: float = 30.0

# Seed for reproducibility.
STRESS_SEED: int = int(os.environ.get("GLOWUP_STRESS_SEED", "42"))

# Simulated device IPs for stress testing.
DEVICE_IPS: list[str] = [f"10.0.0.{i}" for i in range(50, 66)]

# Group config for stress testing.
GROUP_CONFIG: dict[str, list[str]] = {
    "porch": DEVICE_IPS[:4],
    "bedroom": DEVICE_IPS[4:8],
    "living_room": DEVICE_IPS[8:12],
    "kitchen": DEVICE_IPS[12:],
}


def _run_workers(
    target: Any,
    num_workers: int = NUM_WORKERS,
    timeout: float = DEADLOCK_TIMEOUT,
) -> list[Exception]:
    """Spawn worker threads, wait for completion, collect errors.

    Args:
        target:      Callable for each thread (no args).
        num_workers: Number of threads to spawn.
        timeout:     Maximum wait time before declaring deadlock.

    Returns:
        List of exceptions raised by worker threads (empty = success).
    """
    errors: list[Exception] = []
    lock: threading.Lock = threading.Lock()

    def wrapper() -> None:
        try:
            target()
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads: list[threading.Thread] = [
        threading.Thread(target=wrapper, daemon=True)
        for _ in range(num_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        if t.is_alive():
            with lock:
                errors.append(
                    TimeoutError(f"Thread {t.name} deadlocked (>{timeout}s)")
                )
    return errors


# ===================================================================
# DeviceManager stress tests
# ===================================================================

class TestStressDeviceManagerOverrides(unittest.TestCase):
    """Concurrent mark/clear/check override operations.

    Verifies that _overrides dict remains consistent under contention
    from multiple threads doing mark, clear, and is_overridden
    simultaneously.
    """

    def test_concurrent_override_operations(self) -> None:
        """No crashes, deadlocks, or KeyError from concurrent overrides."""
        from server import DeviceManager

        dm = DeviceManager(
            device_ips=DEVICE_IPS,
            groups=GROUP_CONFIG,
        )

        random.seed(STRESS_SEED)

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            for _ in range(STRESS_ITERATIONS):
                ip: str = rng.choice(DEVICE_IPS)
                op: int = rng.randint(0, 3)
                if op == 0:
                    dm.mark_override(ip, f"entry_{rng.randint(0, 10)}")
                elif op == 1:
                    dm.clear_override(ip)
                elif op == 2:
                    dm.is_overridden(ip)
                else:
                    dm.is_overridden_or_member(f"group:{rng.choice(list(GROUP_CONFIG.keys()))}")

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"Override stress errors: {errors}")


class TestStressDeviceManagerNicknames(unittest.TestCase):
    """Concurrent nickname set/get operations."""

    def test_concurrent_nickname_operations(self) -> None:
        """No crashes or data corruption from concurrent nickname writes."""
        from server import DeviceManager

        dm = DeviceManager(
            device_ips=DEVICE_IPS,
            groups=GROUP_CONFIG,
        )

        random.seed(STRESS_SEED)

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            for _ in range(STRESS_ITERATIONS):
                ip: str = rng.choice(DEVICE_IPS)
                if rng.random() < 0.5:
                    dm.set_nickname(ip, f"nick_{rng.randint(0, 100)}")
                else:
                    dm.get_nickname(ip)

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"Nickname stress errors: {errors}")


class TestStressDeviceManagerPowerStates(unittest.TestCase):
    """Concurrent power state read/write operations."""

    def test_concurrent_power_state_operations(self) -> None:
        """No crashes from concurrent power state mutations."""
        from server import DeviceManager

        dm = DeviceManager(
            device_ips=DEVICE_IPS,
            groups=GROUP_CONFIG,
        )

        random.seed(STRESS_SEED)

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            for _ in range(STRESS_ITERATIONS):
                ip: str = rng.choice(DEVICE_IPS)
                if rng.random() < 0.5:
                    with dm._lock:
                        dm._power_states[ip] = rng.choice([True, False])
                else:
                    with dm._lock:
                        _ = dm._power_states.get(ip)

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"Power state stress errors: {errors}")


class TestStressDeviceManagerGroupConfig(unittest.TestCase):
    """Concurrent group config read/write operations.

    Simulates scheduler reading group config while API modifies it.
    """

    def test_concurrent_group_config_access(self) -> None:
        """No crashes from concurrent group config reads and writes."""
        from server import DeviceManager

        dm = DeviceManager(
            device_ips=DEVICE_IPS,
            groups=dict(GROUP_CONFIG),
        )

        random.seed(STRESS_SEED)
        group_names: list[str] = list(GROUP_CONFIG.keys())

        def reader() -> None:
            """Simulates scheduler polling group config."""
            for _ in range(STRESS_ITERATIONS):
                with dm._lock:
                    snapshot = dict(dm._group_config)
                # Use the snapshot outside the lock.
                for name, ips in snapshot.items():
                    _ = len(ips)

        def writer() -> None:
            """Simulates API modifying group config."""
            rng = random.Random(threading.current_thread().ident)
            for _ in range(STRESS_ITERATIONS):
                with dm._lock:
                    name: str = rng.choice(group_names)
                    dm._group_config[name] = list(dm._group_config[name])

        errors: list[Exception] = []
        lock = threading.Lock()

        def wrap(fn: Any) -> Any:
            def inner() -> None:
                try:
                    fn()
                except Exception as exc:
                    with lock:
                        errors.append(exc)
            return inner

        threads = (
            [threading.Thread(target=wrap(reader), daemon=True)
             for _ in range(NUM_WORKERS // 2)]
            + [threading.Thread(target=wrap(writer), daemon=True)
               for _ in range(NUM_WORKERS // 2)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=DEADLOCK_TIMEOUT)
            if t.is_alive():
                errors.append(TimeoutError(f"Deadlock in {t.name}"))

        self.assertEqual(errors, [], f"Group config stress errors: {errors}")


# ===================================================================
# Config save stress tests
# ===================================================================

class TestStressConfigSave(unittest.TestCase):
    """Concurrent _save_config_field operations on different keys.

    The C7 fix added _config_save_lock to serialize config file writes.
    This test verifies that concurrent saves on different keys don't
    clobber each other or deadlock.
    """

    def test_concurrent_config_saves(self) -> None:
        """Config file remains valid JSON after concurrent saves."""
        from server import GlowUpRequestHandler

        # Create a temp config file.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump({
                "key_a": 0, "key_b": 0, "key_c": 0,
                "location": {"latitude": 30.0, "longitude": -88.0},
            }, f)
            config_path: str = f.name

        try:
            # Set up handler class attributes.
            with open(config_path, "r") as f:
                config: dict = json.load(f)
            GlowUpRequestHandler.config = config
            GlowUpRequestHandler.config_path = config_path

            # Create a mock handler instance for _save_config_field.
            handler = GlowUpRequestHandler.__new__(GlowUpRequestHandler)

            random.seed(STRESS_SEED)
            keys: list[str] = ["key_a", "key_b", "key_c"]

            def worker() -> None:
                rng = random.Random(threading.current_thread().ident)
                for i in range(STRESS_ITERATIONS // 10):
                    key: str = rng.choice(keys)
                    handler._save_config_field(key, i)

            errors = _run_workers(worker)
            self.assertEqual(errors, [], f"Config save errors: {errors}")

            # Verify config file is still valid JSON.
            with open(config_path, "r") as f:
                final: dict = json.load(f)
            self.assertIsInstance(final, dict)
            for key in keys:
                self.assertIn(key, final)
        finally:
            os.unlink(config_path)


# ===================================================================
# Param.validate stress tests
# ===================================================================

class TestStressParamValidate(unittest.TestCase):
    """Concurrent Param.validate() calls.

    Param instances are shared across threads (class-level attributes
    on Effect subclasses).  This verifies that concurrent validation
    doesn't corrupt state.
    """

    def test_concurrent_int_param_validation(self) -> None:
        """Concurrent int param validation produces correct results."""
        from effects import Param

        p = Param(50, min=0, max=100)
        results: list[int] = []
        results_lock: threading.Lock = threading.Lock()

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            local_results: list[int] = []
            for _ in range(STRESS_ITERATIONS):
                value = rng.choice([
                    rng.randint(-1000, 1000),
                    rng.uniform(-100.0, 200.0),
                    "garbage",
                    None,
                    float("inf"),
                ])
                result = p.validate(value)
                local_results.append(result)
                # Every result must be an int in [0, 100].
                assert isinstance(result, int), f"Not int: {result!r}"
                assert 0 <= result <= 100, f"Out of range: {result}"
            with results_lock:
                results.extend(local_results)

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"Param validate errors: {errors}")
        self.assertEqual(
            len(results), NUM_WORKERS * STRESS_ITERATIONS,
            "Lost results — thread safety issue",
        )


# ===================================================================
# PulseDetector stress tests
# ===================================================================

class TestStressPulseDetector(unittest.TestCase):
    """Concurrent PulseDetector.feed() calls.

    The M28 fix added a lock to PulseDetector.  This test verifies
    it holds under contention.
    """

    def test_concurrent_feed(self) -> None:
        """Concurrent feed() calls don't corrupt detection state."""
        from media.calibration import PulseDetector

        det = PulseDetector(sample_rate=44100, threshold=0.3)

        # Pre-generate a silent chunk and a loud chunk.
        silent: bytes = struct.pack("<" + "h" * 512, *([0] * 512))
        loud: bytes = struct.pack(
            "<" + "h" * 512,
            *([30000 if i % 2 == 0 else -30000 for i in range(512)]),
        )

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            for _ in range(STRESS_ITERATIONS):
                chunk: bytes = rng.choice([silent, loud])
                det.feed(chunk)
                # Also read detections concurrently.
                _ = det.detections
                _ = det.detection_count

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"PulseDetector stress errors: {errors}")

        # Verify state is internally consistent.
        count: int = det.detection_count
        detections: list[float] = det.detections
        self.assertEqual(len(detections), count)


# ===================================================================
# DeviceRegistry stress tests
# ===================================================================

class TestStressDeviceRegistry(unittest.TestCase):
    """Concurrent load/save operations on DeviceRegistry.

    The M4/M5 fix added _io_lock.  This test verifies concurrent
    save() calls don't corrupt the file.
    """

    def test_concurrent_save(self) -> None:
        """Concurrent save() calls produce valid JSON."""
        from device_registry import DeviceRegistry

        reg = DeviceRegistry()

        # Create a temp registry file.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump({"devices": {}}, f)
            reg_path: str = f.name

        try:
            reg.load(reg_path)

            # Pre-populate with some devices.
            for i in range(5):
                mac: str = f"d0:73:d5:00:00:{i:02x}"
                reg.add_device(mac, f"device-{i}")

            def worker() -> None:
                for _ in range(STRESS_ITERATIONS // 10):
                    reg.save()

            errors = _run_workers(worker, num_workers=5)
            self.assertEqual(errors, [], f"Registry save errors: {errors}")

            # Verify file is valid JSON with all devices.
            with open(reg_path, "r") as f:
                data: dict = json.load(f)
            self.assertIn("devices", data)
            self.assertEqual(len(data["devices"]), 5)
        finally:
            os.unlink(reg_path)


# ===================================================================
# Automation validation stress tests
# ===================================================================

class TestStressAutomationValidate(unittest.TestCase):
    """Concurrent validate_automation() calls.

    Verifies the validator is stateless and thread-safe.
    """

    def test_concurrent_validation(self) -> None:
        """Concurrent validation calls don't interfere with each other."""
        from automation import validate_automation

        known_groups: set[str] = {"porch", "bedroom", "living_room"}
        known_effects: set[str] = {"on", "off", "cylon", "aurora"}
        media_effects: set[str] = {"spectrum", "waveform"}

        valid_entry: dict = {
            "name": "test",
            "sensor": {"type": "ble", "label": "m", "characteristic": "motion"},
            "trigger": {"condition": "eq", "value": 1},
            "action": {"group": "porch", "effect": "on"},
            "off_trigger": {"type": "watchdog", "minutes": 30},
            "off_action": {"effect": "off"},
            "schedule_conflict": "defer",
        }
        invalid_entry: dict = {"name": "", "sensor": 42}

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            for _ in range(STRESS_ITERATIONS):
                entry = rng.choice([valid_entry, invalid_entry])
                errors = validate_automation(
                    entry, known_groups, known_effects, media_effects,
                )
                assert isinstance(errors, list)
                if entry is valid_entry:
                    assert errors == [], f"Valid entry got errors: {errors}"

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"Validation stress errors: {errors}")


# ===================================================================
# Scheduler GroupState stress tests
# ===================================================================

class TestStressSchedulerGroupState(unittest.TestCase):
    """Concurrent access to scheduler's IP-keyed procs dict.

    The M14 fix changed procs from list to dict. This verifies
    the dict survives concurrent reads and writes.
    """

    def test_concurrent_proc_tracking(self) -> None:
        """Concurrent dict operations on procs don't crash."""
        from scheduler import GroupState

        state = GroupState()
        ips: list[str] = DEVICE_IPS[:4]

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            for _ in range(STRESS_ITERATIONS):
                ip: str = rng.choice(ips)
                op: int = rng.randint(0, 3)
                if op == 0:
                    state.procs[ip] = MagicMock()
                elif op == 1:
                    state.procs.pop(ip, None)
                elif op == 2:
                    _ = list(state.procs.items())
                else:
                    _ = any(
                        p.poll() is not None
                        for p in list(state.procs.values())
                    )

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"GroupState stress errors: {errors}")


# ===================================================================
# Mixed workload — the big one
# ===================================================================

class TestStressMixedWorkload(unittest.TestCase):
    """Simulate realistic concurrent access patterns.

    Multiple threads performing different operations simultaneously:
    scheduler reading config, API writing config, overrides toggling,
    nicknames changing, power states flipping.  This is the closest
    approximation to production contention.
    """

    def test_mixed_concurrent_operations(self) -> None:
        """No deadlocks or crashes under mixed workload."""
        from server import DeviceManager

        dm = DeviceManager(
            device_ips=DEVICE_IPS,
            groups=dict(GROUP_CONFIG),
        )

        random.seed(STRESS_SEED)
        barrier = threading.Barrier(NUM_WORKERS)

        def worker() -> None:
            rng = random.Random(threading.current_thread().ident)
            # Wait for all threads to be ready — maximizes contention.
            barrier.wait()
            for _ in range(STRESS_ITERATIONS):
                ip: str = rng.choice(DEVICE_IPS)
                group: str = rng.choice(list(GROUP_CONFIG.keys()))
                op: int = rng.randint(0, 7)
                if op == 0:
                    dm.mark_override(ip, "entry")
                elif op == 1:
                    dm.clear_override(ip)
                elif op == 2:
                    dm.is_overridden(ip)
                elif op == 3:
                    dm.is_overridden_or_member(f"group:{group}")
                elif op == 4:
                    dm.set_nickname(ip, f"n{rng.randint(0, 50)}")
                elif op == 5:
                    dm.get_nickname(ip)
                elif op == 6:
                    with dm._lock:
                        dm._power_states[ip] = rng.choice([True, False])
                else:
                    with dm._lock:
                        _ = dict(dm._group_config)

        errors = _run_workers(worker)
        self.assertEqual(errors, [], f"Mixed workload errors: {errors}")


if __name__ == "__main__":
    unittest.main()
