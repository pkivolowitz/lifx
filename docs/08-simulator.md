# Live Simulator

The `--sim` flag on the `play` command opens a tkinter window that
displays the effect output in real-time as colored rectangles — one per
zone.  This lets you preview effects without physical hardware, or watch
what the engine is sending alongside real devices.

```bash
# Preview cylon on your lights and in the simulator window
python3 glowup.py play cylon --ip 10.0.0.62 --sim

# Show 36 bulbs instead of 108 zones (LIFX strings have 3 zones per bulb)
python3 glowup.py play cylon --ip 10.0.0.62 --sim --zpb 3

# Works with virtual multizone groups too
python3 glowup.py play aurora --config schedule.json --group office --sim
```

The simulator window shows:

- **Zone strip** — a row of colored rectangles, one per zone (or per
  bulb when `--zpb` is set), updated every frame with true RGB color
  converted from the LIFX HSBK values.  Monochrome (non-polychrome)
  zones are rendered in grayscale using BT.709 luma weighting, matching
  what the physical bulbs actually display.
- **Header** — the effect name and zone count.
- **FPS counter** — the actual display refresh rate (smoothed over 10
  frames).

**Closing the window** triggers the same clean shutdown as Ctrl+C — the
effect fades to black and devices are powered off.

### How It Works

The engine renders frames in a background thread.  After dispatching
colors to devices, it calls an optional `frame_callback` with the
rendered color list.  The simulator puts frame data onto a
`queue.Queue` (thread-safe), and the tkinter event loop on the main
thread polls that queue via `root.after()` to update the display.
This satisfies the macOS requirement that all tkinter calls happen on
the main thread.

### Graceful Fallback

If tkinter is not available (missing the `_tkinter` C extension), the
`--sim` flag prints a note and continues without the window.  The rest
of the system is completely unaffected.  To install tkinter on macOS
with Homebrew Python:

```bash
brew install tcl-tk python-tk@3.10
```

### Zones Per Bulb (`--zpb`)

LIFX string lights use 3 zones per physical bulb (108 zones = 36 bulbs).
By default, the simulator shows one rectangle per zone.  Use `--zpb 3`
to group zones into bulbs — the display shows the middle zone's color
for each group, matching the visual appearance of the physical string.

### Zoom (`--zoom`)

The `--zoom` flag scales all simulator dimensions by an integer factor
(1–10). Zone widths, heights, padding, and header font size are all
multiplied, producing a proportionally larger window with sharp pixel
edges (nearest-neighbor scaling, no interpolation blur).

```bash
# Double-size simulator window
python3 glowup.py play aurora --ip 10.0.0.62 --sim --zoom 2

# Monitor mode also supports zoom
python3 glowup.py monitor --ip 10.0.0.62 --zoom 3
```

Useful for presentations, demos, and high-DPI displays where the
default window is too small to read comfortably.

### Adaptive Sizing

Zone widths automatically shrink for large zone counts so the window
fits on screen (capped at 1600px).  A 108-zone string light fits
comfortably; a 200-zone virtual group will use narrower rectangles.
Using `--zpb` reduces the rectangle count, producing wider, more
readable bulbs.

### Monitor Mode

The `monitor` subcommand turns the simulator into a read-only live
display of a real device's current zone colors.  Unlike `play --sim`
(which shows what the engine is *sending*), `monitor` queries the
device for its *actual* state — whatever is driving the lights (the
scheduler on a Pi, the LIFX phone app, or any other controller).

```bash
# Monitor a string light at 4 Hz (default)
python3 glowup.py monitor --ip 10.0.0.62 --zpb 3

# Higher polling rate for smoother updates
python3 glowup.py monitor --ip 10.0.0.62 --zpb 3 --hz 10
```

| Flag    | Default | Description                                      |
|---------|---------|--------------------------------------------------|
| `--ip`  | —       | Device IP address (required)                     |
| `--hz`  | 4.0     | Polling rate in Hz (0.5–20.0)                    |
| `--zpb` | 1       | Zones per bulb (3 for LIFX string lights)        |

Monitor mode is completely passive — it only reads the device state
and never sends color or power commands.  You can safely run it from
any machine on the LAN while the scheduler drives the lights from
another.

### Sim-Only Mode

The `--sim-only` flag queries the real device (or group) to discover its
zone count and polychrome capabilities, then immediately closes the
connection.  From that point on, **no packets are sent to the lights**.
The effect runs entirely inside the simulator window.

This is useful when you want to:

- Preview a new or modified effect without disturbing lights that are
  already in use (e.g., the scheduler is running).
- Tune parameters in advance and decide on values before deploying.
- Develop effects on a machine that is not on the same LAN as the lights.

```bash
# Preview fireworks on your string light without touching it
python3 glowup.py play fireworks --ip <device-ip> --sim-only

# Preview across a virtual multizone group, 3 zones per bulb display
python3 glowup.py play aurora --config schedule.json --group porch --sim-only --zpb 3

# Tune parameters first, then deploy for real
python3 glowup.py play cylon --ip <device-ip> --sim-only --speed 1.5 --width 8
```

The simulator title bar shows the effect name and zone count.  All
effect parameters work identically to normal `play` mode.

`--sim-only` requires tkinter.  If it is not available, the command
exits with an error rather than silently doing nothing.

`--sim-only` and `--sim` are mutually exclusive — `--sim-only` implies
the simulator; adding `--sim` is redundant but harmless.

### macOS Accessibility Permission

On macOS, the simulator window uses `osascript` to ask System Events
to bring the Python process to the foreground.  The first time you
run any simulator mode (`--sim`, `--sim-only`, or `monitor`), macOS will prompt you
to grant **Accessibility** permission to your terminal application
(Terminal, iTerm2, VS Code, etc.).  This is a standard macOS security
gate for any program that activates another process's window.  The
permission is granted once and remembered — subsequent launches will
not prompt again.

If you prefer not to grant the permission, simply dismiss the dialog.
The simulator will still work; the window just won't automatically
come to the front on launch.

