# Chapter 35: Service vs. Adapter — Architectural Decision Guide

This chapter exists so that the next person (human or AI) adding a
new sensor, integration, or device family to GlowUp picks the right
pattern on the first attempt.  Both patterns are valid; choosing
the wrong one costs weeks of silent-data-loss bugs, which is the
ground truth that motivated this document.

---

## TL;DR

> **A SERVICE is required if and only if both of these are true:**
>
> 1. The data source lives on a **different host** than the hub.
> 2. The data arrives via a **long-lived push protocol** that the
>    hub would have to subscribe to across the network (MQTT
>    subscribe, BLE notify, webhook listener, websocket subscribe).
>
> **Otherwise, write an in-process adapter on the hub.**

That single rule is the whole guide.  Everything below is the
reasoning, the consequences, and the worked examples so future-you
trusts the rule when the pressure to take a shortcut shows up.

---

## The two patterns

### Pattern A: in-process adapter

An **adapter** is a Python class on the hub that subclasses one of
`AdapterBase`, `MqttAdapterBase`, `PollingAdapterBase`, or
`AsyncPollingAdapterBase` (see [Chapter 27](27-adapter-base.md)).
It runs as a thread or coroutine inside the hub server process.
It is started by `adapters/run_adapter.py` as one of the
`glowup-adapter@<name>.service` systemd units on the hub.

What an adapter is good at:

- Talking to a device the hub can reach **outward** over IP — HTTP,
  SNMP, REST, websockets initiated by the hub, cloud APIs.  These
  are request/response or hub-initiated subscriptions, and the hub
  is the active party.
- Talking to anything that lives on the hub itself: hardware
  attached to the hub Pi, files in `/etc/glowup`, sockets bound on
  the hub, the hub's own scheduler.
- Subscribing to MQTT topics on the **hub's own broker** (where
  the wire is local and a dropped TCP segment is impossible).

What an adapter is **bad** at:

- Subscribing to MQTT topics on a **remote** broker.  The TCP
  socket can half-die, the `paho` client can stop receiving without
  noticing, retained messages will silently lie to it about the
  current state, and there is no honest way to detect the silence
  because absence of messages and absence of failure look the same
  on the consumer side.  Every "zombie reconnect" bug in this
  codebase has come from this failure mode.

### Pattern B: standalone service

A **service** is a single-file Python program with its own
`systemd` unit that runs on the **host where the data originates**
(e.g. broker-2 for radio sensors).  It opens a `paho` client to
the **hub's** mosquitto and **publishes** signals there.  It does
not subclass `AdapterBase`; it joins the SOE pipeline at the same
point an adapter would, but from the producer side of the wire.

What a service is good at:

- Talking to the radio / hardware that physically lives on its host.
- Translating raw device messages into the SignalBus signal shape.
- Publishing those signals **to the hub** over the network.  The
  publishing direction matters: if the network blips, paho returns
  a non-zero `rc` on the publish call and the publisher can log,
  retry, or back off.  Publishers see the network; subscribers do
  not.
- Hosting a small HTTP API for direct queries that bypass the hub
  (see `glowup-zigbee-service`'s `/devices` and `/history`
  endpoints — the dashboard talks to broker-2 directly for power
  history rather than asking the hub to proxy).
- Carrying its own per-device persistent state (zigbee_service
  owns `/var/lib/glowup-zigbee/history.db` and the hub does not
  need to replicate it).

What a service is **bad** at:

- Anything on the hub itself.  Spinning up a separate process to
  subscribe to localhost-mosquitto would just be ceremony.
- Polling outward to a remote IP device — that's what an adapter
  on the hub is for.  The "service" pattern is about owning a
  local-only data source, not about being remote for its own sake.

---

## The decision rule, expanded

```
            ┌───────────────────────────────────────┐
            │ Where does the raw data ORIGINATE?    │
            └────────────────────┬──────────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │                                     │
   ┌──────────▼──────────┐               ┌──────────▼──────────┐
   │ On the hub itself   │               │ On a different host │
   │ (hub hardware,      │               │ (radio attached to  │
   │  hub filesystem,    │               │  broker-2, sensor   │
   │  hub scheduler)     │               │  paired with        │
   └──────────┬──────────┘               │  broker-2's BT,     │
              │                          │  Z2M on broker-2)   │
              │                          └──────────┬──────────┘
              │                                     │
              ▼                                     ▼
         ┌─────────┐               ┌────────────────────────────┐
         │ ADAPTER │               │ Can the hub reach the data │
         └─────────┘               │ via REQUEST/RESPONSE only? │
                                   │ (HTTP poll, cloud REST,    │
                                   │  hub-initiated websocket)  │
                                   └──────────┬─────────────────┘
                                              │
                              ┌───────────────┴────────────────┐
                              │                                │
                              ▼                                ▼
                         ┌─────────┐                     ┌─────────┐
                         │ ADAPTER │                     │ SERVICE │
                         └─────────┘                     └─────────┘
                       (hub polls                       (host pushes
                        outward — the                    signals to hub —
                        active party)                    publisher sees
                                                         network failures)
```

