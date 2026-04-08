"""One-shot backfill of NULL columns in power_readings (LOCF carry-forward).

Background
==========

The PowerLogger originally destructively popped its per-device
``_pending`` dict on every write.  ThirdReality smart plugs do
change-based reporting — a single Z2M message can contain only the
properties that changed since the last sample (often just
``current``) — so any sparse message produced a row with NULL
columns for the four other properties.  The dashboard chart's
``power || 0`` JS coercion silently rendered those NULLs as 0 W
drops, making the LRTV plug appear to keep "turning off" while the
TV was on.

The runtime fix (commit pending) makes ``_pending`` a true
carry-forward state that is never popped.  This script repairs the
historical rows that were written before that fix landed, so the
existing 7-day chart isn't peppered with phantom drops.

Strategy
========

LOCF (Last Observation Carried Forward), per-device, in increasing
``timestamp`` order.  Implemented in pure SQL via per-column
correlated subqueries wrapped in COALESCE:

    UPDATE power_readings AS r
    SET power = COALESCE(r.power, (
            SELECT p.power FROM power_readings p
            WHERE p.device = r.device
              AND p.timestamp < r.timestamp
              AND p.power IS NOT NULL
            ORDER BY p.timestamp DESC, p.id DESC
            LIMIT 1
        )),
        voltage = COALESCE(r.voltage, (... same shape ...)),
        ...
    WHERE <r has at least one NULL column>;

For each row that has at least one NULL column, each NULL slot is
filled by the most recent strictly-preceding row for the same
device that had that column non-NULL.  COALESCE makes the operation
a no-op for slots that are already non-NULL.  The id-DESC tiebreaker
guarantees deterministic resolution if two rows ever share a
timestamp (PowerLogger uses ``time.time()`` so ties are exceedingly
rare in practice, but rigor is cheap).

Why correlated subqueries instead of a window function:  SQLite
does NOT support the ``IGNORE NULLS`` modifier on window functions
as of 3.46 — it is a PostgreSQL / Oracle / SQL Server feature, not
in the SQLite grammar.  The correlated subquery approach is the
portable equivalent and matches every SQL engine ever shipped.

Edge case: rows at the very start of a device's history, BEFORE any
non-NULL value of a particular column has been seen, have nothing
to carry forward and remain NULL.  This is correct behavior — we
never had that information, so we cannot fabricate it.

Query plan
==========

The correlated subqueries hit the existing covering index
``idx_power_device_ts (device, timestamp)``: equality on device,
range on timestamp, ORDER BY DESC LIMIT 1 = single index seek.
Each subquery is O(log N).  For ~777 dirty rows × up to 5 NULL
slots × 5 columns of subqueries on the SET clause = ~20K index
seeks.  Sub-second on the Pi for ~8K rows.

The WHERE clause restricts the UPDATE to rows that actually have at
least one NULL — no unnecessary writes to non-NULL rows.

Concurrency
===========

The PowerLogger writes via WAL mode.  This script opens its own
connection in WAL mode and runs the UPDATE inside an implicit
transaction.  The WAL guarantees readers see a consistent snapshot;
concurrent writers append to the WAL and will be flushed on the
next checkpoint.  No locks are taken on the live PowerLogger.

Run as ``python3 backfill_power_nulls.py`` for a dry-run summary.
Add ``--apply`` to actually execute the UPDATE.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import argparse
import sqlite3
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH: str = "/etc/glowup/power.db"

# All five reading columns we may need to backfill.  Order matches
# the schema in power_logger.py.
READING_COLUMNS: tuple[str, ...] = (
    "power", "voltage", "current_a", "energy", "power_factor",
)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Diagnostic — count NULL occurrences per column.
SQL_COUNT_NULLS: str = """
SELECT
    SUM(CASE WHEN power        IS NULL THEN 1 ELSE 0 END) AS power_nulls,
    SUM(CASE WHEN voltage      IS NULL THEN 1 ELSE 0 END) AS voltage_nulls,
    SUM(CASE WHEN current_a    IS NULL THEN 1 ELSE 0 END) AS current_a_nulls,
    SUM(CASE WHEN energy       IS NULL THEN 1 ELSE 0 END) AS energy_nulls,
    SUM(CASE WHEN power_factor IS NULL THEN 1 ELSE 0 END) AS power_factor_nulls,
    COUNT(*) AS total_rows
FROM power_readings
"""

# Diagnostic — count rows that have at least one NULL column.
SQL_COUNT_DIRTY_ROWS: str = """
SELECT COUNT(*) FROM power_readings
WHERE power        IS NULL
   OR voltage      IS NULL
   OR current_a    IS NULL
   OR energy       IS NULL
   OR power_factor IS NULL
