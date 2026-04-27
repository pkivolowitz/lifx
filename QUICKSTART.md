# GlowUp Quick Start

Get GlowUp running in under 2 minutes with its simplest deployment:
one laptop, one LIFX device, no server required.

## Prerequisites

- Python 3.10 or newer
- LIFX bulbs on the same WiFi network as your computer
- macOS or Linux (Windows works but is untested)

## Install

```bash
git clone https://github.com/perrykivolowitz/lifx.git
cd lifx
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Find Your Bulbs

```bash
python3 glowup.py discover
```

This broadcasts on your LAN and lists every LIFX device it finds
with its IP address, label, and capabilities.

## Run an Effect

Pick a device IP from the discover output and run:

```bash
python3 glowup.py play cylon --ip 192.0.2.42
```

Replace `192.0.2.42` with your bulb's IP.

Press **Ctrl+C** to stop.  The bulb fades to black gracefully.

## See All Effects

```bash
python3 glowup.py effects
```

There are 35 built-in effects.  Some favorites:

```bash
python3 glowup.py play aurora --ip 192.0.2.42
python3 glowup.py play breathe --ip 192.0.2.42
python3 glowup.py play campfire --ip 192.0.2.42
python3 glowup.py play rainbow --ip 192.0.2.42
python3 glowup.py play fireworks2d --ip 192.0.2.42
```

## Preview Without Hardware

Don't have bulbs yet?  Use the simulator:

```bash
python3 glowup.py play cylon --sim-only
```

This opens a window that shows what the effect looks like
without sending any packets to real devices.

## Multiple Bulbs

Create a simple JSON config to group bulbs:

```bash
cat > my_lights.json << 'EOF'
{
  "groups": {
    "living": ["192.0.2.42", "192.0.2.43", "192.0.2.44"]
  }
}
EOF

python3 glowup.py play aurora --config my_lights.json --group living
```

## What's Next?

- **[Full Manual](docs/MANUAL.md)** — the complete guide, organized by what you want to do
- **[Effects Reference](docs/06-effects.md)** — every effect with its parameters
- **[CLI Reference](docs/04-cli-reference.md)** — all command-line options
- **Server mode** — run `python3 server.py` for scheduling, SOE coordination, REST API, iOS app, and more (see [Part II](docs/MANUAL.md#part-ii--server))
- **Distributed mode** — add MQTT, remote workers, adapters, and voice components when you are ready