**Why "request/response" gets to use an adapter even across hosts:**
each call returns an immediate success or failure code.  The hub
is the active party and learns about every failure synchronously.
An NVR that doesn't answer an HTTP poll throws a clear error; an
MQTT subscribe that stops receiving messages does nothing.  The
question is never "where does the network live" but "who can
detect it broke."

**Why long-lived subscribes across the network are forbidden:**
in MQTT, an open subscribe that misses messages is *the same wire
state* as an open subscribe that's caught up.  paho cannot tell
you what you didn't receive, because it doesn't know what was
sent.  Watchdogs, heartbeats, and reconnect-on-timeout policies
are all attempts to paper over this fundamental asymmetry, and
they all fail in surprising ways.  Inverting the direction —
making the publisher cross the network instead of the subscriber
— moves the failure detection to the side of the wire that can
actually observe it.

---

## Worked examples (current GlowUp components)

| Component                       | Lives on   | Data origin              | Pattern  | Why                                                                                       |
|---------------------------------|------------|--------------------------|----------|-------------------------------------------------------------------------------------------|
| `glowup-zigbee-service`         | broker-2   | Z2M on broker-2          | SERVICE  | Z2M speaks MQTT only on broker-2's localhost; hub would have to subscribe across network |
| `glowup-ble-sensor` (post-pivot)| broker-2   | BLE radio on broker-2    | SERVICE  | Same: HAP-BLE notifies are local to broker-2's BT chip; hub cannot subscribe to BLE      |
| `MatterAdapter`                 | hub        | Matter devices on LAN    | ADAPTER  | Matter is hub-resident; the hub IS the Matter coordinator                                |
| `NvrAdapter`                    | hub        | Reolink cameras (LAN IP) | ADAPTER  | Hub polls cameras over HTTP/onvif; request/response, hub is active party                  |
| `PrinterAdapter`                | hub        | Brother printer (LAN IP) | ADAPTER  | Hub polls SNMP/HTTP; request/response                                                     |
| `VivintAdapter`                 | hub        | Vivint cloud + PubNub    | ADAPTER  | Hub-initiated cloud connection; even though PubNub is push-ish, the hub establishes it    |
| `HDHomeRunAdapter`              | hub        | HDHomeRun tuner (LAN IP) | ADAPTER  | Hub polls tuner directly over HTTP                                                        |
| `glowup-server` itself          | hub        | the universe             | (host)   | Not in the pattern — it's the consumer                                                    |

If a future device family doesn't fit cleanly into any row above,
walk it through the decision tree.  If you find yourself rationalizing
a long-lived cross-host subscribe, **stop** — the answer is the
service pattern even if it feels like more setup.  The setup is
one-time; the silent-data-loss bugs are forever.

---

## Pattern A: implementation checklist (in-process adapter)

1. Pick the right base class:
   - `MqttAdapterBase` — local MQTT subscribe + signal write
   - `PollingAdapterBase` — periodic synchronous poll (HTTP, SNMP)
   - `AsyncPollingAdapterBase` — periodic asyncio poll (cloud APIs)
2. Subclass it in `adapters/<name>_adapter.py` or
   `contrib/adapters/<name>_adapter.py` (third-party / vendor code
   goes under `contrib/`).
3. Implement `_handle_message` (MQTT) or `_poll_once` (polling).
4. Translate every reading into a `SignalBus.write(name, value)`
   call using the `{source}:{property}` naming convention.
5. Add a factory function and `AdapterSpec` entry in
   `adapters/run_adapter.py`'s `ADAPTERS` dict.
6. Add a `glowup-adapter@<name>.service` systemd unit (templated
   on the existing units in the deploy scripts).
7. Add the adapter attribute name to `handlers/static.py`'s
   `/api/status` health loop and to `handlers/discovery.py`'s
   `_ADAPTER_ATTRS` dict so `/api/adapters/{name}/restart` works.
8. Tests: write `tests/test_<name>_adapter.py` exercising
   `_handle_message` or `_poll_once` with synthetic input.
9. Docs: add a chapter to `docs/` and link it from `MANUAL.md`.

## Pattern B: implementation checklist (standalone service)

1. Create `<name>_service/` at the repo root with:
   - `service.py` — single-file Python program, no external
     dependencies beyond `paho-mqtt` and the protocol library
     for the data source.  See `zigbee_service/service.py` for
     the canonical layout.
   - `glowup-<name>-service.service` — systemd unit.
   - `README.md` — deployment, env-var config, HTTP endpoints.
