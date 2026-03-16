# Troubleshooting

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

LIFX devices maintain internal state that persists across sessions.
Occasionally — particularly after switching between different control
sources (this engine, the LIFX app, HomeKit) — a device may display
unexpected colors or brief visual artifacts when a new effect starts.

If you notice residual colors from a previous session, simply
**power-cycle the device** (off, then on) using the physical switch or
the LIFX app.  If the device is in a location where power-cycling is
inconvenient, opening the official LIFX app and briefly controlling the
device can help clear its internal state.

This is a characteristic of the LIFX firmware's internal state
management and may not be specific to GlowUp.
