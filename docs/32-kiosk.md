# /home Kiosk Display

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

The `/home` endpoint serves an ambient display page designed for
wall-mounted screens.  It requires no authentication.  The page shows
a large clock, sensor tiles, security state, camera feeds, weather
data, and environmental information -- everything a household needs
at a glance without interacting with a phone or computer.

The `tools/setup_clock.sh` script transforms a fresh Raspberry Pi OS
Desktop into a fullscreen kiosk displaying this page.

---

## Current Tiles

The display is a responsive grid of square cards below a large clock.
Tiles appear and hide dynamically based on data availability.

- **Clock** -- 12-hour time with AM/PM, day of week and date.
  Sized for across-room readability (6-8 inches on a 24" monitor).
- **Locks** -- Shows all Vivint deadbolt locks as colored circles.
  Green filled = locked, red hollow = unlocked, pulsing orange =
  stale data.  Includes per-lock battery percentage with low-battery
  blinking.  An occupancy badge (HOME/AWAY) appears above the lock
  circles when the OccupancyOperator is running.
- **Security** -- Alarm panel state (Disarmed, Armed Stay, Armed
  Away) and door contact sensor grid.  Each door shows open/closed
  status and battery level.
- **Camera feed** -- Cycles through NVR camera snapshots.  The image
  rotates on a timer with a camera name label overlay.  Retries on
  failure for slow Pi 3B boots where the NVR feed takes time to
  become available.
- **Weather** -- Current conditions from Open-Meteo (no API key
  required).  Shows temperature, weather icon, wind speed, and
  humidity.
- **Air Quality** -- US AQI from Open-Meteo with color-coded severity
  (good, moderate, sensitive, unhealthy, very bad, hazardous).
  Includes dominant pollutant and pollen details.
- **UV Index** -- Current UV from Open-Meteo with color-coded risk
  levels (low, moderate, high, very high, extreme).
- **NWS Alerts** -- Severe weather alerts from the National Weather
  Service API (`api.weather.gov`).  Title blinks when active alerts
  exist.  Shows "All Clear" with green text when no alerts are
  present.
- **Soil Moisture** -- Scrolls through Zigbee soil moisture sensors
  with navigation dots.  Shows moisture percentage, temperature, and
  battery.  Color-coded: green = wet, amber = dry, red = critical.
- **Moon Phase** -- Canvas-drawn moon with phase name and illumination
  percentage.  Pure math, no external API.
- **Fairhope Pier Webcam** -- Live thumbnail from a YouTube stream.
  Spans 3 grid columns for a wide aspect ratio.  Falls back to a
  static image if the stream is offline.
- **Battery Alerts** -- Conditionally shown card listing all Vivint
  devices with low battery.  Each entry blinks to draw attention.
- **Printer Alerts** -- Conditionally shown card when a network
  printer needs attention (low toner, paper jam, etc.).  Blinks.

---

## Dark Mode

The page supports a dark mode activated when room lights are off at
night.  CSS custom properties switch the entire palette:

- Background shifts from warm leather tones to near-black.
- Card backgrounds become nearly transparent.
- Photos desaturate and dim.
- Overall opacity drops to 0.65 to avoid lighting the room.

The mode class (`body.dark-mode`) is applied based on the server's
occupancy and lighting state via the `/api/home/mode` endpoint.

---

## Portrait Mode Support

For rotated displays (e.g., a Pi mounted vertically), the grid
switches from 4 columns to 3 columns:

```css
@media (orientation: portrait) {
    .sensor-grid {
        grid-template-columns: repeat(3, 1fr);
    }
}
```

Wayland compositors with `wlr-randr` transform may not correctly
report orientation to CSS media queries.  A JavaScript fallback
detects portrait by comparing `window.innerHeight > innerWidth` and
applies a `body.portrait` class:

```javascript
function checkPortrait() {
    document.body.classList.toggle("portrait",
        window.innerHeight > window.innerWidth);
}
window.addEventListener("resize", checkPortrait);
```

Both the CSS media query and the JS class apply identical grid rules,
so the layout works regardless of which detection method succeeds.

---

## Server Endpoints

The `/home` page fetches data from these API endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/home` | `GET` | Serves the `home.html` page |
| `/api/home/photos` | `GET` | List available photos for background slideshow |
| `/api/home/locks` | `GET` | Lock states and battery levels |
| `/api/home/security` | `GET` | Alarm state and door contacts |
| `/api/home/cameras` | `GET` | NVR camera snapshot URLs |
| `/api/home/occupancy` | `GET` | Current HOME/AWAY state |
| `/api/home/mode` | `GET` | Display mode (dark/light) |
| `/api/home/printer` | `GET` | Printer status and alerts |
| `/api/home/soil` | `GET` | Soil moisture sensor readings |

All endpoints return JSON.  No authentication is required -- the
`/home` page is designed for always-on displays that cannot
interactively authenticate.

---

## External APIs

The `/home` page queries external APIs directly from the browser
(client-side JavaScript), not through the GlowUp server:

| API | Source | Data | Auth |
|-----|--------|------|------|
| Open-Meteo Weather | `api.open-meteo.com` | Temperature, wind, humidity, weather code | None (free, no key) |
| Open-Meteo Air Quality | `air-quality-api.open-meteo.com` | US AQI, pollutants, pollen | None |
| Open-Meteo UV | `api.open-meteo.com` (hourly) | UV index | None |
| NWS Alerts | `api.weather.gov` | Severe weather alerts by county/zone | None |
| YouTube Thumbnails | `img.youtube.com` | Live stream thumbnails for webcams | None |

The page uses the server's configured `location` (latitude/longitude)
for weather and AQI queries.

---

## Kiosk Clock Setup

The `tools/setup_clock.sh` script turns a Raspberry Pi OS Desktop
into a fullscreen kiosk.  It is idempotent -- safe to re-run.

### Usage

```
sudo bash setup_clock.sh <server_ip> [options]
```

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `server_ip` | Yes | -- | GlowUp server IP (e.g., `10.0.0.214`) |
| `--hostname NAME` | No | `clock` | Set the Pi hostname |
| `--rotate DEGREES` | No | `0` | Screen rotation: `0`, `90`, `180`, `270` |
| `--ble` | No | -- | Deploy BLE sensor daemon (installs bleak, paho-mqtt) |
| `--reboot "Day HH:MM"` | No | `"Sun 04:00"` | Weekly reboot cron schedule |

### Examples

```bash
# Basic setup.
sudo bash setup_clock.sh 10.0.0.214

# Bedroom clock: rotated display, BLE sensor daemon, custom hostname.
sudo bash setup_clock.sh 10.0.0.214 --hostname bedroom --rotate 90 --ble

# Remote setup via SSH.
ssh a@10.0.0.148 "bash -s" < tools/setup_clock.sh 10.0.0.214 --ble
```

### Phases

The script runs in five phases:

**Phase 1: System Configuration**

- Sets hostname via `/etc/hostname` and `hostnamectl`.
- Sets timezone to `America/Chicago`.
- Disables swap to reduce SD card wear.

**Phase 2: Bloat Removal**

- Purges unnecessary packages: Firefox, VLC, Geany, Thonny, CUPS,
  cross-compilers, VNC, Imager, kernel headers, and more.
- Installs `unclutter` for cursor hiding.

**Phase 3: Kiosk Configuration**

- Detects the display server: labwc (Wayland, Raspberry Pi OS
  Trixie+) or X11 (openbox/lxsession, older releases).
- Finds the Chromium binary (`chromium` on Trixie, `chromium-browser`
  on older).
- Enables auto-login to desktop session.
- Forces HDMI hotplug in `/boot/firmware/config.txt`.
- Handles screen rotation via `wlr-randr` (Wayland) or `xrandr`
  (X11), plus `display_rotate` in `config.txt` for boot-time
  rotation.

For **labwc/Wayland**, the script writes
`~/.config/labwc/autostart`:

```bash
# Screen rotation (if configured).
sleep 2; wlr-randr --output HDMI-A-1 --transform 90 2>/dev/null;

# GlowUp kiosk -- fullscreen Chromium.
sleep 5 && chromium --ozone-platform=wayland --password-store=basic \
    --noerrdialogs --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-restore-session-state --kiosk \
    'http://10.0.0.214:8420/home' &
```

For **X11**, the script writes `.desktop` files to
`/etc/xdg/autostart/` for cursor hiding, screen blanking disable,
optional rotation, and Chromium kiosk launch.

Key Chromium flags:

- `--password-store=basic` -- Skips the GNOME Keyring dialog that
  would otherwise block on a headless kiosk.
- `--kiosk` -- Fullscreen, no URL bar, no tabs.
- `--ozone-platform=wayland` -- Required for labwc/Wayland sessions.
- `--disable-session-crashed-bubble` -- Suppresses the "restore
  session" dialog after a crash or power loss.

A transparent cursor theme is used under Wayland to hide the mouse
pointer (unclutter does not work with Wayland compositors).

**Phase 4: BLE Sensor Daemon (optional, `--ble`)**

- Unblocks Bluetooth via `rfkill`.
- Creates a Python venv and installs `bleak` and `paho-mqtt`.
- Installs a systemd service (`glowup-ble-sensor.service`) that runs
  `python3 -m ble.sensor`.
- The BLE code and `ble_pairing.json` must be deployed separately
  via `deploy.sh`.

**Phase 5: Weekly Reboot Cron**

- Parses the `"Day HH:MM"` schedule string into cron fields.
- Replaces any existing GlowUp reboot cron entry (idempotent).
- Default: Sunday at 04:00.

### Camera Tile Retry Logic

On a Pi 3B, the NVR adapter may take 30-60 seconds to start serving
snapshots after boot.  The camera tile in `/home` retries failed
image loads on a timer, using the same URL with a cache-busting query
parameter.  The tile remains hidden until the first successful load.

---

## Photo Background

The page rotates through family photos as a blurred, low-opacity
background layer.  Two `div` elements alternate with a CSS crossfade
transition (2-second ease).  The photo list refreshes every 5 minutes
from `/api/home/photos`, and images rotate every 30 seconds.

In dark mode, photos are desaturated and dimmed further to avoid
lighting the room.

---

## Design Philosophy

The `/home` page has no controls -- no buttons, no sliders, no forms.
It is pure ambient information: time, weather, security state, sensor
readings.  Interaction happens through the main GlowUp dashboard or
the REST API.  The kiosk display is a window, not a door.

---

## See Also

- [Chapter 24: Persistent Services](24-persistent-services.md) --
  systemd services for the kiosk and BLE daemon
- [Chapter 28: BLE Sensors](28-ble-sensors.md) -- the BLE sensor
  daemon deployed by `--ble`
- [Chapter 11: REST API](11-rest-api.md) -- full API reference
  including `/api/home/*` endpoints
