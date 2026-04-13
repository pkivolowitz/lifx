# glowup-zigbee-service

Single-file HTTP + MQTT bridge that runs **on broker-2** and owns
Zigbee end-to-end for GlowUp. Replaces the fragile hub-side
`zigbee_adapter` + `PowerLogger` chain that spawned seven commits of
flip-flop fixes.

## Architecture

```
  Zigbee radio
        │
        ▼
      Z2M  (broker-2)
        │
        ▼
  localhost mosquitto  (broker-2)
        │
        ▼ subscribe zigbee2mqtt/#
┌──────────────────────────────────┐
│    glowup-zigbee-service         │  ← THIS module (broker-2)
│  HTTP :8422  +  paho publisher   │
└──────────────────────────────────┘
     │                       │
     │ HTTP                  │ MQTT publish
     ▼                       ▼
 Dashboard              hub mosquitto  →  SOE signal bus
```

**broker-2 owns the data.** The service stores history in
`/var/lib/glowup-zigbee/history.db`, responds to dashboard queries
directly, and publishes real-time signals to hub mosquitto for
operator/SOE consumption.

## Deployment

Target: **broker-2** (Pi, `a@10.0.0.123`).

```bash
# Copy code
sudo mkdir -p /opt/glowup-zigbee /var/lib/glowup-zigbee
sudo chown a:a /opt/glowup-zigbee /var/lib/glowup-zigbee
rsync -av service.py a@10.0.0.123:/opt/glowup-zigbee/

# Create venv
ssh a@10.0.0.123 'python3 -m venv /opt/glowup-zigbee/venv'
ssh a@10.0.0.123 '/opt/glowup-zigbee/venv/bin/pip install paho-mqtt'

# Install systemd unit
sudo cp glowup-zigbee-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now glowup-zigbee-service

# Verify
curl -s http://10.0.0.123:8422/health
curl -s http://10.0.0.123:8422/devices | jq
```

## HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + version |
| GET | `/devices` | All current device state |
| GET | `/devices/{name}` | Single device current state |
| GET | `/devices/{name}/history?hours=24&resolution=60` | Power history |
| GET | `/summary?days=7&rate=0.13` | Aggregate kWh + cost |
| POST | `/devices/{name}/state` | Body `{"state":"ON"\|"OFF"}` |

## Config (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `GLZ_HTTP_BIND` | `0.0.0.0` | HTTP listen address |
| `GLZ_HTTP_PORT` | `8422` | HTTP listen port |
| `GLZ_Z2M_BROKER` | `localhost` | Z2M's mosquitto host |
| `GLZ_Z2M_PORT` | `1883` | Z2M's mosquitto port |
| `GLZ_Z2M_PREFIX` | `zigbee2mqtt` | Z2M base topic |
| `GLZ_HUB_BROKER` | `10.0.0.214` | Hub mosquitto host (empty to disable signal publish) |
| `GLZ_HUB_PORT` | `1883` | Hub mosquitto port |
| `GLZ_HUB_SIGNAL_PREFIX` | `glowup/signals` | Hub signal bus prefix |
| `GLZ_DB_PATH` | `/var/lib/glowup-zigbee/history.db` | sqlite history path |
| `GLZ_RATE_USD_PER_KWH` | `0.13` | Default cost rate |
| `GLZ_LOG_LEVEL` | `INFO` | Python log level |

All configured by the systemd unit — never hand-edit.

## Why it replaces what it replaces

The current hub-side `zigbee_adapter` subscribes to broker-2 mosquitto
across the network. That cross-host subscribe loop is where every
"zombie reconnect" bug lives — half-open sockets, broken watchdogs,
bad retry policies. Seven commits worth of python band-aids.

This service inverts the direction: broker-2 **publishes** to hub
instead of hub **subscribing** from broker-2. Publishers see failures
immediately; subscribers have to guess with watchdogs. Same wire,
opposite role, completely different failure profile.
