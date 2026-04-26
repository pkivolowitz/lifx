"""GlowUp site configuration — the single editable place for every
value that depends on the cloner's network.

Every program that needs a site-specific value (hub broker, postgres
DSN, peripheral host, etc.) imports from this module rather than
hardcoding or sprinkling env-var reads. There is exactly one place a
public fork or a fleet operator edits to bring up a new install:

    /etc/glowup/site.json

(or, on a dev Mac without the /etc/ tree, ``~/.glowup/site.json``,
or whatever ``$GLOWUP_SITE_JSON`` points at — see ``_candidate_paths``
below). The file is a deploy-time drop, NEVER committed to either
the public lifx repo or the private glowup-infra repo. The repo-side
artifact is :file:`site.json.example` — a documented template with
``<placeholder>`` strings and no real values.

The loader is permissive at import (a missing or empty config is
fine — many programs run with only a subset of values) but every
``site.require("...")`` call fails fast with a useful message if the
value is missing or still a literal placeholder. That gives us the
"placeholder strings can't reach production" guarantee — a half-
configured deploy crashes the program at startup with a message that
names the offending key, rather than silently connecting nowhere or
late-failing on a bogus hostname.

Typical uses
------------

A program that requires a hub broker::

    from glowup_site import site
    HUB_BROKER: str = site.require("hub_broker")
    HUB_PORT: int = site.get("hub_port", 1883)

A program with an optional feature::

    from glowup_site import site
    DB_DSN: str | None = site.get("postgres_dsn")
    if DB_DSN:
        history = HistoryDB(DB_DSN)
    else:
        history = NoHistoryDB()

Tests
-----

Tests that don't care about site values can run without any config
file present — :class:`_Site` defaults to an empty dict and every
``get()`` returns ``None``. Tests that DO need values can either
write a temporary site.json and point ``$GLOWUP_SITE_JSON`` at it,
or monkeypatch the module-level ``site`` object directly.

Public-fork user experience
---------------------------

::

    cp site.json.example /etc/glowup/site.json
    # edit /etc/glowup/site.json — fill in your hub broker, etc.
    sudo systemctl start glowup-server

A future installer (``python3 -m glowup.install`` or similar) will
automate the cp + edit by prompting for or auto-discovering values.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__version__: str = "1.0.0"


# ─── Constants ────────────────────────────────────────────────────────

# Schema version of the site.json this loader understands. Bump in
# lockstep when site.json grows or shrinks required keys; refuse to
# load mismatched versions rather than silently misinterpret a future
# document. Optional keys may be added without bumping.
SUPPORTED_SCHEMA_VERSION: int = 1

# Default search order for the site config file. The env-var override
# is for tests, dev, and one-off installs; the /etc/ path is the
# production drop; ``~/.glowup/`` is the per-user fallback so a
# developer can iterate on a Mac without sudo.
_ENV_PATH_VAR: str = "GLOWUP_SITE_JSON"
_SYSTEM_PATH: Path = Path("/etc/glowup/site.json")
_USER_PATH: Path = Path.home() / ".glowup" / "site.json"

# Regex matching ``<...>`` placeholder strings. Site.json templates
# (site.json.example) use this style for documentation; a value
# matching this pattern in the LOADED config is treated as "still a
# placeholder", not a real value, and triggers fail-fast in
# ``require()`` and ``get()``.
_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"^<.*>$")


# ─── Errors ───────────────────────────────────────────────────────────

class SiteConfigError(Exception):
    """Raised when site configuration is missing, malformed, or still
    contains literal placeholder text where a real value is needed."""


# ─── Loader ───────────────────────────────────────────────────────────

class _Site:
    """Lazily-validated wrapper around a site.json dict.

    The constructor never raises for missing keys; validation happens
    at the call site via :meth:`require` (mandatory) or :meth:`get`
    (optional with a default). This lets a single program read a
    mix of required and optional values from the same file.
    """

    def __init__(self, data: dict[str, Any], source: str) -> None:
        """Build a site wrapper.

        Args:
            data: parsed JSON payload (may be empty).
            source: human-readable source identifier (path or
                ``"(empty)"``) used in error messages so a confused
                operator can find which file is being consulted.
        """
        self._data: dict[str, Any] = data
        self._source: str = source

    @property
    def source(self) -> str:
        """Path or label of the file this site config was loaded from."""
        return self._source

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for ``key`` if set and not a placeholder,
        otherwise return ``default``.

        A literal placeholder (``"<hub host or IP>"``) raises
        :class:`SiteConfigError`; that case is a configuration bug,
        not a missing-value condition, and silently returning the
        default would defeat the whole point of this module.
        """
        v: Any = self._data.get(key)
        if v is None or v == "":
            return default
        if isinstance(v, str) and _PLACEHOLDER_RE.match(v):
            raise SiteConfigError(
                f"site config key {key!r} is still a placeholder "
                f"({v!r}); edit {self._source} and replace it with a "
                "real value, or unset it to disable the dependent feature"
            )
        return v

    def require(self, key: str) -> Any:
        """Return the value for ``key`` or raise :class:`SiteConfigError`.

        Use this for any value the program cannot run without — the
        error message names the missing key and the source file so the
        operator can fix it without grepping the code.
        """
        v: Any = self.get(key)
        if v is None:
            raise SiteConfigError(
                f"required site config key {key!r} is not set in "
                f"{self._source}; add it (or copy from "
                "site.json.example in the lifx repo) and restart the "
                "service"
            )
        return v


def _candidate_paths() -> list[Path]:
    """Return the ordered list of paths to try for site.json.

    Order:
      1. ``$GLOWUP_SITE_JSON`` if set — explicit override for tests
         and dev.
      2. ``/etc/glowup/site.json`` — production deploy target on
         Linux fleet hosts.
      3. ``~/.glowup/site.json`` — per-user fallback (Mac dev,
         non-root installs).

    The first existing file wins. If none exist, an empty config is
    used and every :meth:`_Site.get` returns ``None``; programs that
    need values fail fast via :meth:`_Site.require`.
    """
    paths: list[Path] = []
    env_path: str | None = os.environ.get(_ENV_PATH_VAR)
    if env_path:
        paths.append(Path(env_path))
    paths.append(_SYSTEM_PATH)
    paths.append(_USER_PATH)
    return paths


def _load() -> _Site:
    """Locate, parse, and validate site.json. Returns an empty site
    if no candidate path exists — that is intentional, see the module
    docstring."""
    for path in _candidate_paths():
        if path.is_file():
            try:
                text: str = path.read_text(encoding="utf-8")
                data: Any = json.loads(text)
            except (OSError, json.JSONDecodeError) as exc:
                raise SiteConfigError(
                    f"failed to read site config at {path}: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise SiteConfigError(
                    f"site config at {path} must be a JSON object, "
                    f"got {type(data).__name__}"
                )
            schema: Any = data.get("schema_version")
            if schema is not None and schema != SUPPORTED_SCHEMA_VERSION:
                raise SiteConfigError(
                    f"site config at {path} declares schema_version "
                    f"{schema!r}; this loader supports "
                    f"{SUPPORTED_SCHEMA_VERSION}"
                )
            return _Site(data, str(path))
    return _Site({}, "(no site.json found)")


# Module-level singleton. Loaded once at import; tests that need a
# different config can monkeypatch ``site`` or set $GLOWUP_SITE_JSON
# before importing this module.
site: _Site = _load()
