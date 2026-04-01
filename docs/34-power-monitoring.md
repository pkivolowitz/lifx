# Power Monitoring

> Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
> Licensed under the [MIT License](../LICENSE).

GlowUp monitors real-time power consumption from Zigbee smart plugs
and presents it on a dedicated `/power` dashboard.  The system
records readings to a local SQLite database with 7-day retention
and provides charting, cost tracking, anomaly detection, and
device on/off control.

---

## Architecture

```
+-------------------+       +-------------------+       +-------------------+
|  ThirdReality     |       |  Zigbee2MQTT      |       |  GlowUp Server   |
|  Smart Plug Gen3  | Zigbee|  (broker-2)       |  MQTT |  ZigbeeAdapter   |
|  (power, voltage, | ----->|                   | ----->|       |          |
|   current, energy)|       +-------------------+       |  PowerLogger     |
+-------------------+                                   |       |          |
                                                        |   SQLite DB     |
                                                        |  /etc/glowup/   |
                                                        |   power.db      |
                                                        +-------------------+
                                                                |
                                                          /power page
                                                         (Chart.js)
```

- **Smart plugs** report power, voltage, current, energy, power
  factor, and AC frequency every ~30 seconds via Zigbee.
- **ZigbeeAdapter** normalizes the values and calls
  `PowerLogger.record()` for each property.
- **PowerLogger** accumulates properties per device, throttles
  writes (5-second minimum interval), and stores readings in SQLite.
- **/power page** fetches chart data and statistics via REST API.

---

## PowerLogger (`power_logger.py`)

### Construction

```python
from power_logger import PowerLogger

logger = PowerLogger(db_path="/etc/glowup/power.db")
```

Creates the SQLite database and table on first use.  WAL mode is
enabled for concurrent read/write access from the server thread
and the Zigbee adapter's MQTT callback thread.

### Recording

```python
logger.record(device="ML_Power", prop="power", value=167.3)
logger.record(device="ML_Power", prop="voltage", value=122.2)
logger.record(device="ML_Power", prop="current", value=1.42)
```

- Accepts properties: `power`, `voltage`, `current`, `energy`,
  `power_factor`, `ac_frequency`.
- Non-power properties (occupancy, temperature, etc.) are silently
  ignored.
- Accumulates properties for the same device and writes a single
  row when the throttle interval (5 seconds) has passed.
- Per-device throttle -- devices are independent.

### Querying

```python
# Charting: 5-minute buckets over the last hour.
rows = logger.query(device="ML_Power", hours=1, resolution=300)
# rows: [{"bucket": ts, "device": "ML_Power", "power": 167.3, ...}, ...]

# Summary statistics for the last 7 days.
stats = logger.summary(device="ML_Power", days=7)
# stats: {"avg_watts": 167.0, "peak_watts": 263.0, "total_kwh": 28.1, ...}

# List of known devices.
devices = logger.devices()
# devices: ["LRTV", "ML_Power"]
```

### Automatic Pruning

Records older than 7 days are pruned automatically every 100
writes.  No manual maintenance is required.

---

## Database Schema

```sql
CREATE TABLE power_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL,
    timestamp REAL NOT NULL,    -- Unix epoch (seconds)
    power REAL,                 -- Watts
    voltage REAL,               -- Volts
    current_a REAL,             -- Amps
    energy REAL,                -- kWh (cumulative)
    power_factor REAL           -- 0.0-1.0
);
CREATE INDEX idx_power_device_ts ON power_readings(device, timestamp);
```

WAL journal mode.  Synchronous set to NORMAL for durability
without sacrificing write performance.

---

## REST API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/power` | Serve the power dashboard HTML |
| GET | `/api/power/readings` | Chart data with time range and resolution |
| GET | `/api/power/summary` | Statistics (avg, peak, kWh, cost) |
| GET | `/api/power/devices` | List of known device names |

### `/api/power/readings`

Query parameters:

- `hours` -- How many hours of history (default: `1`).
- `resolution` -- Bucket size in seconds (default: `300`).
- `device` -- Device name filter (optional).

Response:

