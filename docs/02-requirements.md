# Requirements

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

- **Python 3.10+**
- One or more LIFX devices on the same LAN subnet (multizone, single color, or monochrome)
- No external Python packages — the entire stack is pure Python stdlib
- **Optional:** [ffmpeg](https://ffmpeg.org/) for the `record` subcommand (rendering effects to GIF/MP4/WebM)

### Platform Support

| Platform | Status | Notes |
|----------|--------|-------|
| **macOS** | Fully supported | Primary development platform. Broadcast auto-detection via `ifconfig`, simulator window focus via `osascript`. |
| **Linux (Raspberry Pi, Ubuntu, etc.)** | Fully supported | Broadcast auto-detection via `ioctl`. Recommended deployment target. |
| **Windows** | Degraded (untested) | Broadcast discovery is unavailable — use `--ip` to address devices directly. Effects, simulator, and server should work. See [Windows notes](#windows) below. |

### Platform-Specific Setup

**macOS** — Python 3.10+ from Homebrew, the Xcode command-line tools,
or a conda environment.  tkinter ships with the standard Python
distribution on macOS:

```bash
# Homebrew
brew install python@3.12

# For the record subcommand (optional)
brew install ffmpeg

# Or conda
conda create -n glowup python=3.12
conda activate glowup
```

**Linux (Debian / Ubuntu / Raspberry Pi OS)** — install Python and
tkinter (needed only for the `--sim` live preview):

```bash
sudo apt update
sudo apt install python3 python3-tk

# For the record subcommand (optional)
sudo apt install ffmpeg
```

On Raspberry Pi OS (Bookworm), Python 3.11+ is included by default.
Install tkinter only if you plan to use the simulator on a desktop —
headless Pi deployments (server, scheduler) do not need it.

#### Windows

> **Windows support has not been tested.**  The guidance below is based
> on code analysis, not hands-on verification.  If you try it, we would
> appreciate a report — good or bad — via a GitHub issue.

GlowUp should run on Windows with one limitation: the `discover`
command cannot auto-detect your subnet's broadcast address (this
requires Unix-specific `fcntl`/ioctl calls).  Everything else should
work — effects, the simulator, and the server.

Install Python 3.10+ from [python.org](https://www.python.org/downloads/)
(tkinter is included by default on Windows).

To work around the discovery limitation, find your device IPs using the
official LIFX app or your router's DHCP lease table, then address
devices directly:

```bash
# Play an effect by IP (no broadcast discovery needed)
python glowup.py play aurora --ip 10.0.0.62

# Simulator-only mode works with no devices at all
python glowup.py play aurora --ip 10.0.0.62 --sim-only

# Server mode — list device IPs in server.json
python server.py server.json
```

> **Note:** `discover` may still work on simple single-subnet networks
> because the fallback broadcast address `255.255.255.255` is used
> automatically.  Results vary by network configuration.
