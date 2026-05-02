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

Secrets overlay
---------------

Sensitive values (database passwords, API tokens) live in a
companion file:

    /etc/glowup/secrets.json

Same JSON shape as site.json, gitignored everywhere, never committed.
The operator drops it manually until the age-encrypted secrets store
exists.  Loaded after site.json and merged on top — any key in
secrets.json wins over the same key in site.json.  This lets the
glowup-infra renderer keep producing site.json from inventory while
sensitive values stay out of the rendered payload entirely.

Empty / missing secrets.json is fine; programs that need a
secret-only key fail fast at .require() the same way they would for
any other missing site value.
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

# Secrets overlay — read after the main site config and merged on
# top, so any key present in secrets.json wins.  Same JSON shape as
# site.json but typically only the sensitive keys (postgres_dsn, API
# tokens, etc.).  Lives at /etc/glowup/secrets.json (or
# ~/.glowup/secrets.json), gitignored everywhere, never committed —
# the operator drops it manually until the age-encrypted secrets
# store exists.  Empty / missing is fine; programs that need a
# secret-only key fail fast at .require() the same way they would
# for any other missing site value.
_ENV_SECRETS_VAR: str = "GLOWUP_SECRETS_JSON"
_SYSTEM_SECRETS: Path = Path("/etc/glowup/secrets.json")
_USER_SECRETS: Path = Path.home() / ".glowup" / "secrets.json"

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
                "site.json.example in the glowup repo) and restart the "
                "service"
            )
        return v

    # -- Typed feature accessors ----------------------------------------
    #
    # Common site values get typed properties on top of get/require so
    # callers don't repeat the float-coerce + range-check at every site.
    # The free-form ``site.get("foo")`` API still works and remains the
    # right tool for keys this module doesn't know about (operator-added
    # extensions, deploy-specific values).  Adding a typed property here
    # is appropriate when (a) the key is common across the codebase,
    # (b) it has a well-defined unit / range, or (c) silently accepting
    # a malformed value would cause a downstream failure that's hard to
    # trace back to site.json.

    @property
    def latitude(self) -> float:
        """Operator latitude in decimal degrees, ``[-90, 90]``.

        Raises:
            SiteConfigError: If unset, malformed, or out of range.
                Sunrise/sunset and weather lookups all need this; a
                missing value is not a soft fall-through.
        """
        return self._coerce_coord(
            self.require("latitude"), "latitude", -90.0, 90.0,
        )

    @property
    def longitude(self) -> float:
        """Operator longitude in decimal degrees, ``[-180, 180]``.

        Raises:
            SiteConfigError: If unset, malformed, or out of range.
        """
        return self._coerce_coord(
            self.require("longitude"), "longitude", -180.0, 180.0,
        )

    @property
    def electricity_rate_per_kwh(self) -> "float | None":
        """Operator residential electricity rate in $/kWh, or ``None``.

        Optional — features that quote energy cost (voice power
        summary) read this and adapt their reply if it isn't set.
        Returns ``None`` cleanly when the key is absent.

        Raises:
            SiteConfigError: If the value is present but not a number.
                Out-of-range checks are intentionally lenient (rates
                vary widely by region; refusing to load a real-world
                value because it's "too high" would defeat the point).
        """
        val: Any = self.get("electricity_rate_per_kwh")
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError) as exc:
            raise SiteConfigError(
                f"site config key 'electricity_rate_per_kwh' in "
                f"{self._source} must be a number; got {val!r}"
            ) from exc

    @staticmethod
    def _coerce_coord(
        val: Any, key: str, lo: float, hi: float,
    ) -> float:
        """Float-coerce *val* and bounds-check against ``[lo, hi]``.

        Centralised here so latitude / longitude / future per-feature
        coordinates all surface the same error shape — operator sees
        which key failed, what they wrote, and what the legal range is.
        """
        try:
            f: float = float(val)
        except (TypeError, ValueError) as exc:
            raise SiteConfigError(
                f"site config key {key!r} must be a number; "
                f"got {val!r}"
            ) from exc
        if not lo <= f <= hi:
            raise SiteConfigError(
                f"site config key {key!r} must be between {lo} and "
                f"{hi}; got {f}"
            )
        return f


def _candidate_paths(env_var: str, system_path: Path, user_path: Path) -> list[Path]:
    """Return the ordered list of paths to try for a config file.

    Order:
      1. ``$<env_var>`` if set — explicit override for tests and dev.
      2. The system path (``/etc/glowup/...``) — production drop on
         Linux fleet hosts.
      3. The per-user path (``~/.glowup/...``) — Mac dev / non-root.

    The first existing file wins. None existing is fine for the
    secrets overlay (no overlay applied) and for the main site config
    (empty, every ``get`` returns None).
    """
    paths: list[Path] = []
    env_path: str | None = os.environ.get(env_var)
    if env_path:
        paths.append(Path(env_path))
    paths.append(system_path)
    paths.append(user_path)
    return paths


def _read_json_config(path: Path) -> dict[str, Any]:
    """Read + minimally validate a site/secrets JSON document.

    Validates the top-level shape (must be a JSON object) and the
    optional ``schema_version`` field (must match this loader's
    supported version when present).  Other validation is per-call
    via :meth:`_Site.get` / :meth:`_Site.require`.
    """
    try:
        text: str = path.read_text(encoding="utf-8")
        data: Any = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise SiteConfigError(
            f"failed to read site/secrets config at {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise SiteConfigError(
            f"config at {path} must be a JSON object, "
            f"got {type(data).__name__}"
        )
    schema: Any = data.get("schema_version")
    if schema is not None and schema != SUPPORTED_SCHEMA_VERSION:
        raise SiteConfigError(
            f"config at {path} declares schema_version "
            f"{schema!r}; this loader supports "
            f"{SUPPORTED_SCHEMA_VERSION}"
        )
    return data


def _load() -> _Site:
    """Locate, parse, and merge site.json + secrets.json.

    Returns an empty site if no candidate site path exists — that is
    intentional, see the module docstring.  Secrets are merged on top
    (same-named keys win), so a sensitive value can sit in
    secrets.json while the rest of site.json stays freely renderable
    from inventory.

    ``schema_version`` is special: secrets.json's value (if any) is
    validated but does not overwrite site.json's.
    """
    site_data: dict[str, Any] = {}
    sources: list[str] = []

    # Main site config — pick the first existing.
    for path in _candidate_paths(_ENV_PATH_VAR, _SYSTEM_PATH, _USER_PATH):
        if path.is_file():
            site_data = _read_json_config(path)
            sources.append(str(path))
            break

    # Secrets overlay — pick the first existing; values overwrite
    # matching keys in site_data.
    for path in _candidate_paths(_ENV_SECRETS_VAR, _SYSTEM_SECRETS, _USER_SECRETS):
        if path.is_file():
            secrets_data: dict[str, Any] = _read_json_config(path)
            # Don't let secrets.json clobber a different schema_version
            # in site.json — the validation already ran in
            # _read_json_config; keep the original.
            secrets_data.pop("schema_version", None)
            site_data = {**site_data, **secrets_data}
            sources.append(str(path))
            break

    label: str = " + ".join(sources) if sources else "(no site.json found)"
    return _Site(site_data, label)


# Module-level singleton. Loaded once at import; tests that need a
# different config can monkeypatch ``site`` or set $GLOWUP_SITE_JSON
# before importing this module.
site: _Site = _load()
