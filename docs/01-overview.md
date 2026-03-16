# Overview

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The GLOWUP LIFX Effect Engine drives animated lighting effects on LIFX
devices (string lights, beams, Z strips, single color bulbs, and monochrome
bulbs) over the local network using the LIFX LAN protocol. It replaces the
battery-draining phone app with a lightweight CLI that can run on a
Raspberry Pi or similar as a daemon.

Color effects on monochrome (white-only) bulbs are automatically converted
to perceptually correct brightness using BT.709 luma coefficients.

**Virtual multizone** — Any combination of LIFX devices can be grouped
into a virtual multizone strip.  Multizone devices (string lights, beams)
contribute all their zones; single bulbs contribute one zone each.  Five
white lamps around a room become a 5-zone animation surface; add a
108-zone string light and it becomes 113 zones.  A cylon scanner sweeps
lamp to lamp, aurora curtains drift around you, a wave oscillates across
the room.  Define device groups in a config file and the engine treats
them as one device.  Effects don't need any changes — they already
render per-zone colors, and the virtual device routes each color back
to the correct physical device, batching multizone updates efficiently.

LIFX limits a single physical chain to 3 string lights (36 bulbs,
108 zones — 12 bulbs × 3 zones × 3 strings).  The virtual multizone
feature removes that ceiling entirely.  Each chain is an independent
network device with its own IP address; the engine stitches them
together in software.  Five separate 3-string chains scattered around
a room become a single 180-bulb, 540-zone animation surface with no
hardware modifications.

Effects are **pure renderers** — they know nothing about devices or
networking. Given a timestamp and a zone count, they return a list of
colors. The engine handles framing, timing, and transport.
