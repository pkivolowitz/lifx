# Testing

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp includes a comprehensive test suite that validates the core engine
without requiring physical LIFX hardware or network access.  All tests
use mock objects, temporary files, or pure math — no sockets are opened.

### Running the Full Suite

```bash
# Run the full suite via pytest (from project root):
python3 -m pytest tests/ -v

# Or run a specific module:
python3 -m pytest tests/test_effects.py -v
```

### Test Modules

**1,150+ test methods** across 36 test files.  The table below lists every
module; run `python3 -m pytest test_*.py tests/ -v` for the full count.

| Module | Tests | What it covers |
|--------|------:|----------------|
| `tests/test_effects.py` | 168 | Every registered effect × {1, 3, 36, 108} zones — frame length, HSBK range, 50-frame stability |
| `tests/test_use_cases.py` | 17 | End-to-end Controller/Engine/Emitter pipeline integration |
| `tests/test_effect_contracts.py` | 6 | Effect render contract enforcement (170 subtests) |
| `tests/test_schedule.py` | 47 | Time parsing, symbolic solar times, overnight wraparound, day filtering |
| `tests/test_schedule_unification.py` | 17 | Unified schedule config for server and scheduler |
| `tests/test_config.py` | 28 | Server config validation: auth, ports, groups, MQTT, file errors |
| `tests/test_override.py` | 19 | DeviceManager override logic: group-level, member-level, clear-and-resume |
| `tests/test_solar.py` | 11 | Solar calculations for multiple latitudes (polar, equatorial, mid-latitude) |
| `tests/test_virtual_multizone.py` | 8 | VirtualMultizoneEmitter zone mapping and dispatch |
| `tests/test_multizone_products.py` | 7 | LIFX product database validation against official registry |
| `tests/test_routing.py` | 22 | Declarative URL route table consistency and pattern matching |
| `tests/test_rest_integration.py` | 66 | REST API integration with real HTTP server (security, validation, CRUD) |
| `tests/test_audit_critical.py` | 52 | Regression tests for critical audit fixes (C1–C17) |
| `tests/test_audit_medium.py` | 25 | Regression tests for medium-severity audit fixes (M1–M35) |
| `tests/test_audit_regressions.py` | 66 | Tech debt audit regressions (HSBK, signals, MQTT, logging) |
| `tests/test_fuzz.py` | 42 | Fuzz testing: random bytes to parsers, validators, and param system |
| `tests/test_concurrency.py` | 11 | Thread safety under contention (overrides, config, registry) |
| `tests/test_affinity.py` | 12 | Effect device affinity metadata validation |
| `tests/test_distributed.py` | 62 | Distributed SOE pipeline, MIDI parsing, audio extraction |
| `tests/test_fft.py` | 22 | Dual-path FFT with numpy and pure-Python fallback |
| `tests/test_media.py` | 38 | SignalBus and AudioExtractor pipeline operations |
| `tests/test_audio_out.py` | 30 | AudioOutEmitter registration, parameters, mute, lifecycle |
| `tests/test_protocol.py` | 20 | Distributed protocol UDP wire format pack/unpack |
| `tests/test_transport_adapter.py` | 8 | UDP transport loopback and SignalValue interface |
| `tests/test_udp_channel.py` | 11 | UDP sender/receiver loopback frame validation |
| `tests/test_luna_hardware.py` | 23 | Hardware validation for LIFX Luna 700-series matrix (requires device) |
| `tests/test_adapter_base.py` | 117 | Adapter base class lifecycle, MQTT, polling, async |
| `tests/test_automation.py` | 65 | Automation rules, watchdog, trigger conditions |
| `tests/test_ble_endpoint.py` | 8 | BLE sensor REST endpoint |
| `tests/test_engine.py` | 40 | Engine controller lifecycle, play/stop/resume |
| `tests/test_diagnostics.py` | 13 | PostgreSQL diagnostics subsystem |
| `tests/test_environment.py` | 3 | System-level environment sanity checks |
| `tests/test_midi_parser.py` | 36 | MIDI file parser (header, events, edge cases) |
| `tests/test_midi_pipeline.py` | 37 | MIDI sensor, emitter, and light bridge components |

### VirtualMultizoneEmitter Tests (Detail)

The tests in `test_virtual_multizone.py` use `MockDevice` objects that
record all method calls for assertion.  To add new tests, follow the same
pattern: create `MockDevice` instances with the desired `zone_count`,
`is_multizone`, and `is_polychrome` values, build a
`VirtualMultizoneEmitter`, call methods, and assert against the recorded
calls.

### Effect Rendering Tests (Detail)

`test_effects.py` dynamically generates a test method for every registered
effect at four zone counts (1, 3, 36, 108 zones).  Each test calls
`on_start()` then `render()` at t=0, 1, and 10 seconds, validating:

- Frame length matches the requested zone count
- Every HSBK tuple has exactly 4 components
- H, S, B values are in [0, 65535]
- Kelvin values are in [1500, 9000]

A separate stability test renders 50 consecutive frames at 20 fps for
every effect to catch late-onset crashes in stateful effects like
fireworks, rule30, and newtons_cradle.

### Override Tests (Detail)

`test_override.py` covers the group-member override bug fixed in 8a27b45.
When a user overrides an individual device (e.g., 192.0.2.62) that belongs
to a group (e.g., group:porch), the scheduler must recognize the conflict
and skip the group.  The `is_overridden_or_member()` method checks both
the group ID and every member IP.  Tests verify:

- Overriding one member makes the group check return True
- Overriding a non-member does not affect the group
- Clearing the member override restores normal scheduling
- Both group and member overridden simultaneously works correctly
