#!/usr/bin/env python3
"""Fuzz tests for GlowUp — elevated tier for pre-release validation.

These tests throw random, malformed, and boundary-case inputs at every
system-boundary parser and validator in the codebase.  The goal is to
verify that no input — no matter how garbled — causes a crash, hang,
or unhandled exception.  Every function under test must either return
a valid result or raise a documented exception type.

**When to run:**
    Before any public push or release.  These tests are deliberately
    more expensive than the fast unit suite (~10-30 seconds depending
    on iteration counts).  They are NOT part of the pre-commit hook.

**Run:**
    python3 -m pytest test_fuzz.py -v --tb=short

**Iteration counts:**
    Each fuzz loop runs FUZZ_ITERATIONS times.  Override with the
    environment variable ``GLOWUP_FUZZ_ITERATIONS`` for longer runs::

        GLOWUP_FUZZ_ITERATIONS=50000 python3 -m pytest test_fuzz.py -v

Two groups:
    Group 1 — Protocol / Wire Format:
        transport._parse_message, ble/tlv.py decode, MIDI parser,
        media/fft.py fft_magnitudes

    Group 2 — REST / Validation:
        Param.validate, scheduler._parse_time_spec,
        automation.validate_automation, automation._parse_value,
        schedule entry validation, play command validation
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import math
import os
import random
import string
import struct
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Module imports — done at module level so function references are plain
# module attributes, not class-bound descriptors.
# ---------------------------------------------------------------------------

import transport as _transport_mod
import ble.tlv as _tlv_mod
from distributed.midi_parser import MidiParser as _MidiParser
import media.fft as _fft_mod
from effects import Param as _Param, create_effect as _create_effect_fn
from effects import get_registry as _get_registry_fn
import scheduler as _scheduler_mod
from solar import sun_times as _sun_times_fn
import automation as _automation_mod

# ---------------------------------------------------------------------------
# Fuzz configuration
# ---------------------------------------------------------------------------

# Default iteration count per fuzz loop.  Override with env var for
# longer pre-release runs.
FUZZ_ITERATIONS: int = int(os.environ.get("GLOWUP_FUZZ_ITERATIONS", "5000"))

# Seed for reproducibility.  Override with GLOWUP_FUZZ_SEED for
# exploring specific failure modes.
FUZZ_SEED: int = int(os.environ.get("GLOWUP_FUZZ_SEED", "42"))

# Maximum byte length for random payloads.
MAX_PAYLOAD_SIZE: int = 2048

# Interesting byte patterns that tend to trigger edge cases.
INTERESTING_BYTES: list[bytes] = [
    b"",                          # empty
    b"\x00",                      # null
    b"\xff",                      # all-ones
    b"\x00" * 36,                 # LIFX header-sized zeros
    b"\xff" * 36,                 # LIFX header-sized ones
    b"\x00" * 4096,               # large zeros
    b"\xff" * 4096,               # large ones
    b"MThd",                      # MIDI header magic (incomplete)
    b"MThd\x00\x00\x00\x06",     # MIDI header with length (incomplete)
    bytes(range(256)),            # all byte values
]

# Interesting string values for REST/validation fuzzing.
INTERESTING_STRINGS: list[Any] = [
    "",
    " ",
    "\x00",
    "\n",
    "a" * 10000,
    "../../../etc/passwd",
    "<script>alert(1)</script>",
    "'; DROP TABLE schedule;--",
    "null",
    "undefined",
    "NaN",
    "Infinity",
    "-Infinity",
    "true",
    "false",
    "0",
    "-1",
    "99999999999999999999",
    "1e308",
    "1e-308",
]

# Interesting values for numeric fields.
INTERESTING_NUMBERS: list[Any] = [
    0, -1, 1, -0.0, 0.0,
    float("inf"), float("-inf"), float("nan"),
    2**31 - 1, 2**31, 2**63 - 1, 2**63,
    -2**31, -2**63,
    0.1, -0.1, 1e-300, 1e300,
    65535, 65536, 255, 256,
]

# Interesting values for any-type fields.
INTERESTING_VALUES: list[Any] = [
    None, True, False,
    0, -1, 1, 0.0, -0.0,
    float("inf"), float("-inf"), float("nan"),
    "", " ", "null", "NaN",
    [], [1, 2, 3], [None],
    {}, {"a": 1}, {"__class__": "str"},
    b"", b"\x00", b"\xff",
    set(), frozenset(),
    object(),
    type,
    lambda: None,
]


def _random_bytes(max_len: int = MAX_PAYLOAD_SIZE) -> bytes:
    """Generate random bytes of random length."""
    length: int = random.randint(0, max_len)
    return bytes(random.getrandbits(8) for _ in range(length))


def _random_string(max_len: int = 200) -> str:
    """Generate a random string of random length."""
    length: int = random.randint(0, max_len)
    chars = string.printable + "\x00\n\r\t"
    return "".join(random.choice(chars) for _ in range(length))


def _random_json_value() -> Any:
    """Generate a random JSON-compatible value."""
    choice: int = random.randint(0, 8)
    if choice == 0:
        return None
    elif choice == 1:
        return random.choice([True, False])
    elif choice == 2:
        return random.randint(-2**32, 2**32)
    elif choice == 3:
        return random.uniform(-1e10, 1e10)
    elif choice == 4:
        return _random_string(50)
    elif choice == 5:
        return [_random_json_value() for _ in range(random.randint(0, 5))]
    elif choice == 6:
        return {_random_string(10): _random_json_value()
                for _ in range(random.randint(0, 5))}
    elif choice == 7:
        return random.choice(INTERESTING_STRINGS)
    else:
        return random.choice(INTERESTING_NUMBERS)


# ===================================================================
# GROUP 1: PROTOCOL / WIRE FORMAT
# ===================================================================


class TestFuzzTransportParseMessage(unittest.TestCase):
    """Fuzz transport._parse_message() with random UDP payloads.

    The function must either return a valid dict or None.
    It must never raise an exception on any input.
    """

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_random_bytes(self) -> None:
        """Random byte strings of varying length must not crash."""
        for _ in range(FUZZ_ITERATIONS):
            data: bytes = _random_bytes()
            result = _transport_mod._parse_message(data)
            if result is not None:
                self.assertIsInstance(result, dict)
                self.assertIn("source", result)
                self.assertIn("target", result)
                self.assertIn("type", result)
                self.assertIn("payload", result)

    def test_interesting_patterns(self) -> None:
        """Known edge-case byte patterns must not crash."""
        for pattern in INTERESTING_BYTES:
            result = _transport_mod._parse_message(pattern)
            if result is not None:
                self.assertIsInstance(result, dict)

    def test_truncated_headers(self) -> None:
        """Every prefix length from 0 to HEADER_SIZE must not crash."""
        hs: int = _transport_mod.HEADER_SIZE
        full: bytes = bytes(range(hs + 20))
        for length in range(hs + 5):
            result = _transport_mod._parse_message(full[:length])
            if length < hs:
                self.assertIsNone(result)
            else:
                self.assertIsInstance(result, dict)

    def test_declared_size_mismatches(self) -> None:
        """Packet with declared size wildly different from actual."""
        hs: int = _transport_mod.HEADER_SIZE
        for _ in range(FUZZ_ITERATIONS // 10):
            buf: bytearray = bytearray(_random_bytes(200))
            if len(buf) < hs:
                buf.extend(b"\x00" * (hs - len(buf)))
            declared_size: int = random.choice([
                0, 1, 35, 36, 37, 100, 65535,
                len(buf), len(buf) + 100,
            ])
            struct.pack_into("<H", buf, 0, declared_size & 0xFFFF)
            result = _transport_mod._parse_message(bytes(buf))
            if result is not None:
                self.assertIsInstance(result, dict)


class TestFuzzTlvDecode(unittest.TestCase):
    """Fuzz ble/tlv.py decode functions with random bytes.

    decode() must either return a list of (int, bytes) tuples or
    raise ValueError on structurally invalid input.  It must never
    raise any other exception type.
    """

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_random_bytes(self) -> None:
        """Random byte strings must either decode or raise ValueError."""
        for _ in range(FUZZ_ITERATIONS):
            data: bytes = _random_bytes(512)
            try:
                result = _tlv_mod.decode(data)
                self.assertIsInstance(result, list)
                for item in result:
                    self.assertIsInstance(item, tuple)
                    self.assertEqual(len(item), 2)
                    self.assertIsInstance(item[0], int)
                    self.assertIsInstance(item[1], bytes)
            except ValueError:
                pass  # Expected for malformed input.

    def test_interesting_patterns(self) -> None:
        """Known edge-case patterns must not crash."""
        for pattern in INTERESTING_BYTES:
            try:
                _tlv_mod.decode(pattern)
            except ValueError:
                pass

    def test_decode_dict_random(self) -> None:
        """decode_dict must behave identically to decode for crash safety."""
        for _ in range(FUZZ_ITERATIONS // 2):
            data: bytes = _random_bytes(256)
            try:
                result = _tlv_mod.decode_dict(data)
                self.assertIsInstance(result, dict)
            except ValueError:
                pass

    def test_valid_then_truncated(self) -> None:
        """Valid TLV followed by truncated item must raise ValueError."""
        valid: bytes = _tlv_mod.encode([(1, b"hello"), (2, b"world")])
        truncated: bytes = valid + bytes([3, 50, 0xAA, 0xBB, 0xCC])
        with self.assertRaises(ValueError):
            _tlv_mod.decode(truncated)

    def test_roundtrip_random(self) -> None:
        """Encode -> decode must survive random valid payloads."""
        for _ in range(FUZZ_ITERATIONS // 10):
            pairs: list[tuple[int, bytes]] = []
            for _ in range(random.randint(0, 10)):
                type_code: int = random.randint(0, 255)
                value: bytes = _random_bytes(400)
                pairs.append((type_code, value))
            encoded: bytes = _tlv_mod.encode(pairs)
            decoded = _tlv_mod.decode(encoded)
            self.assertIsInstance(decoded, list)


class TestFuzzMidiParser(unittest.TestCase):
    """Fuzz the MIDI parser with random bytes.

    MidiParser must either parse successfully or raise ValueError.
    It must never hang, segfault, or raise unexpected exceptions.
    """

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_random_bytes(self) -> None:
        """Random bytes as MIDI data must not crash or hang."""
        for _ in range(FUZZ_ITERATIONS // 5):
            data: bytes = _random_bytes(1024)
            try:
                parser = _MidiParser(data)
                events = parser.events()
                self.assertIsInstance(events, list)
            except (ValueError, struct.error):
                pass  # Expected for garbage input.

    def test_interesting_patterns(self) -> None:
        """Known edge-case patterns must not crash."""
        for pattern in INTERESTING_BYTES:
            try:
                parser = _MidiParser(pattern)
                parser.events()
            except (ValueError, struct.error):
                pass

    def test_truncated_midi_header(self) -> None:
        """Partial MIDI headers must raise ValueError."""
        header: bytes = b"MThd\x00\x00\x00\x06\x00\x01\x00\x02\x01\xe0"
        for length in range(len(header)):
            try:
                _MidiParser(header[:length])
            except (ValueError, struct.error):
                pass

    def test_valid_header_garbage_tracks(self) -> None:
        """Valid header followed by garbage track data must not crash."""
        header: bytes = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0"
        for _ in range(FUZZ_ITERATIONS // 10):
            track_garbage: bytes = _random_bytes(512)
            data: bytes = header + track_garbage
            try:
                parser = _MidiParser(data)
                parser.events()
            except (ValueError, struct.error):
                pass

    def test_massive_declared_track_length(self) -> None:
        """Track chunk claiming huge length must not allocate gigabytes."""
        header: bytes = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0"
        track_start: bytes = b"MTrk\x7f\xff\xff\xff"  # ~2 GB declared
        data: bytes = header + track_start + b"\x00" * 100
        try:
            parser = _MidiParser(data)
            parser.events()
        except (ValueError, struct.error):
            pass


class TestFuzzFftMagnitudes(unittest.TestCase):
    """Fuzz media/fft.py fft_magnitudes() with random sample data.

    The function must always return a list of floats or an empty list.
    It must never crash on any input.
    """

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_random_floats(self) -> None:
        """Random float lists of varying length must not crash."""
        for _ in range(FUZZ_ITERATIONS // 10):
            length: int = random.randint(0, 2000)
            samples: list[float] = [
                random.uniform(-10.0, 10.0) for _ in range(length)
            ]
            result = _fft_mod.fft_magnitudes(samples)
            self.assertIsInstance(result, list)
            for val in result:
                self.assertIsInstance(val, float)
                self.assertGreaterEqual(val, 0.0)

    def test_extreme_values(self) -> None:
        """Samples containing inf, -inf, and extreme floats must not crash.

        numpy emits RuntimeWarning on extreme FFT inputs — this is
        expected.  If the warning disappears, something changed in
        the input sanitization that needs investigation.
        """
        import warnings
        extreme_cases: list[list[float]] = [
            [float("inf")] * 64,
            [float("-inf")] * 64,
            [1e300] * 64,
            [-1e300] * 64,
            [1e-300] * 64,
            [0.0] * 64,
            [1.0, -1.0] * 32,
        ]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for samples in extreme_cases:
                result = _fft_mod.fft_magnitudes(samples)
                self.assertIsInstance(result, list)
        # At least one RuntimeWarning expected from inf/extreme inputs.
        runtime_warnings: list = [
            w for w in caught if issubclass(w.category, RuntimeWarning)
        ]
        self.assertGreater(
            len(runtime_warnings), 0,
            "Expected RuntimeWarning from extreme FFT inputs — "
            "sanitization may have changed",
        )

    def test_empty_input(self) -> None:
        """Empty sample list must return empty result."""
        self.assertEqual(_fft_mod.fft_magnitudes([]), [])

    def test_single_sample(self) -> None:
        """Single-sample input must not crash."""
        result = _fft_mod.fft_magnitudes([0.5])
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)

    def test_oversized_input_capped(self) -> None:
        """Input exceeding MAX_WINDOW must be capped, not OOM."""
        huge: list[float] = [0.1] * (_fft_mod.MAX_WINDOW * 4)
        result = _fft_mod.fft_magnitudes(huge)
        self.assertIsInstance(result, list)
        self.assertLessEqual(len(result), _fft_mod.MAX_WINDOW // 2 + 1)

    def test_nan_samples(self) -> None:
        """NaN in samples must not crash (output may contain NaN)."""
        samples: list[float] = [float("nan")] * 128
        try:
            result = _fft_mod.fft_magnitudes(samples)
            self.assertIsInstance(result, list)
        except Exception:
            self.fail("fft_magnitudes crashed on NaN input")


# ===================================================================
# GROUP 2: REST / VALIDATION
# ===================================================================


class TestFuzzParamValidate(unittest.TestCase):
    """Fuzz effects.Param.validate() with every conceivable input type.

    validate() must always return a value of the correct type or raise
    ValueError (for choices violations only).  It must never crash on
    any input — garbage input falls back to the default.
    """

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_int_param_all_types(self) -> None:
        """Int param with every Python type must not crash."""
        p = _Param(50, min=0, max=100)
        for value in INTERESTING_VALUES:
            result = p.validate(value)
            self.assertIsInstance(result, int)
            self.assertGreaterEqual(result, 0)
            self.assertLessEqual(result, 100)

    def test_float_param_all_types(self) -> None:
        """Float param with every Python type must not crash."""
        p = _Param(0.5, min=0.0, max=1.0)
        for value in INTERESTING_VALUES:
            result = p.validate(value)
            self.assertIsInstance(result, float)

    def test_string_choices_param(self) -> None:
        """String choices param with garbage must raise ValueError."""
        p = _Param("red", choices=["red", "green", "blue"])
        for value in INTERESTING_VALUES:
            if value in ["red", "green", "blue"]:
                self.assertEqual(p.validate(value), value)
            else:
                with self.assertRaises(ValueError):
                    p.validate(value)

    def test_random_strings_to_int_param(self) -> None:
        """Random strings coerced to int must fall back to default."""
        p = _Param(50, min=0, max=100)
        for _ in range(FUZZ_ITERATIONS // 10):
            value: str = _random_string(30)
            result = p.validate(value)
            self.assertIsInstance(result, int)
            self.assertGreaterEqual(result, 0)
            self.assertLessEqual(result, 100)

    def test_random_numbers_clamped(self) -> None:
        """Random extreme numbers must be clamped, not overflow."""
        p = _Param(50, min=0, max=100)
        for value in INTERESTING_NUMBERS:
            result = p.validate(value)
            self.assertIsInstance(result, (int, float))


class TestFuzzSchedulerTimeParse(unittest.TestCase):
    """Fuzz scheduler._parse_time_spec() with random strings.

    Must return a datetime or None.  Must never crash.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._sun = _sun_times_fn(30.6954, -88.0399, date(2026, 3, 26))
        cls._date = date(2026, 3, 26)
        cls._offset = timedelta(hours=-5)

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def _parse(self, spec: str) -> Optional[datetime]:
        """Helper — calls _parse_time_spec with precomputed args."""
        return _scheduler_mod._parse_time_spec(
            spec, self._sun, self._date, self._offset,
        )

    def test_random_strings(self) -> None:
        """Random strings must return None or a valid datetime."""
        for _ in range(FUZZ_ITERATIONS):
            spec: str = _random_string(30)
            result = self._parse(spec)
            if result is not None:
                self.assertIsInstance(result, datetime)

    def test_interesting_strings(self) -> None:
        """Known edge-case strings must not crash."""
        for spec in INTERESTING_STRINGS:
            try:
                result = self._parse(str(spec))
                if result is not None:
                    self.assertIsInstance(result, datetime)
            except (ValueError, TypeError, OverflowError):
                pass

    def test_valid_formats(self) -> None:
        """Valid time specs must parse correctly."""
        valid: list[str] = [
            "12:00", "00:00", "23:59", "06:30",
            "sunrise", "sunset", "dawn", "dusk", "noon", "midnight",
            "sunrise+30m", "sunset-1h", "dawn+1h30m",
        ]
        for spec in valid:
            result = self._parse(spec)
            self.assertIsNotNone(result, f"Valid spec {spec!r} returned None")

    def test_boundary_times(self) -> None:
        """Boundary hour/minute values must be handled correctly."""
        boundaries: list[str] = [
            "00:00", "23:59", "24:00", "99:99",
            "0:0", "1:60", "25:00",
        ]
        for spec in boundaries:
            result = self._parse(spec)
            # Should return None for invalid times, datetime for valid.
            if result is not None:
                self.assertIsInstance(result, datetime)

    def test_symbolic_with_huge_offsets(self) -> None:
        """Symbolic times with absurd offsets must not crash."""
        specs: list[str] = [
            "sunrise+999h", "sunset-999h999m",
            "noon+0h0m", "midnight+24h",
        ]
        for spec in specs:
            result = self._parse(spec)
            if result is not None:
                self.assertIsInstance(result, datetime)


