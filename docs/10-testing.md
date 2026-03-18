# Testing

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp includes a comprehensive test suite that validates the core engine
without requiring physical LIFX hardware or network access.  All tests
use mock objects, temporary files, or pure math — no sockets are opened.

### Running the Full Suite

```bash
# Run every test file at once (stop on first failure):
for f in test_*.py; do python3 -m unittest "$f" || exit 1; done

# Or run a specific module:
python3 -m unittest test_effects -v
```

### Test Modules

| Module | Tests | What it covers |
|--------|------:|----------------|
| `test_effects.py` | 73 | Every registered effect × {1, 3, 36, 108} zones — frame length, HSBK range (0–65535 for H/S/B, 1500–9000 K), 50-frame stability for stateful effects, registry sanity |
| `test_schedule.py` | 28 | `_parse_time_spec` (fixed times, symbolic solar times, offsets), `_validate_days`, `_days_display`, `_resolve_entries` (overnight wraparound, group filtering), `_find_active_entry` |
| `test_config.py` | 22 | `_load_config` validation: auth tokens (missing, default, empty, non-string), ports (zero, negative, >65535, string), groups (missing, empty), schedule entries (missing fields, unknown groups, invalid days), MQTT section (bad port, empty prefix, negative interval), file errors |
| `test_override.py` | 18 | DeviceManager override logic: basic set/clear, group-level overrides, individual member overrides within groups (`is_overridden_or_member`), override entry tracking, clear-and-resume |
| `test_solar.py` | 12 | `sun_times()` for Mobile AL, NYC, Tromso (polar night + midnight sun), Quito (equator): event ordering, day length, timezone awareness, noon always present, latitude validation |
| `test_virtual_multizone.py` | 6 | `VirtualMultizoneDevice` zone mapping and dispatch: multizone + color + mono mix, batched `set_zones()`, `set_color` broadcast, two-strip independent batching, all-singles regression |
| `test_multizone_products.py` | varies | LIFX product database: product IDs, zone counts, multizone detection |

### VirtualMultizoneDevice Tests (Detail)

The tests in `test_virtual_multizone.py` use `MockDevice` objects that
record all method calls for assertion.  To add new tests, follow the same
pattern: create `MockDevice` instances with the desired `zone_count`,
`is_multizone`, and `is_polychrome` values, build a
`VirtualMultizoneDevice`, call methods, and assert against the recorded
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
