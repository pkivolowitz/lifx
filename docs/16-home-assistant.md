# Home Assistant Integration

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp's REST API works with [Home Assistant](https://www.home-assistant.io/)
out of the box using HA's built-in
[`rest_command`](https://www.home-assistant.io/integrations/rest_command/)
integration.  No custom component or code changes are needed — just add a
few lines to your HA `configuration.yaml`.

### Prerequisites

- GlowUp server (`server.py`) running on your Pi, Mac, Windows PC, or Linux box
- An auth token configured in `server.json`
  (see [Server Configuration](#server-configuration) for how to generate one)
- Home Assistant installed and accessible on the same network
  (or reachable via Cloudflare Tunnel / VPN)

### Configuration

Add the following to your Home Assistant `configuration.yaml`:

```yaml
rest_command:
  glowup_play:
    url: "http://GLOWUP_IP:8420/api/devices/{{ ip }}/play"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTH_TOKEN"
      Content-Type: "application/json"
    payload: '{"effect":"{{ effect }}","params":{{ params | default("{}")}}}'
    content_type: "application/json"

  glowup_stop:
    url: "http://GLOWUP_IP:8420/api/devices/{{ ip }}/stop"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTH_TOKEN"

  glowup_resume:
    url: "http://GLOWUP_IP:8420/api/devices/{{ ip }}/resume"
    method: POST
    headers:
      Authorization: "Bearer YOUR_AUTH_TOKEN"
```

Replace `GLOWUP_IP` with the IP address (or hostname) of the machine
running `server.py`, and `YOUR_AUTH_TOKEN` with the token from your
`server.json`.  If using a Cloudflare Tunnel, replace the URL with
your tunnel hostname (e.g., `https://lights.yourdomain.com`).

Restart Home Assistant after saving the file, or reload the REST
command integration from **Developer Tools → YAML → REST commands**.

### Automation Example

Trigger an effect automatically — for example, play aurora at sunset:

```yaml
automation:
  - alias: "GlowUp aurora at sunset"
    trigger:
      - platform: sun
        event: sunset
        offset: "-00:30:00"
    action:
      - service: rest_command.glowup_play
        data:
          ip: "192.0.2.62"
          effect: "aurora"
          params: '{"speed": 10.0, "brightness": 60}'

  - alias: "GlowUp off at midnight"
    trigger:
      - platform: time
        at: "00:00:00"
    action:
      - service: rest_command.glowup_stop
        data:
          ip: "192.0.2.62"
```

For virtual multizone groups, use the group identifier (e.g.,
`group:porch`) as the `ip` value.

### Dashboard Button

Add a button to your Lovelace dashboard that plays an effect on tap:

```yaml
type: button
name: "Aurora"
icon: mdi:aurora
tap_action:
  action: call-service
  service: rest_command.glowup_play
  data:
    ip: "192.0.2.62"
    effect: "aurora"
    params: '{"speed": 10.0, "brightness": 80}'
```

### Notes

- **Scheduling:** GlowUp has its own scheduler with sunrise/sunset
  awareness built into `server.py`.  You can use either GlowUp's
  scheduler or HA automations — but not both on the same device
  simultaneously, as they will conflict.  If using HA automations,
  leave the `schedule` section out of your `server.json`.
- **Phone override:** Effects started via HA (or any HTTP client)
  set a phone override on the device, pausing the GlowUp scheduler.
  Use `rest_command.glowup_resume` to clear the override and return
  to the scheduled program.
- **Live color monitoring:** HA's `rest_command` does not support
  Server-Sent Events, so live zone color streaming requires the
  GlowUp iOS app or a direct SSE client.
- **No custom component needed:** A full HA custom component
  (registering GlowUp devices as HA light entities) is possible but
  unnecessary for most use cases.  The REST command approach covers
  play, stop, resume, and power control with no additional code.
- **Untested:** This integration has not yet been tested against a
  live Home Assistant instance.  If you try it, please open an issue
  at the [GitHub repo](https://github.com/pkivolowitz/glowup/issues)
  with any corrections or suggestions.