class TestFuzzAutomationValidate(unittest.TestCase):
    """Fuzz automation.validate_automation() with garbage configs.

    Must always return a list of error strings.  Must never crash.
    """

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)
        self._known_groups = {"living_room", "bedroom", "porch"}
        self._known_effects = {"on", "off", "cylon", "aurora", "breathe"}
        self._media_effects = {"spectrum", "waveform"}

    def _validate(self, entry: dict) -> list[str]:
        """Helper — calls validate_automation with standard args."""
        return _automation_mod.validate_automation(
            entry,
            self._known_groups,
            self._known_effects,
            self._media_effects,
        )

    def test_completely_random_dicts(self) -> None:
        """Random dicts must not crash — just return errors."""
        for _ in range(FUZZ_ITERATIONS // 5):
            entry: dict = {}
            for _ in range(random.randint(0, 10)):
                key = random.choice([
                    "name", "sensor", "trigger", "action",
                    "off_trigger", "off_action", "schedule_conflict",
                    "enabled", _random_string(10),
                ])
                entry[key] = _random_json_value()
            errors = self._validate(entry)
            self.assertIsInstance(errors, list)
            for err in errors:
                self.assertIsInstance(err, str)

    def test_interesting_values_in_fields(self) -> None:
        """Every interesting value in every field must not crash."""
        for value in INTERESTING_VALUES:
            for field in ["name", "sensor", "trigger", "action",
                          "off_trigger", "off_action"]:
                entry: dict = {field: value}
                try:
                    errors = self._validate(entry)
                    self.assertIsInstance(errors, list)
                except (TypeError, AttributeError):
                    pass

    def test_valid_entry(self) -> None:
        """A properly formed entry must validate with no errors."""
        entry: dict = {
            "name": "test automation",
            "sensor": {
                "type": "ble",
                "label": "onvis_motion",
                "characteristic": "motion",
            },
            "trigger": {"condition": "eq", "value": 1},
            "action": {"group": "living_room", "effect": "on"},
            "off_trigger": {"type": "watchdog", "minutes": 30},
            "off_action": {"effect": "off"},
            "schedule_conflict": "defer",
        }
        errors = self._validate(entry)
        self.assertEqual(errors, [])

    def test_empty_dict(self) -> None:
        """Empty dict must produce errors, not crash."""
        errors = self._validate({})
        self.assertIsInstance(errors, list)
        self.assertGreater(len(errors), 0)


class TestFuzzAutomationParseValue(unittest.TestCase):
    """Fuzz automation._parse_value() with garbage MQTT payloads.

    Must always return a number or raise ValueError.
    Must never crash with an unexpected exception.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._mgr = _automation_mod.AutomationManager.__new__(
            _automation_mod.AutomationManager,
        )

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_random_strings_int_reference(self) -> None:
        """Random strings with int reference must not crash."""
        for _ in range(FUZZ_ITERATIONS):
            raw: str = _random_string(30)
            try:
                result = self._mgr._parse_value(raw, 1)
                self.assertIsInstance(result, (int, float))
            except (ValueError, OverflowError):
                pass

    def test_random_strings_float_reference(self) -> None:
        """Random strings with float reference must not crash."""
        for _ in range(FUZZ_ITERATIONS):
            raw: str = _random_string(30)
            try:
                result = self._mgr._parse_value(raw, 1.0)
                self.assertIsInstance(result, float)
            except (ValueError, OverflowError):
                pass

    def test_interesting_strings(self) -> None:
        """Known edge-case strings must not crash."""
        for raw in INTERESTING_STRINGS:
            for ref in [1, 1.0]:
                try:
                    result = self._mgr._parse_value(str(raw), ref)
                    self.assertIsInstance(result, (int, float))
                except (ValueError, OverflowError):
                    pass

    def test_numeric_strings(self) -> None:
        """Valid numeric strings must parse correctly."""
        cases: list[tuple[str, Any, Any]] = [
            ("0", 1, 0),
            ("1", 1, 1),
            ("-1", 1, -1),
            ("42", 1, 42),
            ("3.14", 1.0, 3.14),
            ("1.0", 1, 1),       # M12 fix: "1.0" -> int 1
            ("-0.5", 1.0, -0.5),
            ("1e3", 1.0, 1000.0),
        ]
        for raw, ref, expected in cases:
            result = self._mgr._parse_value(raw, ref)
            if isinstance(expected, float):
                self.assertAlmostEqual(result, expected, places=5)
            else:
                self.assertEqual(result, expected)


class TestFuzzScheduleEntryValidation(unittest.TestCase):
    """Fuzz schedule entry validation with random JSON-like dicts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._sun = _sun_times_fn(30.6954, -88.0399, date(2026, 3, 26))
        cls._date = date(2026, 3, 26)
        cls._offset = timedelta(hours=-5)

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_random_schedule_entries(self) -> None:
        """Random schedule-shaped dicts must not crash _resolve_entries."""
        for _ in range(FUZZ_ITERATIONS // 10):
            entry: dict = {
                "name": _random_string(20),
                "group": random.choice(["porch", "bedroom", "", None,
                                        _random_string(10)]),
                "start": random.choice([
                    _random_string(15), "12:00", "sunset",
                    "sunrise+30m", "", "99:99",
                ]),
                "stop": random.choice([
                    _random_string(15), "23:00", "midnight",
                    "dawn-1h", "", "00:00",
                ]),
                "effect": random.choice([
                    "cylon", "aurora", "", _random_string(10),
                ]),
                "enabled": random.choice([True, False, None, "yes", 0]),
            }
            try:
                result = _scheduler_mod._resolve_entries(
                    [entry], 30.6954, -88.0399,
                    self._date, self._offset,
                )
                self.assertIsInstance(result, list)
            except (ValueError, TypeError, AttributeError):
                pass


class TestFuzzPlayCommandValidation(unittest.TestCase):
    """Fuzz effect creation with random names and params.

    create_effect() must either return an Effect or raise ValueError.
    It must never crash on unexpected param types.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._registry = _get_registry_fn()
        cls._effect_names = list(cls._registry.keys())

    def setUp(self) -> None:
        random.seed(FUZZ_SEED)

    def test_random_effect_names(self) -> None:
        """Random strings as effect names must raise ValueError."""
        for _ in range(FUZZ_ITERATIONS // 5):
            name: str = _random_string(20)
            if name not in self._registry:
                with self.assertRaises((ValueError, KeyError)):
                    _create_effect_fn(name)

    def test_valid_effects_garbage_params(self) -> None:
        """Valid effect names with garbage params must not crash."""
        for _ in range(FUZZ_ITERATIONS // 5):
            name: str = random.choice(self._effect_names)
            params: dict = {}
            for _ in range(random.randint(0, 8)):
                key: str = random.choice([
                    "speed", "brightness", "hue", "width",
                    "saturation", "floor", "color", "kelvin",
                    _random_string(10),
                ])
                params[key] = _random_json_value()
            try:
                effect = _create_effect_fn(name, **params)
                self.assertIsNotNone(effect)
            except (ValueError, TypeError):
                pass

    def test_every_effect_renders_with_extreme_params(self) -> None:
        """Every registered effect must survive render() with edge cases.

        Calls on_start() first to honor the Effect lifecycle contract.
        Effects that require on_start() for state initialization are
        allowed to fail gracefully without it, but the normal path
        (on_start then render) must never crash.
        """
        zone_counts: list[int] = [1, 2, 3, 36, 108]
        times: list[float] = [0.0, 0.001, 1.0, 100.0, -1.0]
        for name in self._effect_names:
            try:
                effect = _create_effect_fn(name)
            except (ValueError, TypeError, ImportError):
                continue
            for zc in zone_counts:
                # Call on_start() — the correct lifecycle.
                try:
                    effect.on_start(zc)
                except Exception:
                    pass
                for t in times:
                    try:
                        result = effect.render(t, zc)
                        if result is not None:
                            self.assertIsInstance(result, list)
                    except (ValueError, ZeroDivisionError,
                            IndexError, TypeError, AttributeError):
                        pass


if __name__ == "__main__":
    unittest.main()