"""

# Diagnostic — for each device, count how many NULL slots would
# remain NULL after LOCF (start-of-history rows with no preceding
# non-NULL value to carry forward).  Uses NOT EXISTS for each
# column.
SQL_COUNT_UNFILLABLE: str = """
SELECT
    device,
    SUM(CASE WHEN power IS NULL AND NOT EXISTS (
        SELECT 1 FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.power IS NOT NULL
    ) THEN 1 ELSE 0 END) AS power_left,
    SUM(CASE WHEN voltage IS NULL AND NOT EXISTS (
        SELECT 1 FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.voltage IS NOT NULL
    ) THEN 1 ELSE 0 END) AS voltage_left,
    SUM(CASE WHEN current_a IS NULL AND NOT EXISTS (
        SELECT 1 FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.current_a IS NOT NULL
    ) THEN 1 ELSE 0 END) AS current_a_left,
    SUM(CASE WHEN energy IS NULL AND NOT EXISTS (
        SELECT 1 FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.energy IS NOT NULL
    ) THEN 1 ELSE 0 END) AS energy_left,
    SUM(CASE WHEN power_factor IS NULL AND NOT EXISTS (
        SELECT 1 FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.power_factor IS NOT NULL
    ) THEN 1 ELSE 0 END) AS power_factor_left
FROM power_readings AS r
GROUP BY device
ORDER BY device
"""

# The actual backfill.  Each NULL slot is filled by the nearest
# strictly-preceding non-NULL value of that column for the same
# device.  COALESCE makes the per-column expression a no-op when
# the row's slot is already non-NULL.  The id DESC tiebreaker is
# defensive — PowerLogger uses time.time() so collisions are
# vanishingly rare, but determinism is cheap.
SQL_BACKFILL: str = """
UPDATE power_readings AS r
SET
    power = COALESCE(r.power, (
        SELECT p.power FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.power IS NOT NULL
         ORDER BY p.timestamp DESC, p.id DESC
         LIMIT 1
    )),
    voltage = COALESCE(r.voltage, (
        SELECT p.voltage FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.voltage IS NOT NULL
         ORDER BY p.timestamp DESC, p.id DESC
         LIMIT 1
    )),
    current_a = COALESCE(r.current_a, (
        SELECT p.current_a FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.current_a IS NOT NULL
         ORDER BY p.timestamp DESC, p.id DESC
         LIMIT 1
    )),
    energy = COALESCE(r.energy, (
        SELECT p.energy FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.energy IS NOT NULL
         ORDER BY p.timestamp DESC, p.id DESC
         LIMIT 1
    )),
    power_factor = COALESCE(r.power_factor, (
        SELECT p.power_factor FROM power_readings p
         WHERE p.device = r.device
           AND p.timestamp < r.timestamp
           AND p.power_factor IS NOT NULL
         ORDER BY p.timestamp DESC, p.id DESC
         LIMIT 1
    ))
WHERE r.power        IS NULL
   OR r.voltage      IS NULL
   OR r.current_a    IS NULL
   OR r.energy       IS NULL
   OR r.power_factor IS NULL
"""


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_null_counts(conn: sqlite3.Connection, label: str) -> None:
    """Print per-column NULL counts and total row count."""
    row: Any = conn.execute(SQL_COUNT_NULLS).fetchone()
    print(f"\n=== {label} ===")
    print(f"  total rows:        {row[5]}")
    print(f"  power NULL:        {row[0]}")
    print(f"  voltage NULL:      {row[1]}")
    print(f"  current_a NULL:    {row[2]}")
    print(f"  energy NULL:       {row[3]}")
    print(f"  power_factor NULL: {row[4]}")
    dirty: int = conn.execute(SQL_COUNT_DIRTY_ROWS).fetchone()[0]
    print(f"  rows with >=1 NULL column: {dirty}")


def print_unfillable(conn: sqlite3.Connection) -> None:
    """Print, per device, how many NULL slots will REMAIN NULL after LOCF.

    These are the start-of-history rows where we never had a value
    to carry forward.  They cannot be repaired by this script (or
    any other LOCF strategy) — the information was never observed.
    """
    rows: list[Any] = conn.execute(SQL_COUNT_UNFILLABLE).fetchall()
    print("\n=== unfillable NULLs after LOCF (start-of-history rows) ===")
    print(f"  {'device':<20} {'power':>8} {'volt':>8} {'curr':>8} "
          f"{'energy':>8} {'pf':>8}")
    for r in rows:
        print(f"  {r[0]:<20} {r[1]:>8} {r[2]:>8} {r[3]:>8} {r[4]:>8} {r[5]:>8}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    """Entry point.  Returns process exit code."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="LOCF backfill for NULL columns in power.db",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to power.db (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually run the UPDATE.  Without this flag, the script "
             "is dry-run only.",
    )
    args: argparse.Namespace = parser.parse_args()

    print(f"Opening {args.db} (mode={'APPLY' if args.apply else 'DRY-RUN'})")
    print(f"SQLite version: {sqlite3.sqlite_version}")
    try:
        conn: sqlite3.Connection = sqlite3.connect(args.db, timeout=10)
    except sqlite3.Error as exc:
        print(f"ERROR: could not open database: {exc}", file=sys.stderr)
        return 1

    print_null_counts(conn, "BEFORE")
    print_unfillable(conn)

    if not args.apply:
        print("\nDry-run only.  Re-run with --apply to execute the UPDATE.")
        conn.close()
        return 0

    print("\nExecuting UPDATE ...")
    cursor: sqlite3.Cursor = conn.execute(SQL_BACKFILL)
    rows_updated: int = cursor.rowcount
    conn.commit()
    print(f"  rows updated: {rows_updated}")

    print_null_counts(conn, "AFTER")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
