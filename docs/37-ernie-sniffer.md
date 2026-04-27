# Chapter 37: Ernie Sniffer — BLE + TPMS Persistence

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

Ernie (`.153`, Odroid N2+) runs two RF capture pipelines that
publish to the hub mosquitto: a BLE advertisement sniffer (`v2`,
per-MAC state machine with RPA-clustering hooks) and an `rtl_433`
decoder that emits TPMS frames from passing vehicles on
315/433&nbsp;MHz.  The hub persists both streams to PostgreSQL and
drives the `/ernie` dashboard from those tables.

This chapter documents the persistence layer, not the capture
daemons themselves — those live on ernie and have their own
configuration in `contrib/sensors/ble_sniffer.py` and the
`rtl_433` systemd unit.

---

## Architecture

```
+---------------------+                            +----------------------+
| ernie (.153)        |                            | hub (.214)           |
|                     |                            |                      |
|  ble_sniffer v2     |  glowup/ble/seen/<mac>     |  BleSnifferLogger    |
|  (nRF52840, Sniffle)|   glowup/ble/events/<mac>  |  ----------------->  |
|                     | -------------------------> |       UPSERT /       |
|  rtl_433 TPMS       |  glowup/tpms/events        |  INSERT              |
|  (RTL-SDR, 315/433) |                            |  TpmsLogger          |
|                     |                            |  ----------------->  |
|  pi_thermal_sensor  |  glowup/hardware/thermal/  |  ThermalLogger       |
|                     |   ernie                    |  ----------------->  |
+---------------------+                            |                      |
                                                   |     PostgreSQL       |
                                                   |  (.111 jail, role    |
                                                   |    glowup)           |
                                                   +----------------------+
                                                            |
                                                      /ernie page
                                                      (polled via REST)
```

All three loggers live in `infrastructure/` alongside the existing
`thermal_logger` and `power_logger`.  They follow the same
guarded-import pattern: missing `psycopg2` or `paho-mqtt`
downgrades the affected logger to a no-op without preventing the
server from starting.

---

## Topics consumed

| Topic | Retained? | Consumer | Destination |
|-------|-----------|----------|-------------|
| `glowup/tpms/events` | No | `TpmsLogger` | `tpms_observations` (append) |
| `glowup/ble/seen/<mac>` | Yes | `BleSnifferLogger` | `ble_seen` (UPSERT by `mac`) |
| `glowup/ble/events/<mac>` | No | `BleSnifferLogger` | `ble_events` (append) |
| `glowup/hardware/thermal/ernie` | No | `ThermalLogger` (shared) | `thermal_readings` (append) |

Retained messages on `glowup/ble/seen/<mac>` replay on every
subscribe, so the `ble_seen` catalog is restored whenever the hub
reconnects — even cold-start the dashboard shows every MAC the
sniffer currently believes exists.

---

## Schemas

```sql
CREATE TABLE tpms_observations (
    id            BIGSERIAL PRIMARY KEY,
    timestamp     DOUBLE PRECISION NOT NULL,
    model         TEXT NOT NULL,
    sensor_id     TEXT NOT NULL,
    pressure_kpa  REAL,
    temperature_c REAL,
    battery_ok    SMALLINT,
    payload       JSONB
);
CREATE INDEX idx_tpms_sensor_ts ON tpms_observations(model, sensor_id, timestamp);
CREATE INDEX idx_tpms_ts        ON tpms_observations(timestamp);

CREATE TABLE ble_seen (
    mac            TEXT PRIMARY KEY,
    first_heard_ts DOUBLE PRECISION,
    last_heard_ts  DOUBLE PRECISION,
    gone           SMALLINT DEFAULT 0,
    payload        JSONB,
    updated_ts     DOUBLE PRECISION
);

CREATE TABLE ble_events (
    id        BIGSERIAL PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    mac       TEXT NOT NULL,
    event     TEXT,
    payload   JSONB
);
CREATE INDEX idx_ble_events_mac_ts ON ble_events(mac, timestamp);
CREATE INDEX idx_ble_events_ts     ON ble_events(timestamp);
```

Each row carries the full upstream payload in a `JSONB` column so
future work (vehicle clustering, RPA fingerprinting, protocol
family rollups) can mine fields the loggers don't yet extract,
without a schema change.

---

## REST endpoints

All four `/api/ernie/*` endpoints are served by the mixin at
[`handlers/ernie.py`](../handlers/ernie.py) and now read from
PostgreSQL.  They survive server restarts without losing history.

| Method | Path | Source |
|--------|------|--------|
| GET | `/ernie` | static HTML (`static/ernie.html`) |
| GET | `/api/ernie/ble` | `BleSnifferLogger.catalog(window_s=600)` — last 10 min by default |
| GET | `/api/ernie/ble/events` | `BleSnifferLogger.events_tail(n=200)` |
| GET | `/api/ernie/tpms` | `TpmsLogger.unique_sensors(window_s=7200)` — last 2 h by default |
| GET | `/api/ernie/thermal` | `ThermalLogger.latest()["ernie"]` + health derived from BLE/TPMS/thermal freshness |

The endpoints are `requires_auth=False` on the route table, meaning
they are LAN-reachable without a token.  The Cloudflare tunnel gate
in [`server.py::_dispatch`](../server.py) blocks them over
`lights.schoolio.net` — see [Chapter 15](15-tunnel.md).

### Window override

