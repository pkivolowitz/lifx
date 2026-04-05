"""Classified I/O timing — measure, log, and classify all blocking operations.

Every blocking I/O call (network, subprocess, database) is wrapped in a
``timed_io`` context manager that records elapsed time, detects threshold
violations, and maintains per-label histogram statistics.

The classification system starts with every operation in ``DEFAULT``.
As histogram data accumulates, each label gets reassigned to the class
that matches its observed timing profile.

Usage::

    from infrastructure.timed_io import timed_io, IOClass

    with timed_io("vivint.poll", IOClass.MEDIUM):
        resp = urllib.request.urlopen(url, timeout=5)

    with timed_io("sqlite.connect", IOClass.INSTANT):
        conn = sqlite3.connect(path, timeout=5)

The context manager does NOT kill operations that exceed their class
threshold — Python has no safe way to interrupt a blocked thread.
It logs a WARNING and records the violation so the operator can
identify slow operations and tighten timeouts at the call site.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

import enum
import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator

logger: logging.Logger = logging.getLogger("glowup.timed_io")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of samples retained per label for percentile calculation.
# Reservoir sampling would be more memory-efficient at scale, but
# 200 samples per label is negligible for our workload.
SAMPLE_RESERVOIR_SIZE: int = 200

# Log a histogram summary every N calls per label.
HISTOGRAM_LOG_INTERVAL: int = 100


# ---------------------------------------------------------------------------
# IO Classification
# ---------------------------------------------------------------------------

class IOClass(enum.Enum):
    """Timeout classification for blocking I/O operations.

    Each class defines a threshold in seconds.  Operations that
    exceed their class threshold are logged as warnings.  Initial
    assignments are conservative — refine from histogram data.

    Values are seconds (float).
    """

    INSTANT  = 0.5     # expected < 500ms (local DB, in-memory)
    FAST     = 2.0     # expected < 2s (LAN UDP, local subprocess)
    MEDIUM   = 5.0     # expected < 5s (HTTP to local services, DNS)
    SLOW     = 15.0    # expected < 15s (external APIs, cloud services)
    BLOCKING = 30.0    # expected < 30s (Ollama inference, large transfers)
    DEFAULT  = 5.0     # unclassified — start here, refine from data


# ---------------------------------------------------------------------------
# Per-label statistics
# ---------------------------------------------------------------------------

# Sliding window duration in seconds.
WINDOW_SECONDS: float = 30.0


@dataclass
class IOStats:
    """Timing statistics for a single I/O label.

    Maintains two views:
    - **Lifetime**: all-time count, min, max, avg, stddev via Welford.
    - **Window**: last WINDOW_SECONDS of timestamped samples for
      percentiles, avg, stddev that reflect current behavior.

    Thread-safe — all mutations are guarded by the module-level lock.
    """

    io_class: IOClass = IOClass.DEFAULT

    # --- Lifetime accumulators ---
    count: int = 0
    timeout_count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    _welford_mean: float = 0.0
    _welford_m2: float = 0.0

    # --- Windowed samples: (timestamp_monotonic, elapsed_ms, exceeded) ---
    _window: list[tuple[float, float, bool]] = field(default_factory=list)

    def record(self, elapsed_ms: float, exceeded: bool) -> None:
        """Record a single I/O timing.

        Args:
            elapsed_ms: Elapsed time in milliseconds.
            exceeded:   True if the class threshold was exceeded.
        """
        # Lifetime.
        self.count += 1
        if exceeded:
            self.timeout_count += 1
        self.total_ms += elapsed_ms
        if elapsed_ms < self.min_ms:
            self.min_ms = elapsed_ms
        if elapsed_ms > self.max_ms:
            self.max_ms = elapsed_ms

        # Welford's online variance.
        delta: float = elapsed_ms - self._welford_mean
        self._welford_mean += delta / self.count
        delta2: float = elapsed_ms - self._welford_mean
        self._welford_m2 += delta * delta2

        # Windowed sample with timestamp.
        now: float = time.monotonic()
        self._window.append((now, elapsed_ms, exceeded))

        # Prune expired samples.
        cutoff: float = now - WINDOW_SECONDS
        while self._window and self._window[0][0] < cutoff:
            self._window.pop(0)

    def _window_values(self) -> list[float]:
        """Return elapsed_ms values within the window."""
        now: float = time.monotonic()
        cutoff: float = now - WINDOW_SECONDS
        return [ms for ts, ms, _ in self._window if ts >= cutoff]

    def _window_exceeded(self) -> int:
        """Count exceeded calls within the window."""
        now: float = time.monotonic()
        cutoff: float = now - WINDOW_SECONDS
        return sum(1 for ts, _, exc in self._window if ts >= cutoff and exc)

    # --- Lifetime accessors ---

    def avg_ms(self) -> float:
        """Lifetime average elapsed time in milliseconds."""
        if self.count == 0:
            return 0.0
        return self.total_ms / self.count

    def stddev_ms(self) -> float:
        """Lifetime population standard deviation in milliseconds."""
        if self.count < 2:
            return 0.0
        return math.sqrt(self._welford_m2 / self.count)

    # --- Window accessors ---

    def window_count(self) -> int:
        """Number of calls in the current window."""
        return len(self._window_values())

    def window_avg_ms(self) -> float:
        """Average within the sliding window."""
        vals: list[float] = self._window_values()
        if not vals:
            return 0.0
        return sum(vals) / len(vals)

    def window_stddev_ms(self) -> float:
        """Standard deviation within the sliding window."""
        vals: list[float] = self._window_values()
        if len(vals) < 2:
            return 0.0
        mean: float = sum(vals) / len(vals)
        variance: float = sum((v - mean) ** 2 for v in vals) / len(vals)
        return math.sqrt(variance)

    def window_percentile(self, p: float) -> float:
        """Percentile within the sliding window.

        Args:
            p: Percentile as a fraction (0.0–1.0).

        Returns:
            Percentile value in milliseconds, or 0.0 if no data.
        """
        vals: list[float] = self._window_values()
        if not vals:
            return 0.0
        vals.sort()
        idx: int = min(int(p * len(vals)), len(vals) - 1)
        return vals[idx]

    def window_min_ms(self) -> float:
        """Minimum within the sliding window."""
        vals: list[float] = self._window_values()
        return min(vals) if vals else 0.0

    def window_max_ms(self) -> float:
        """Maximum within the sliding window."""
        vals: list[float] = self._window_values()
        return max(vals) if vals else 0.0

    def window_exceeded(self) -> int:
        """Exceeded count within the sliding window."""
        return self._window_exceeded()

    # --- Legacy percentile (for histogram log) ---

    def percentile(self, p: float) -> float:
        """Percentile from recent window samples.

        Args:
            p: Percentile as a fraction (0.0–1.0).

        Returns:
            Percentile value in milliseconds, or 0.0 if no data.
        """
        return self.window_percentile(p)


# ---------------------------------------------------------------------------
# Global stats registry
# ---------------------------------------------------------------------------

_lock: threading.Lock = threading.Lock()
_stats: dict[str, IOStats] = {}


def get_stats(label: str) -> IOStats:
    """Get or create stats for a label.  Thread-safe.

    Args:
        label: I/O operation label (e.g., "vivint.poll").

    Returns:
        The IOStats instance for this label.
    """
    with _lock:
        if label not in _stats:
            _stats[label] = IOStats()
        return _stats[label]


def get_all_stats() -> dict[str, IOStats]:
    """Return a snapshot of all stats.  Thread-safe.

    Returns:
        Dict of label → IOStats (shallow copy).
    """
    with _lock:
        return dict(_stats)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

@contextmanager
def timed_io(
    label: str,
    io_class: IOClass = IOClass.DEFAULT,
) -> Generator[None, None, None]:
    """Time a blocking I/O operation and record statistics.

    Logs a WARNING if the operation exceeds its class threshold.
    Logs a histogram summary every HISTOGRAM_LOG_INTERVAL calls.

    Args:
        label:    Descriptive label for this I/O operation.
        io_class: Expected timing class.

    Yields:
        Nothing — the wrapped code runs inside the ``with`` block.
    """
    t0: float = time.monotonic()
    try:
        yield
    finally:
        elapsed_s: float = time.monotonic() - t0
        elapsed_ms: float = elapsed_s * 1000.0
        threshold_s: float = io_class.value
        exceeded: bool = elapsed_s > threshold_s

        with _lock:
            if label not in _stats:
                _stats[label] = IOStats(io_class=io_class)
            stats: IOStats = _stats[label]
            stats.record(elapsed_ms, exceeded)
            count: int = stats.count
            should_log_histogram: bool = (
                count > 0 and count % HISTOGRAM_LOG_INTERVAL == 0
            )

        # Log threshold violation.
        if exceeded:
            logger.warning(
                "SLOW IO [%s]: %.1fms (class %s, limit %.0fms)",
                label, elapsed_ms,
                io_class.name, threshold_s * 1000.0,
            )

        # Log histogram summary periodically.
        if should_log_histogram:
            with _lock:
                p50: float = stats.percentile(0.50)
                p95: float = stats.percentile(0.95)
                p99: float = stats.percentile(0.99)
                avg: float = stats.avg_ms()
                total: int = stats.count
                timeouts: int = stats.timeout_count
                max_ms: float = stats.max_ms
                min_ms: float = stats.min_ms

            logger.info(
                "IO STATS [%s]: %d calls, "
                "p50=%.1fms p95=%.1fms p99=%.1fms "
                "min=%.1fms max=%.1fms avg=%.1fms, "
                "%d exceeded (%s class, limit %.0fms)",
                label, total,
                p50, p95, p99,
                min_ms, max_ms, avg,
                timeouts, io_class.name, threshold_s * 1000.0,
            )
