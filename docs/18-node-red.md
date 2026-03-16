# Node-RED Integration

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

[Node-RED](https://nodered.org/) is a flow-based visual programming
tool popular in the home-automation community.  GlowUp's REST API
integrates with Node-RED using the built-in **http request** node —
no custom nodes or palettes are needed.

### Prerequisites

- Node-RED installed and running (standalone, or as a Home Assistant
  add-on — either works)
- GlowUp server (`server.py`) running and reachable from the
  Node-RED host

### Play Effect Flow

Import the following JSON into Node-RED (**Menu → Import → Clipboard**)
to create a one-click flow that plays an effect:

```json
[
    {
        "id": "glowup_play",
        "type": "http request",
        "name": "GlowUp Play",
        "method": "POST",
        "url": "http://GLOWUP_IP:8420/api/devices/DEVICE_IP/play",
        "headers": {
            "Authorization": "Bearer YOUR_AUTH_TOKEN",
            "Content-Type": "application/json"
        },
        "payload": "{\"effect\":\"aurora\",\"params\":{\"speed\":10.0,\"brightness\":60}}",
        "paytoqs": "ignore",
        "ret": "txt",
        "wires": []
    }
]
```

Replace `GLOWUP_IP`, `DEVICE_IP`, and `YOUR_AUTH_TOKEN` with your
actual values.

Wire an **inject** node to the input to trigger it manually, or
connect any Node-RED trigger (MQTT message, time scheduler, dashboard
button, webhook, etc.).

### Stop and Resume Flows

Use the same **http request** node pattern with these URLs:

| Action | Method | URL | Body |
|--------|--------|-----|------|
| **Stop** | POST | `http://GLOWUP_IP:8420/api/devices/DEVICE_IP/stop` | *(none)* |
| **Resume** | POST | `http://GLOWUP_IP:8420/api/devices/DEVICE_IP/resume` | *(none)* |

### Dynamic Effect Selection

To select the effect at runtime, wire a **function** node ahead of the
http request node that sets the payload dynamically:

```javascript
msg.payload = {
    effect: msg.effect || "aurora",
    params: msg.params || { speed: 10.0, brightness: 60 }
};
msg.headers = {
    "Authorization": "Bearer YOUR_AUTH_TOKEN",
    "Content-Type": "application/json"
};
return msg;
```

Then set the http request node's **Method** to `POST` and leave its
body configuration set to use `msg.payload`.

### Dashboard Button

If you use
[node-red-dashboard](https://flows.nodered.org/node/node-red-dashboard)
or
[FlexDash](https://flows.nodered.org/node/@flexdash/node-red-fd-corewidgets),
wire a **button** widget node into the play flow for a browser-based
control panel.

### Live Color Streaming

Node-RED can consume GlowUp's Server-Sent Events stream for real-time
zone color monitoring.  Use the
[node-red-contrib-sse-client](https://flows.nodered.org/node/node-red-contrib-sse-client)
node pointed at:

```
http://GLOWUP_IP:8420/api/devices/DEVICE_IP/colors/stream
```

Each event delivers the current HSBK values for all zones, which you
can feed into dashboard gauges, color displays, or further automation
logic.

### Notes

- **MQTT bridge:** If your Node-RED setup is MQTT-centric, create a
  simple flow that subscribes to an MQTT topic (e.g.,
  `glowup/play/porch`) and forwards the message payload to the
  GlowUp http request node.  This gives you MQTT control of GlowUp
  without modifying server.py.
- **Scheduler conflict:** Like any external client, effects started
  via Node-RED set a phone override on the device.  Use the resume
  endpoint to hand control back to the GlowUp scheduler.
- **Error handling:** Wire the http request node's second output
  (error) to a **debug** node or a **catch** node to log failures.
- **Untested:** This integration has not yet been tested against a
  live Node-RED instance.  If you try it, please open an issue
  at the [GitHub repo](https://github.com/pkivolowitz/lifx/issues)
  with any corrections or suggestions.
