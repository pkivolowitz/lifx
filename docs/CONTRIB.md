# GlowUp — Vendor-Specific Integrations

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

These adapters bridge specific commercial products into the GlowUp
platform.  They live in `contrib/adapters/` and are **fully optional** --
the core system runs without any of them.  They also serve as examples
of how to write adapters for your own hardware.  `contrib/` is a
first-class top-level tree with sibling subdirectories for sensors,
operators, and emitters contributed by users.

| Adapter | Product | Base Class | Config Key |
|---------|---------|------------|------------|
| `vivint_adapter.py` | Vivint smart-home panel (locks, sensors, cameras) | `AsyncPollingAdapterBase` | `vivint` |
| `nvr_adapter.py` | Reolink network video recorder | `AsyncPollingAdapterBase` | `nvr` |
| `printer_adapter.py` | Brother laser printers (SNMP) | `PollingAdapterBase` | `printer` |
| `hdhr_adapter.py` | SiliconDust HDHomeRun tuners | `PollingAdapterBase` | `hdhr` |

---

## How Contrib Adapters Load

Every contrib adapter is behind a guarded import with a `_HAS_*`
sentinel in `server.py`.  If the adapter module or its dependencies
are missing, the sentinel is `False` and the adapter is silently
skipped.  No configuration needed to exclude one -- just don't
install its dependencies.

---

## Detailed Documentation

- [HDHomeRun Adapter](33-hdhr-adapter.md) -- Network tuner integration,
  signal diagnostics, dashboard tiles
- [Power Monitoring](34-power-monitoring.md) -- Smart plug energy
  tracking via Zigbee, cost charts, anomaly detection

---

## Writing Your Own Contrib Adapter

See [Chapter 27: Adapter Base Classes](27-adapter-base.md) for the
full API.  The short version:

- Subclass `PollingAdapterBase` (synchronous) or
  `AsyncPollingAdapterBase` (async loop with backoff)
- Implement `_do_poll()` -- called on each cycle
- Optionally implement `_check_prerequisites()` for config validation
- Place the file in `contrib/adapters/`
- Add a guarded import in `server.py` and a factory in
  `adapters/run_adapter.py`
- Add a `server.json` config section gated on the sentinel
