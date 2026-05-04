<p align="center">
  <img src="docs/assets/logo.jpg" alt="GlowUp" width="200">
</p>

# GLOWUP

**LIFX lighting system**

GlowUp is a self-hosted home lighting control platform. The supported install
drives LIFX lights.

This project has used AI assistance (Claude 4.6) for boilerplate and
logic expansion. All architectural decisions and code integration are by Perry
Kivolowitz, the sole Human Author.

## What `install.sh` Delivers

```bash
git clone https://github.com/pkivolowitz/glowup.git
cd glowup
./install.sh
```

A working LIFX system on either a single host (your laptop) or an
always-on Linux server.  The supported surface:

- **Discover** every LIFX bulb on your network

- **Name** each bulb — the label travels in firmware and survives DHCP changes

- **Group** bulbs into named virtual surfaces — a whole room becomes one canvas

- **33 effects** — aurora, fireworks, Newton's Cradle, plasma, sonar, matrix
  rain, Conway's Game of Life, 199 country flags, and more. Effects span 1D
  strips, single bulbs, and 2D matrix devices (Tile, Luna, Candle, Ceiling).
  Animated previews in the [Effect
  Gallery](https://pkivolowitz.github.io/glowup/)

- **Schedule** bulbs to react to sunrise / sunset / clock time

- **Dashboard** at `http://<host>:8420/home` — group control, schedule editor,
  device registry

- **Simulator** preview — render an effect to a window without
  touching a bulb

Two install flavors: **standalone** runs while you're at the keyboard, no
`sudo`, state under `~/.glowup`.  **Server** runs as a `systemd` daemon on
Linux, owns config under `/etc/glowup`, keeps schedules alive while your laptop
sleeps.  You can move from the first to the second later without losing your
bulb names or groups.

That is the **whole** supported surface today.  Manual is located here:
[docs/BASIC.md](docs/BASIC.md).

## Requirements

- Python 3.11 or later
- A LIFX bulb on your local network

The optional simulator needs `tkinter` (ships with macOS and Windows
Python; one `apt-get install` on Linux).  Everything in the
"What's Also In Here" section has its own opt-in dependencies — see
[docs/02-requirements.md](docs/02-requirements.md) for the full
matrix.

## Documentation

- [docs/BASIC.md](docs/BASIC.md) — the supported install, end to end:
  standalone, Linux server, dashboard, schedules, effects
- [docs/CONTRIB.md](docs/CONTRIB.md) — vendor-integration notes for
  the unsupported adapters (Vivint, Reolink, Brother, HDHomeRun)

## Caveat

Tested with LIFX string lights, Neon, Luna (700-series matrix), Ceiling, and
monochrome bulbs.  I don't own every LIFX product — please report problems;
fixes for other devices are welcome.

## License

MIT.

## Appreciation

> If you find this software useful, please consider donating to a
> local food pantry.  Even a single can of soup makes someone in
> your neighborhood's day a little easier.
