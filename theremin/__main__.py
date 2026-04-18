"""Allow running ``python3 -m theremin`` to show usage."""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0.0"

print("GlowUp Theremin — available subcommands:")
print()
print("  python3 -m theremin.simulator   — Sensor simulator (Mac sliders)")
print("  python3 -m theremin.synth       — Audio synthesizer (Mac)")
print("  python3 -m theremin.display     — Display window (Mac)")
print()
print("The ThereminEffect runs on the Pi as part of the GlowUp server.")
print("Play it via CLI:  python3 glowup.py --ip 192.0.2.23 theremin")
print("Or via API:       POST /api/play {\"ip\": \"192.0.2.23\", \"effect\": \"theremin\"}")
