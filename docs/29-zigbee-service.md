# Chapter 29: Zigbee Service (`glowup-zigbee-service`)

> **What changed:** Until 2026-04, GlowUp had an in-process
> `ZigbeeAdapter` running on the hub (.214) that subscribed across
> the network to broker-2's mosquitto and pulled Z2M traffic
> back to the hub.  That adapter is gone.  In its place,
> `glowup-zigbee-service` runs as a standalone systemd unit
> **on broker-2**, owns Zigbee end-to-end, and **publishes**
> normalized signals cross-host to the hub instead of the hub
> subscribing across the network for them.

## Why it moved

The old `ZigbeeAdapter` was the source of seven commits' worth of
flip-flop fixes for the same class of bug: half-open MQTT sockets,
broken watchdog reconnects, retained-message replays, slow-reporting
devices being silently dropped.  Every one of those incidents came
from the same root cause — the hub was subscribing **across the
network** to broker-2's mosquitto, and there is no honest way to
know what messages a remote subscribe failed to receive.  When the
network blips, a publisher gets an immediate `rc != 0` and can
react; a subscriber gets silence and has to guess (with watchdogs,
heartbeats, retries, and prayers).

The fix was to invert the direction of the network coupling.
`glowup-zigbee-service`:

- subscribes to Z2M's `zigbee2mqtt/#` on **localhost mosquitto** on
  broker-2 (where it cannot fail mysteriously);
- normalizes every property the same way the old adapter did
  (boolean → 0.0/1.0, battery ÷ 100, raw temp/humid, etc.);
- opens **its own paho client** to the hub mosquitto at
  `192.0.2.214:1883` and **publishes** each normalized property as
  a discrete signal on `glowup/signals/{device}:{property}`.

The hub no longer hosts a Zigbee adapter at all.  It already
subscribes to `glowup/signals/#` for every other device-origin
signal source, and the existing `_on_remote_signal` callback in
`server.py` feeds those messages into the local `SignalBus` and
into `PowerLogger.record()` — same code path as BLE sensors, no
Zigbee-specific branch anywhere in the hub.

## Architecture

```
  Zigbee radio
        │
        ▼
      Zigbee2MQTT  (broker-2)
        │
        ▼
  localhost mosquitto  (broker-2)
        │
        ▼ subscribe zigbee2mqtt/#
┌──────────────────────────────────┐
│    glowup-zigbee-service         │  ← standalone systemd unit
│    HTTP :8422                    │     on broker-2 (.123)
│    + paho cross-host publisher   │
└──────────────────────────────────┘
     │                       │
     │ HTTP                  │ MQTT publish (cross-host)
     ▼                       ▼
 Dashboard /power      hub mosquitto (.214)
                            │ subscribe glowup/signals/#
                            ▼
                       _on_remote_signal
                            │
                            ▼
                       SignalBus + PowerLogger
                            │
                            ▼
                       Operators, automations, /power
```

**broker-2 owns the data.**  The service stores per-device history
in its own SQLite database at `/var/lib/glowup-zigbee/history.db`
and answers dashboard queries directly over HTTP.  That history is
authoritative — the hub does not need to store Zigbee-specific
data and the `/power` page can ask broker-2 for it instead of
reconstructing it from `_on_remote_signal` deltas.

## Source and operation

- Repo source: [`zigbee_service/service.py`](../zigbee_service/service.py)
- Repo README (deployment, env vars, HTTP endpoints): [`zigbee_service/README.md`](../zigbee_service/README.md)
- Systemd unit: [`zigbee_service/glowup-zigbee-service.service`](../zigbee_service/glowup-zigbee-service.service)
- Restart on broker-2: `ssh mortimer.snerd@192.0.2.123 sudo systemctl restart glowup-zigbee-service`
- Logs: `ssh mortimer.snerd@192.0.2.123 sudo journalctl -u glowup-zigbee-service -f`

## How the hub sees Zigbee data

The hub's view is identical to BLE and any other signal producer:

```python
def _on_remote_signal(client, userdata, message):
    # topic: glowup/signals/{device}:{property}
    parts = message.topic.split("/", 2)
    sig_name = parts[2]               # "BYIR:power"
    value = json.loads(message.payload)
    signal_bus.write_local(sig_name, value)
    power_logger.record(*sig_name.split(":", 1), float(value))
```

`server.py` also stamps `GlowUpRequestHandler.broker2_signals_last_ts`
on every non-`time:*` signal arriving here.  That timestamp is the
liveness probe behind the `zigbee` field in `/api/home/health` —
"have we heard a non-time signal from broker-2 in the last
`BROKER2_SIGNALS_STALE_SEC` seconds."  See
[`handlers/dashboard.py`](../handlers/dashboard.py) and
`feedback_read_the_producer_first.md` for the rationale.

## What's broken (follow-up)

`POST /api/zigbee/set` on the hub is the one piece that did not
get rewired during the broker-2 pivot.  It still references the
removed in-process `_zigbee_adapter` proxy and currently returns
`503` for every call, which means **plug on/off toggles from the
`/power` dashboard do not actually reach the device.**  The fix is
the inverse direction of the data flow above: have the hub publish
`zigbee2mqtt/{device}/set` cross-host to broker-2's mosquitto (or
POST to the service's own `/devices/{name}/state` HTTP endpoint —
see the service README for the contract).  Tracked as a follow-up
in the session that introduced this chapter.

## Cross-references

- [Chapter 19: MQTT Topology](19-mqtt.md) — broker layout, topic conventions
- [Chapter 21: SOE Pipeline](21-soe-pipeline.md) — how signals flow into operators and emitters
- [Chapter 27: Adapter Base Classes](27-adapter-base.md) — note that
  `glowup-zigbee-service` is **not** an `AdapterBase` subclass; it
  is a standalone service that joins the SOE pipeline at the same
  point that an `AdapterBase` subclass would, just without
  inheriting any of the adapter-base machinery
- [Chapter 34: Power Monitoring](34-power-monitoring.md) — how
  `_on_remote_signal` feeds Zigbee plug readings into `PowerLogger`