`/api/ernie/ble` and `/api/ernie/tpms` accept a `?window_s=<seconds>`
query parameter to widen or narrow the default window.  Responses
echo the effective window in the JSON body as `window_s`.  A zero
or negative value drops the filter and returns the full retention
catalog — expensive on long-running databases, so the dashboard
polling path should never send it.

---

## Retention and throttling

| Table | Retention | Throttle |
|-------|-----------|----------|
| `tpms_observations` | 30 days (pruned every 500 writes) | None — every decoded frame is recorded |
| `ble_events` | 30 days (pruned every 500 writes) | None |
| `ble_seen` | Unbounded (naturally bounded by distinct MACs) | 10 s per MAC on non-gone updates; gone-transitions bypass |

TPMS throttling is deliberately absent: a sensor only emits while
the wheel is rotating above ~20 km/h, so an 8-frame burst is the
entire signal of a passing vehicle.  Dropping frames would lose
sightings.  At expected neighborhood traffic volumes the storage
cost is negligible.

BLE `seen` throttling exists because the retained snapshot is
refreshed every few seconds for every active MAC.  Without
throttling, an active indoor device can generate hundreds of
UPSERTs per minute with near-identical payloads.  "Gone"
transitions bypass the throttle because a departure is too rare
and too load-bearing for dashboard semantics to drop.

---

## Configuration

Logger DSN comes from the `GLOWUP_DIAG_DSN` environment variable.
On the hub this is provided by an `EnvironmentFile` directive in
`glowup-server.service`:

```ini
# /etc/systemd/system/glowup-server.service  (excerpt)
[Service]
EnvironmentFile=/etc/glowup/diag.env
```

```dotenv
# /etc/glowup/diag.env  (mode 0600, owner a:a)
GLOWUP_DIAG_DSN=postgresql://glowup:<password>@192.0.2.111:5432/glowup
```

The same file is read by `ThermalLogger`, `PowerLogger`,
`TpmsLogger`, and `BleSnifferLogger` — one env var for every
PostgreSQL-backed logger.  When the variable is unset, each logger
falls back to a `DEFAULT_DSN` placeholder that will not
authenticate, so a missing file is immediately visible in the
server log at startup.

Pi-side dependency:

```bash
/home/a/venv/bin/pip install psycopg2-binary
```

`psycopg2-binary` is a compiled wheel for aarch64 Debian trixie;
the source distribution is not required.

---

## Queries

Common operational queries for ops work on the `glowup` database:

```sql
-- How many TPMS frames in the last hour, per sensor
SELECT model, sensor_id, COUNT(*) AS frames
FROM tpms_observations
WHERE timestamp > extract(epoch FROM now() - interval '1 hour')
GROUP BY model, sensor_id
ORDER BY frames DESC;

-- BLE devices heard today, freshest first
SELECT mac,
       to_timestamp(last_heard_ts) AS last_heard,
       payload->>'name'            AS name,
       gone
FROM ble_seen
WHERE last_heard_ts > extract(epoch FROM now() - interval '1 day')
ORDER BY gone ASC, last_heard_ts DESC;

-- BLE event rate per hour over the past day
SELECT date_trunc('hour', to_timestamp(timestamp)) AS hour,
       COUNT(*)                                    AS events
FROM ble_events
WHERE timestamp > extract(epoch FROM now() - interval '1 day')
GROUP BY hour
ORDER BY hour;
```

---

## Testing

- [`tests/test_tpms_logger.py`](../tests/test_tpms_logger.py) —
  12 tests, DB-gated.
- [`tests/test_ble_sniffer_logger.py`](../tests/test_ble_sniffer_logger.py) —
  8 tests, DB-gated.

Both suites auto-skip when `GLOWUP_DIAG_DSN` is unset or the DSN
is unreachable.  Run against a live DB with:

```bash
GLOWUP_DIAG_DSN='postgresql://glowup:<password>@192.0.2.111:5432/glowup' \
  ~/venv/bin/python -m pytest tests/test_tpms_logger.py \
                              tests/test_ble_sniffer_logger.py -v
```

Test rows are keyed with sentinel prefixes (`TestModel-*`,
`00:00:00:TEST:*`) so `setUp`/`tearDown` can scrub them without
touching production data.

---

## Design notes

- **Loggers own their own paho clients.**  Each of `TpmsLogger` and
  `BleSnifferLogger` spins up a dedicated paho connection in
  `start_subscriber()`.  This keeps failures isolated (a DB
  outage in one logger cannot starve the shared hub subscribe
  loop) and matches the existing `ThermalLogger` pattern.
- **Re-subscribe in `on_connect`.**  Both loggers subscribe on every
  connect, not just at init.  Paho silently deafens after a
  reconnect if this is missed — a bug pattern already documented
  in Claude memory under `feedback_paho_resubscribe_on_connect.md`.
- **`ble_seen` is authoritative over retained snapshots.**  The v2
  sniffer publishes `gone=true` to retire a MAC rather than
  deleting it.  The logger preserves prior payload fields and only
  flips the `gone` flag, so the dashboard can render a recently
  departed device with its full last-known state.
- **No in-memory caches.**  The earlier implementation maintained
  `_ernie_tpms`, `_ernie_ble_seen`, and `_ernie_ble_events` dicts
  on the handler class.  Every server restart wiped them and the
  dashboard had to re-accumulate from live RF hits, which for TPMS
  could take hours or days to rebuild.  The dicts are gone; the
  handlers query the loggers directly.
