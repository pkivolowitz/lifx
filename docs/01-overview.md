# Overview

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp is a decentralized home-control system built around the
**Sensors → Operators → Emitters** model.

Sensors bring information into the system.  A motion detector, Zigbee
soil sensor, lock state, microphone, camera feed, wake word, REST call,
or user-set parameter are all signals.  Operators transform those
signals: thresholding, gating, occupancy inference, FFT analysis,
voice intent execution, ML classification, scheduling, and effect
rendering.  Emitters send results back into the world as light, audio,
screens, notifications, logical signals, or other downstream inputs.

LIFX is the most mature emitter family in GlowUp today, which is why
the project includes a rich effect engine, virtual multizone surfaces,
and direct LAN control.  But GlowUp is not defined by LIFX.  It is
defined by a transport-agnostic signal fabric, composable operators,
and the ability to distribute work across multiple machines.

## Core Ideas

- **Generalized signal system** — a temperature reading, a voice command,
  and a UI parameter change are all just named signals.
- **Decentralized deployment** — sensors, operators, and emitters may run
  on different computers and communicate over MQTT, UDP, HTTP, BLE,
  Zigbee, vendor APIs, and other transports.
- **Resiliency by structure** — adapter processes, keepalive daemons,
  device registry indirection, and service supervision reduce the blast
  radius of failures.
- **Voice is native** — wake word, speech recognition, intent execution,
  and speech output are part of the same architecture, not a sidecar app.

## LIFX as a Flagship Emitter

GlowUp includes a mature lighting engine for LIFX devices (string lights,
beams, Z strips, single-color bulbs, matrix products, and monochrome
bulbs) over the LAN protocol.  Color effects on monochrome bulbs are
automatically converted to perceptually correct brightness using
BT.709 luma coefficients.

**Virtual multizone** groups let multiple LIFX devices behave as one
animation surface.  Multizone devices contribute all their zones;
single bulbs contribute one zone each.  Effects remain pure renderers:
they know nothing about networking or hardware layout.  They render to
an abstract surface, and GlowUp maps the result back onto real devices.

That same separation of concerns is what lets GlowUp extend past
lighting into generalized home control.
