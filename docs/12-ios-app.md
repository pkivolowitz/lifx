# GlowUp iOS App

The GlowUp iOS app is a native SwiftUI remote control for your LIFX
devices.  It communicates with `server.py` over HTTP(S) and provides
live color monitoring, auto-generated parameter UI, and
Keychain-secured authentication.

### Connectivity Options

The app connects to a running `server.py` instance.  There are
several ways to make this work depending on your setup:

| Method | Setup | Use Case |
|--------|-------|----------|
| **LAN (direct IP)** | Point the app at `http://<pi-ip>:8420` | Controlling lights from home — no tunnel, no account, simplest setup |
| **Cloudflare Tunnel** | See [TUNNEL.md](TUNNEL.md) | Secure remote access from anywhere without opening router ports |
| **Tailscale / WireGuard** | Install on Pi and phone | Private VPN mesh — works from anywhere, free for personal use |
| **Port forwarding** | Forward 8420 on your router | Works remotely but exposes a port to the internet |

For most users, **LAN mode is all you need** — your phone and the Pi
are on the same WiFi, so just enter the Pi's local IP address as the
server URL in the app's Settings screen.

### Building the App

**Requirements:**

- macOS with Xcode 16+ installed
- Apple ID signed into Xcode (free tier works for simulator testing)
- For deploying to a physical iPhone: a free Apple ID is sufficient for
  7-day provisioning profiles; a $99/yr Apple Developer account removes
  that expiration

**Steps:**

1. Open the project:
   ```bash
   open ios/GlowUp.xcodeproj
   ```
2. In Xcode, select the **GlowUp** target, go to **Signing &
   Capabilities**, check **Automatically manage signing**, and select
   your Apple ID team
3. If the bundle identifier `com.kivolowitz.glowup` is taken under your
   team, change it to something unique (e.g.,
   `com.yourname.glowup`)
4. Select an iPhone simulator or your connected device as the run
   destination
5. Build and run (**Cmd+R**)

### Running on Your iPhone

To install on a physical device for the first time:

1. Connect your iPhone to your Mac via USB
2. On the phone, tap **Trust This Computer** when prompted
3. On the phone, enable **Developer Mode**: Settings → Privacy &
   Security → Developer Mode → toggle on and restart
4. In Xcode, select your iPhone from the run destination dropdown (top
   toolbar, next to the Play button)
5. Build and run (**Cmd+R**) — Xcode will automatically create a
   provisioning profile
6. On first launch, you may need to trust the developer certificate on
   the phone: **Settings → General → VPN & Device Management** → tap
   your developer certificate → Trust

After the first wired install, you can enable wireless debugging in
Xcode: **Window → Devices and Simulators**, select your phone, and
check **Connect via network**.

### App Screens

1. **Device List** — Shows all configured devices with name, product
   type, group, and current effect.  Virtual multizone groups are
   prefixed with "Group:" and display a group icon with member count.
   Pull-to-refresh fetches the latest state.

2. **Device Detail** — Live color strip visualization (SSE-fed at 4 Hz),
   current effect info, power toggle, stop button, restart button, and
   a link to change the effect.  Virtual groups show the combined zone
   count, member device IPs, and type "Virtual Group" (see screenshot
   below).

3. **Effect Picker** — Lists all registered effects with descriptions
   and parameter counts.

4. **Effect Config** — Auto-generated parameter UI built from the
   server's `Param` metadata.  Sliders for numeric params, pickers
   for choice params, text fields for strings.  Tap "Play" to send
   the command.  **Save as Defaults** pushes the current parameter
   values to the server so the scheduler uses them — no need to edit
   `server.json` by hand.  Parameter values persist across app
   sessions.

5. **Settings** — Server URL and API token configuration.  Token is
   stored in the iOS Keychain.  Includes a "Test Connection" button
   and an About section displaying the app icon, version, and license
   information.

#### Virtual Group Detail

The screenshot below shows the Device Detail screen for a virtual
multizone group named "porch" — two physical string lights (10.0.0.23
and 10.0.0.62) combined into a single 144-zone animation surface.

<p align="center">
  <img src="multizone.PNG" alt="Virtual multizone group detail" width="300">
</p>

