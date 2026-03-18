"""Centralized network configuration for GlowUp.

Loads infrastructure addresses (MQTT broker, REST server, database,
camera) from a single JSON file, eliminating hardcoded IPs scattered
across the codebase.

**Resolution order:**

1. ``GLOWUP_NETWORK`` environment variable → path to a JSON file
   outside the repo (e.g. ``/etc/glowup/network.json``).
2. ``network.json`` in the project root (committed with safe
   placeholder values).

The external file takes precedence so that real IPs never need to
live inside the repository.

Usage::

    from network_config import net

    broker = net.broker        # MQTT broker hostname/IP
    server = net.server        # GlowUp REST server hostname/IP
    db_host = net.db_host      # PostgreSQL hostname/IP
    camera = net.camera_host   # RTSP camera hostname/IP
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import json
import logging
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Environment variable pointing to the real network config file.
ENV_VAR: str = "GLOWUP_NETWORK"

#: Fallback config bundled with the repo (placeholder values).
_REPO_CONFIG: Path = Path(__file__).resolve().parent / "network.json"

# Module logger.
logger: logging.Logger = logging.getLogger("glowup.network_config")


# ---------------------------------------------------------------------------
# NetworkConfig
# ---------------------------------------------------------------------------

class NetworkConfig:
    """Immutable container for network infrastructure addresses.

    Attributes:
        broker:      MQTT broker hostname or IP.
        server:      GlowUp REST server hostname or IP.
        db_host:     PostgreSQL hostname or IP.
        camera_host: RTSP camera hostname or IP.
        source:      Path of the JSON file that was loaded.
    """

    # Defaults matching the committed network.json placeholders.
    _DEFAULTS: dict[str, str] = {
        "broker": "localhost",
        "server": "localhost",
        "db_host": "localhost",
        "camera_host": "camera",
    }

    def __init__(self, data: dict[str, Any], source: str) -> None:
        self.broker: str = data.get("broker", self._DEFAULTS["broker"])
        self.server: str = data.get("server", self._DEFAULTS["server"])
        self.db_host: str = data.get("db_host", self._DEFAULTS["db_host"])
        self.camera_host: str = data.get("camera_host", self._DEFAULTS["camera_host"])
        self.source: str = source

    def __repr__(self) -> str:
        return (
            f"NetworkConfig(broker={self.broker!r}, server={self.server!r}, "
            f"db_host={self.db_host!r}, camera_host={self.camera_host!r}, "
            f"source={self.source!r})"
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load() -> NetworkConfig:
    """Load network configuration using the resolution order.

    Returns:
        A populated :class:`NetworkConfig` instance.
    """
    # 1. Check GLOWUP_NETWORK env var.
    env_path: str | None = os.environ.get(ENV_VAR)
    if env_path is not None:
        path = Path(env_path)
        if path.is_file():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            logger.info("Network config loaded from %s (env)", path)
            return NetworkConfig(data, str(path))
        logger.warning(
            "%s=%s but file not found; falling back to repo config",
            ENV_VAR, env_path,
        )

    # 2. Fall back to repo-bundled network.json.
    if _REPO_CONFIG.is_file():
        with open(_REPO_CONFIG, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        logger.info("Network config loaded from %s (repo)", _REPO_CONFIG)
        return NetworkConfig(data, str(_REPO_CONFIG))

    # 3. Absolute fallback — hardcoded defaults.
    logger.warning("No network config found; using built-in defaults")
    return NetworkConfig({}, "built-in")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: The global network configuration.  Import and use directly::
#:
#:     from network_config import net
#:     print(net.broker)
net: NetworkConfig = _load()