2. The service must:
   - Connect to the local data source over the cheap path
     (localhost MQTT, local hardware, local file).
   - Open a `paho.mqtt.client.Client` to the **hub's** mosquitto
     using `connect_async` + `loop_start` so reconnects are
     handled by paho itself.  Read the hub address from an env
     var (`GL<NAME>_HUB_BROKER`) so the systemd unit owns the
     config — never hand-edit.
   - Publish each normalized property as a discrete signal on
     `glowup/signals/{source}:{property}` with `qos=0`, payload
     a stringified float (or JSON for non-scalars).  This is the
     **only** schema the hub's `_on_remote_signal` callback
     understands — see [Chapter 21](21-soe-pipeline.md).
   - Log every publish with the paho `rc` so a network failure
     is visible in `journalctl -u glowup-<name>-service`.
   - Optionally expose an HTTP API on a per-service port for
     direct queries (history, current state) that bypass the
     hub.  Keep this stdlib-only (`http.server.HTTPServer`)
     unless you really need flask/fastapi.
3. Add a chapter to `docs/` describing the service, link it from
   `MANUAL.md`, and update [Chapter 21](21-soe-pipeline.md)'s
   adapters table to include the service in the producer column
   (note explicitly that it is **not** an `AdapterBase` subclass
   — services join the SOE pipeline at the same point but from
   the producer side).
4. Add a deploy step to the appropriate `deploy/scripts/` script
   that pushes the service files to the host that owns the data.
   For broker-2 services, edit `deploy/scripts/deploy-broker2.sh`.
5. Tests: services typically have a unit-test file at
   `tests/test_<name>_service.py` exercising the publish path
   with a stub broker.  The hub side is already covered by
   `tests/test_signal_power_recording.py`'s `_on_remote_signal`
   simulator — extend it with an assertion for any new signal
   shape your service introduces.
6. Add the service to the periodic broker-2 health-check
   (currently broker-2 only has `glowup-zigbee-service` and
   `glowup-ble-sensor`; the satellite deep-check protocol covers
   voice satellites; if you add a non-radio service, decide
   whether it needs its own probe).

## What both patterns owe the rest of the system

Regardless of which pattern you pick, every new producer must:

- **Use the SignalBus signal-name convention** `{source}:{property}`.
  Not `{source}/{property}`, not `{source}_{property}`.  The colon
  is what `_on_remote_signal` and the operator framework parse on.
- **Document itself** in MANUAL.md, in its own chapter, and in
  whatever architecture diagrams it touches.  A new producer that
  isn't in the SOE_ARCHITECTURE table will get forgotten in three
  months.
- **Be discoverable from `/api/home/health`** in some form — either
  as an adapter entry, a `broker2_signals_last_ts`-style staleness
  probe, or a satellites-style deep-check.  An invisible producer
  is a silently-failing producer.
- **Have a clear rollback story** in the deploy script comment.
  How do you put it back if the new version fails?  Leave the
  answer in the file, not in someone's head.

---

## When to revisit an existing choice

Sometimes a component is on the wrong side of the line and nobody
notices for months.  The signs:

- The "running" health field stays green but the data is stale by
  hours (ground truth: `mosquitto_sub -h <consumer-broker> -t
  '<topic>/#' -W 30` returns retained messages with old
  timestamps and zero new ones).  Almost always a cross-host
  subscribe that has gone half-open.
- A test exists for the adapter's `_handle_message` but no
  integration test verifies that a real producer is publishing on
  the topic the test simulates.  The simulator stays green forever
  while the wire goes silent.
- The codebase contains words like "watchdog," "auto-reconnect
  loop," "zombie cleanup," or "retained-message defense" applied
  to the consumer side of an MQTT subscribe.  These are all
  symptoms of treating cross-host subscribe as if it could be
  made reliable through code; it cannot.  The right fix is to
  invert the direction of the wire, not to add another retry
  loop.
- The same subsystem has appeared in three or more bug-fix commits
  with similar narratives ("fix zombie state", "add reconnect",
  "clear stale cache").  The bug is structural, not logical —
  this is a service-pattern conversion in disguise.

When you see those signs, do the conversion.  It is almost always
smaller than it looks, because the producer already knows how to
talk to its data source — the only change is **where it sends the
result**.

---

## See also

- [Chapter 21: SOE Pipeline](21-soe-pipeline.md) — how signals
  flow into operators and emitters once a producer (adapter or
  service) writes them
- [Chapter 27: Adapter Base Classes](27-adapter-base.md) — the
  base classes you subclass when you pick the adapter pattern
- [Chapter 29: Zigbee Service](29-zigbee-service.md) — the
  canonical example of the service pattern, with the "what
  changed and why" story
- `feedback_read_the_producer_first.md` — debugging discipline
  for "subscriber sees nothing" bugs.  When in doubt, grep the
  producer.