```json
{
    "readings": [
        {
            "bucket": 1711929600,
            "device": "ML_Power",
            "power": 167.3,
            "voltage": 122.2,
            "current_a": 1.42,
            "energy": 2.0,
            "power_factor": 0.97
        }
    ]
}
```

### `/api/power/summary`

Query parameters:

- `days` -- Number of days to summarize (default: `7`).
- `device` -- Device name filter (optional).

Response:

```json
{
    "avg_watts": 167.0,
    "peak_watts": 263.0,
    "total_kwh": 28.1,
    "days_covered": 6.8,
    "device_count": 2
}
```

---

## Dashboard Features (`/power`)

### Stats Cards

Eight stat cards across the top:

- **Current Draw** -- Latest reading in watts
- **Voltage** -- Latest reading in volts
- **Current** -- Latest reading in amps
- **7-Day Avg** -- Average watts over the retention window
- **7-Day Peak** -- Maximum watts observed
- **7-Day Energy** -- Total kWh consumed
- **7-Day Cost** -- Actual cost at $0.171/kWh
- **Projected 7-Day** -- Projected cost from average watts

### Power Draw Chart

Line chart with four time ranges:

- **1h** -- 10-second resolution
- **6h** -- 1-minute resolution
- **24h** -- 5-minute resolution
- **7d** -- 30-minute resolution

Device selector dropdown filters by individual plug or all devices.

### Device Controls

On/off toggle switch per device.  Publishes state changes to
Zigbee2MQTT via the GlowUp API (`/api/zigbee/set`).  Device state
is inferred from power draw (>1W = ON).

### Daily Cost Chart

Bar chart showing estimated daily cost for the selected device or
all devices over the last 7 days.  Calculated from average power
per day-bucket multiplied by the electricity rate.

### Anomaly Detection

Runs every 30 seconds.  Flags two conditions:

- **Spike** -- Current power exceeds 2x the rolling average.
  Severity: warning (2-3x), alert (>3x).
- **Drop** -- Device that was drawing >50W suddenly reads <1W.
  May indicate the device powered off unexpectedly.

Anomaly cards appear/disappear automatically based on current
readings.

---

## Configuration

No dedicated configuration section in `server.json`.  The power
logger is automatically created when the Zigbee adapter is enabled.

Perry's electricity rate: **$0.171/kWh** (hardcoded in `power.html`
as `RATE_PER_KWH`).

The database path defaults to `/etc/glowup/power.db`.  On the Pi,
this directory is created by the deploy script.

---

## Devices

Current monitored plugs:

| Name | Location | Typical Draw |
|------|----------|-------------|
| ML_Power | ML server (AMD Epyc, 3x RTX) | 165-175W idle, 263W boot |
| LRTV | Living room TV | 100W on (quick-start), 0W standby |
| BYIR | Upstairs (distance test) | 0W (nothing plugged in) |

---

## Tests

39 exhaustive tests in `tests/test_power_logger.py`:

- Construction: DB creation, table/index, WAL mode, idempotent open,
  bad path handling
- Recording: writes, throttling, multi-device independence,
  property filtering, accumulation
- Pruning: old record removal, recent record survival, auto-trigger
- Queries: time windows, resolution, device filter, column names,
  bucket averaging, empty/null cases
- Summary: avg/peak/count, multi-device, empty DB
- Devices: distinct, sorted, empty
- Thread safety: concurrent records (5 threads x 20 writes),
  concurrent query + record
- Close: sets conn to None, double-close safe

---

## Dependencies

- `sqlite3` (stdlib)
- `threading` (stdlib)
- Chart.js 4.4.4 (CDN, loaded by `power.html`)

---

## Files

| File | Description |
|------|-------------|
| `power_logger.py` | PowerLogger class — SQLite recording + queries |
| `static/power.html` | Dashboard page — charts, stats, controls, anomaly detection |
| `tests/test_power_logger.py` | 39 exhaustive tests |

---

## See Also

- [Chapter 29: Zigbee Adapter](29-zigbee-adapter.md) -- The adapter
  that feeds power readings to the logger.
- [Chapter 27: Adapter Base Classes](27-adapter-base.md) -- Base
  classes used by the Zigbee adapter.
