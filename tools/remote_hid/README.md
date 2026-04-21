# glowup-remote-hid

Remote mouse + keyboard over TCP. Server runs on a Pi with an HDMI display
but no physical keyboard or mouse; client runs on a Mac and forwards your
trackpad and keyboard events to the Pi when you press the **toggle key**
(Right Option by default) to arm capture, and again to release it.

The server creates a composite Linux **uinput** device presenting as both a
mouse (`REL_X/Y`, left/middle/right buttons, vertical and horizontal
wheels) and a keyboard (every `EV_KEY` our macOS -> Linux keycode table
produces). Because events are emitted at the kernel level, this works
transparently under X11, labwc/Wayland, and the bare console — no
compositor-specific hooks.

## Wire protocol

Framed, little-endian:

```
uint16 length    (bytes to follow, = 1 + len(payload))
uint8  type      (1=move, 2=button, 3=scroll, 4=key)
bytes  payload
```

| type   | payload                                         |
|--------|-------------------------------------------------|
| move   | `int16 dx, int16 dy`                            |
| button | `uint8 button_id (1=L, 2=M, 3=R), uint8 pressed`|
| scroll | `int16 dx, int16 dy`                            |
| key    | `uint16 ev_key_code, uint8 pressed`             |

On connect, the client sends a 32-byte HMAC-SHA256 over the label
`GLOWUP-REMOTE-HID:v1` using the shared secret. Servers with no
`auth_token` in the config accept zero bytes and run unauthenticated —
LAN-only is the intended deployment.

## Server (Pi) install

```
scp tools/remote_hid/*.py a@pi:/tmp/remote_hid/
ssh a@pi 'sudo mkdir -p /opt/glowup-remote-hid/remote_hid && \
    sudo install -o root -g root -m 0644 /tmp/remote_hid/*.py \
        /opt/glowup-remote-hid/remote_hid/'
```

```
scp tools/remote_hid/deploy/99-uinput.rules a@pi:/tmp/
ssh a@pi 'sudo install -o root -g root -m 0644 /tmp/99-uinput.rules \
    /etc/udev/rules.d/ && sudo udevadm control --reload-rules && \
    sudo udevadm trigger'
```

```
scp tools/remote_hid/remote_hid.json.example a@pi:/tmp/
ssh a@pi 'sudo install -o a -g a -m 0600 /tmp/remote_hid.json.example \
    /etc/glowup/remote_hid.json'
```

Venv + evdev:

```
ssh a@pi 'sudo mkdir -p /opt/glowup-remote-hid && \
    sudo chown a:a /opt/glowup-remote-hid && \
    python3 -m venv /opt/glowup-remote-hid/venv && \
    /opt/glowup-remote-hid/venv/bin/pip install evdev'
```

Systemd unit:

```
scp tools/remote_hid/deploy/glowup-remote-hid.service a@pi:/tmp/
ssh a@pi 'sudo install -o root -g root -m 0644 \
    /tmp/glowup-remote-hid.service /etc/systemd/system/ && \
    sudo systemctl daemon-reload && \
    sudo systemctl enable --now glowup-remote-hid'
```

Edit `/etc/glowup/remote_hid.json` to set `auth_token` (or `null` for
unauthenticated LAN-only).

## Client (Mac) install

```
pip install pynput pyobjc-framework-Quartz
```

Grant **Input Monitoring** and **Accessibility** permission to the
terminal (or IDE) running the client. System Settings -> Privacy & Security.

Run:

```
python -m tools.remote_hid.client --host 10.0.0.123 --port 8429 \
    --secret-file ~/.glowup/remote_hid.token
```

(Omit `--secret-file` for unauthenticated LAN-only.)

Press **Right Option** to arm capture; local trackpad + keyboard are
forwarded to the Pi until you press **Right Option** again. You can
press **Ctrl-C** in the terminal to quit entirely.

To change the toggle key, pass `--toggle-key <macOS keyCode>`. The
client deliberately never forwards the toggle to the server, so pick
something you wouldn't want the Pi to see typed. Common choices
(macOS virtual keyCodes):

| Key           | keyCode |
|---------------|---------|
| Right Option  | 61 (default) |
| Right Command | 54      |
| Right Control | 62      |
| Caps Lock     | 57      |
| F13           | 105     |
| F19           | 80      |

## Testing without a Pi

Any Linux box with `/dev/uinput` works. On the Pi itself a quick smoke
test is to `evtest /dev/input/by-id/…-event-mouse` while pressing F13 on
the Mac client; each move should scroll the evtest log.
