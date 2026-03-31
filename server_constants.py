"""Server constants — shared across all server modules.

Extracted from server.py to reduce monolith size.  All values are
plain constants with no dependencies on other GlowUp modules.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__: str = "1.0"

# ---------------------------------------------------------------------------
# HTTP / Server
# ---------------------------------------------------------------------------

# Default HTTP port for the REST API.
DEFAULT_PORT: int = 8420

# Server-Sent Events polling rate (Hz) for live color streaming.
SSE_POLL_HZ: float = 4.0

# Computed interval between SSE polls (seconds).
SSE_POLL_INTERVAL: float = 1.0 / SSE_POLL_HZ

# Maximum allowed size of an HTTP request body (bytes).
MAX_REQUEST_BODY: int = 65536

# Default fade-to-black duration when stopping an effect (ms).
DEFAULT_FADE_MS: int = 500

# Brightness is normalized from 0-100 percentage to 0-HSBK_MAX.
BRIGHTNESS_PERCENTAGE_SCALE: int = 100

# Brief delay for multizone devices to initialize after power-on (seconds).
DEVICE_WAKEUP_DELAY_SECONDS: float = 0.1

# HTTP Strict-Transport-Security max-age (seconds in one year).
HSTS_MAX_AGE_SECONDS: int = 365 * 24 * 60 * 60  # 31536000

# SSE stream timeout — close idle connections after this many seconds.
SSE_TIMEOUT_SECONDS: float = 3600.0

# API path prefix.
API_PREFIX: str = "/api"

# Error message for device identifier resolution failures.
DEVICE_RESOLVE_ERROR: str = "Cannot resolve device identifier"

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

# HTTP Authorization header name.
AUTH_HEADER: str = "Authorization"

# Bearer token prefix in the Authorization header.
BEARER_PREFIX: str = "Bearer "

# Maximum failed authentication attempts per IP before throttling.
AUTH_RATE_LIMIT: int = 10

# Time window for the auth rate limiter (seconds).
AUTH_RATE_WINDOW: int = 60

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

# How often the scheduler thread checks for schedule transitions (seconds).
SCHEDULER_POLL_SECONDS: int = 30

# ---------------------------------------------------------------------------
# Audio / Media
# ---------------------------------------------------------------------------

# Timeout for reading audio chunks from the processing queue (seconds).
AUDIO_QUEUE_TIMEOUT_SECONDS: float = 0.05

# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

# Timeout for the calibration protocol TCP handshake (seconds).
CALIBRATION_SOCKET_TIMEOUT_SECONDS: float = 10.0

# Delay between calibration pulses (seconds).
CALIBRATION_PULSE_DELAY_SECONDS: float = 0.1

# ---------------------------------------------------------------------------
# Device identification (pulse-to-locate)
# ---------------------------------------------------------------------------

# Identify pulse duration (seconds).
IDENTIFY_DURATION_SECONDS: float = 10.0

# Seconds per full brightness cycle during identify.
IDENTIFY_CYCLE_SECONDS: float = 3.0

# Seconds between brightness updates during identify (20 fps).
IDENTIFY_FRAME_INTERVAL: float = 0.05

# Minimum brightness fraction during identify pulse (5%).
IDENTIFY_MIN_BRI: float = 0.05

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

# Per-device UDP query timeout for /api/command/discover (seconds).
COMMAND_DISCOVER_TIMEOUT_SECONDS: float = 4.0

# Maximum identify duration accepted from /api/command/identify (seconds).
COMMAND_IDENTIFY_MAX_DURATION: float = 60.0

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default configuration file path (for Pi deployment).
DEFAULT_CONFIG_PATH: str = "/etc/glowup/server.json"

# Filename for user-saved effect parameter defaults (co-located with config).
EFFECT_DEFAULTS_FILENAME: str = "effect_defaults.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Logging format matching scheduler.py.
LOG_FORMAT: str = "%(asctime)s %(levelname)s %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Device identifiers
# ---------------------------------------------------------------------------

# Prefix used to distinguish group identifiers from IP addresses in
# the API path and internal device dictionaries.
GROUP_PREFIX: str = "group:"

# Prefix for grid identifiers (2D spatial device arrangements).
GRID_PREFIX: str = "grid:"
