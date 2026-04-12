"""GlowUp contrib — user-contributed extensions to the SOE pipeline.

Contrib is a first-class citizen at the repo root organized along the
Sensor → Operator → Emitter architecture.  Anything that produces,
transforms, or consumes a GlowUp signal can live here without
cluttering the core engine.

Subtrees:
- adapters/   — bridges to third-party systems (Vivint, HDHomeRun, etc.)
- sensors/    — local signal sources (thermal, BLE, audio, etc.)
- operators/  — custom signal transformers
- emitters/   — custom output devices
"""
