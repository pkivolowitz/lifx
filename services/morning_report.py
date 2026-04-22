#!/usr/bin/env python3
"""GlowUp Morning Report — comprehensive fleet health email.

Runs daily at 06:00 CDT on the hub (.214) via systemd timer.
Gathers health data from all fleet hosts, checks service states,
reads API status, optionally runs the test suite on a dev Mac,
and sends a formatted HTML email.

Credential file: /etc/glowup/email.json (mode 0600)
Server config:   /etc/glowup/server.json
Timer unit:      glowup-morning-report.timer

Can also be run manually:
    /home/a/venv/bin/python3 /home/a/lifx/services/morning_report.py
"""

__version__: str = "1.0.0"

import fnmatch
import json
import logging
import smtplib
import socket
import ssl
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Credential and config paths.
EMAIL_CONFIG_PATH: str = "/etc/glowup/email.json"
SERVER_CONFIG_PATH: str = "/etc/glowup/server.json"

# SMTP defaults (overridable via email.json).
SMTP_HOST: str = "smtp.gmail.com"
SMTP_PORT: int = 587

# API endpoint — report runs on the hub, so localhost.
API_BASE: str = "http://localhost:8420"

# SSH timeout for fleet host checks (seconds).
SSH_TIMEOUT: int = 10

# HTTP timeout for API calls (seconds).
HTTP_TIMEOUT: int = 10

# NAS bare repo for git status.
NAS_GIT_USER: str = "perryk"
NAS_GIT_HOST: str = "10.0.0.24"
NAS_GIT_PATH: str = "/mnt/storage/perryk/git/lifx.git"

# Dev Mac for running the test suite.  Daedalus (.191) is the
# primary test host — always on, deploy includes tests/.
# If unreachable at report time, tests are skipped with a note.
TEST_HOST_USER: str = "perrykivolowitz"
TEST_HOST_IP: str = "10.0.0.191"  # Daedalus
TEST_VENV: str = "~/venv/bin/python"
TEST_REPO: str = "~/lifx"

# Fleet hosts — key is display name, value is connection and role info.
# Fleet hosts.
#   "os"       — "linux", "macos", or "freebsd"; controls health commands.
#   "local"    — True if the report runs on this host (skip SSH).
#   "services" — systemd units (linux) or launchd labels (macos).
#   "processes" — non-service processes checked via pgrep.
#   "zpool"    — ZFS pool name to check (freebsd).
FLEET: dict[str, dict] = {
    "glowup (.214)": {
        "ip": "10.0.0.214",
        "user": "a",
        "os": "linux",
        "role": "Hub: primary server, Dining Room satellite, MQTT broker",
        "local": True,
        "repo_path": "/home/a/lifx",
        "services": [
            "glowup-server",
            "glowup-satellite",
            "glowup-keepalive",
            "glowup-adapter@vivint",
            "glowup-adapter@nvr",
            "glowup-adapter@printer",
            "glowup-adapter@matter",
            "mosquitto",
            "pi-thermal",
        ],
    },
    "broker-2 (.123)": {
        "ip": "10.0.0.123",
        "user": "a",
        "os": "linux",
        "role": "Zigbee coordinator, BLE gateway, secondary MQTT broker",
        # No repo_path: broker-2 uses per-role /opt/glowup-* layout,
        # not a single ~/lifx tree. Drift check skips it until that
        # deployment shape is normalized by the installer.
        "services": [
            "glowup-zigbee-service",
            "glowup-ble-sensor",
            "zigbee2mqtt",
            "mosquitto",
        ],
    },
    "mbclock (.220)": {
        "ip": "10.0.0.220",
        "user": "a",
        "os": "linux",
        "role": "Bedroom kiosk, Main Bedroom satellite, thermal sensor",
        "repo_path": "/home/a/lifx",
        "services": [
            "glowup-satellite",
            "pi-thermal",
        ],
        "processes": [
            {
                "label": "kiosk",
                "pattern": "^[^ ]*python[^ ]* -m kiosk",
            },
        ],
    },
    "ernie (.153)": {
        "ip": "10.0.0.153",
        "user": "perryk",
        "os": "linux",
        "role": (
            "Odroid N2+: far-side BLE + SDR sniffer "
            "(ASUS BT dongle, RTL-SDR), local MQTT broker bridging "
            "glowup/ble/# glowup/tpms/# glowup/hardware/# glowup/node/# "
            "to hub. No repo tree — services run from /opt/ernie "
            "and /opt/glowup-sensors."
        ),
        "services": [
            "mosquitto",
            "ble-sniffer",
            "rtl433",
            "pi-thermal",
        ],
    },
    "Daedalus (.191)": {
        "ip": "10.0.0.191",
        "user": "perrykivolowitz",
        "os": "macos",
        "role": "Mac Studio — voice coordinator, development",
        "repo_path": "/Users/perrykivolowitz/lifx",
        "services": [
            "com.glowup.ollama-preload",
        ],
        "processes": [
            {
                "label": "voice-coordinator",
                "pattern": "voice.coordinator.daemon",
            },
        ],
    },
}

# Vivint battery warning threshold (percent).
BATTERY_WARNING_PCT: int = 30

# Door lock battery warning threshold (percent) — higher than sensors
# because a dead lock strands you outside; replace earlier.
LOCK_WARNING_PCT: int = 40

# Logging setup.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger: logging.Logger = logging.getLogger("glowup.morning_report")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ssh(host: str, user: str, cmd: str,
         timeout: int = SSH_TIMEOUT,
         stdin: str | None = None) -> tuple[bool, str]:
    """Run a command on a remote host via SSH.

    Returns (success, output).  On failure, output contains the
    error message rather than raising. Host-key mismatch (common after
    a rebuild) is surfaced explicitly — it used to fall through as
    generic "unreachable".
    """
    try:
        ssh_argv: list[str] = ["ssh"]
        if stdin is None:
            ssh_argv.append("-n")
        ssh_argv += [
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            f"{user}@{host}", cmd,
        ]
        result: subprocess.CompletedProcess = subprocess.run(
            ssh_argv,
            input=stdin,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return (True, result.stdout.strip())
        stderr: str = result.stderr or ""
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr:
            return (
                False,
                f"host key mismatch (run: ssh-keygen -R {host})",
            )
        if "Permission denied" in stderr:
            return (False, "ssh permission denied (authorized_keys?)")
        last: str = (stderr or result.stdout or "ssh failed").strip()
        return (False, last.splitlines()[-1] if last else "ssh failed")
    except subprocess.TimeoutExpired:
        return (False, "SSH timeout")
    except Exception as exc:
        return (False, str(exc))


def _local(cmd: str, timeout: int = SSH_TIMEOUT) -> tuple[bool, str]:
    """Run a local shell command.  Returns (success, output)."""
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout,
        )
        return (result.returncode == 0, result.stdout.strip())
    except subprocess.TimeoutExpired:
        return (False, "command timeout")
    except Exception as exc:
        return (False, str(exc))


def _api_get(path: str, token: str) -> tuple[bool, Any]:
    """GET from the local GlowUp API.  Returns (success, data)."""
    url: str = f"{API_BASE}{path}"
    req: urllib.request.Request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return (True, json.loads(resp.read().decode()))
    except Exception as exc:
        return (False, str(exc))


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _run(host: str, user: str, cmd: str,
         timeout: int = SSH_TIMEOUT,
         is_local: bool = False) -> tuple[bool, str]:
    """Run a command locally or via SSH depending on ``is_local``."""
    if is_local:
        return _local(cmd, timeout)
    return _ssh(host, user, cmd, timeout)


def collect_host_health(name: str, info: dict) -> dict[str, Any]:
    """Gather health metrics from a single fleet host.

    Handles Linux (systemd), macOS (launchctl), and FreeBSD
    (TrueNAS CORE) hosts via the ``os`` field in the fleet config.
    """
    host: str = info["ip"]
    user: str = info["user"]
    local: bool = info.get("local", False)
    platform: str = info.get("os", "linux")
    result: dict[str, Any] = {
        "name": name,
        "role": info["role"],
        "reachable": False,
        "uptime": "",
        "cpu_temp": "",
        "disk": "",
        "memory": "",
        "load": "",
        "services": {},
        "zpool": "",
    }

    # Reachability — uptime works on all three platforms.
    ok, uptime = _run(host, user, "uptime", is_local=local)
    if not ok:
        return result
    result["reachable"] = True
    result["uptime"] = uptime

    # --- CPU temperature (platform-specific) ---
    if platform == "linux":
        _, temp = _run(
            host, user,
            "cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null",
            is_local=local,
        )
        if temp and temp.isdigit():
            result["cpu_temp"] = f"{int(temp) / 1000:.1f} C"
    elif platform == "freebsd":
        # TrueNAS CORE — CPU temp via sysctl.
        _, temp = _run(
            host, user,
            "sysctl -n dev.cpu.0.temperature 2>/dev/null",
            is_local=local,
        )
        if temp:
            result["cpu_temp"] = temp.strip()

    # --- Disk usage ---
    _, disk = _run(host, user, "df -h / | tail -1", is_local=local)
    result["disk"] = disk

    # --- ZFS pool health (FreeBSD / TrueNAS) ---
    zpool_name: str = info.get("zpool", "")
    if zpool_name:
        _, zstatus = _run(
            host, user,
            f"zpool status {zpool_name} 2>/dev/null | head -15",
            is_local=local,
        )
        result["zpool"] = zstatus
        _, zlist = _run(
            host, user,
            f"zpool list {zpool_name} 2>/dev/null",
            is_local=local,
        )
        if zlist:
            result["zpool_capacity"] = zlist

    # --- Memory (platform-specific) ---
    if platform == "linux":
        _, mem = _run(host, user,
                      "free -h | grep Mem", is_local=local)
        result["memory"] = mem
    elif platform == "macos":
        _, mem = _run(
            host, user,
            "sysctl -n hw.memsize 2>/dev/null",
            is_local=local,
        )
        if mem and mem.isdigit():
            gb: float = int(mem) / (1024 ** 3)
            result["memory"] = f"{gb:.0f} GB total"
    elif platform == "freebsd":
        _, mem = _run(
            host, user,
            "sysctl -n hw.physmem 2>/dev/null",
            is_local=local,
        )
        if mem and mem.isdigit():
            gb = int(mem) / (1024 ** 3)
            result["memory"] = f"{gb:.0f} GB total"

    # --- Load average (platform-specific) ---
    if platform == "linux":
        _, load = _run(host, user,
                       "cat /proc/loadavg 2>/dev/null",
                       is_local=local)
        if load:
            result["load"] = " ".join(load.split()[:3])
    else:
        # macOS and FreeBSD — parse from uptime output.
        # uptime format: "... load averages: 1.23 2.34 3.45"
        for token in ("load averages:", "load average:"):
            if token in uptime:
                tail: str = uptime.split(token)[-1].strip()
                result["load"] = tail.replace(",", " ")
                break

    # --- Service statuses (platform-specific) ---
    services: list[str] = info.get("services", [])
    if platform == "linux":
        for svc in services:
            _, state = _run(
                host, user,
                f"systemctl is-active {svc} 2>/dev/null",
                is_local=local,
            )
            result["services"][svc] = state if state else "unknown"
    elif platform == "macos":
        # launchctl list exits 0 if label exists, prints PID or "-".
        for svc in services:
            ok_svc, out = _run(
                host, user,
                f"launchctl list {svc} 2>/dev/null && echo active || echo inactive",
                is_local=local,
            )
            # launchctl list <label> prints multi-line info if found.
            result["services"][svc] = (
                "active" if ok_svc else "inactive"
            )

    # --- Non-service processes (kiosk, coordinator, etc.) ---
    for proc in info.get("processes", []):
        _, out = _run(
            host, user,
            f"pgrep -f '{proc['pattern']}' >/dev/null 2>&1 "
            f"&& echo active || echo dead",
            is_local=local,
        )
        result["services"][proc["label"]] = out if out else "unknown"

    # --- Recent errors (Linux only — journalctl) ---
    if platform == "linux":
        _, errors = _run(
            host, user,
            "sudo journalctl --since '30 min ago' -p err --no-pager "
            "-u 'glowup-*' -u 'zigbee*' -u mosquitto 2>/dev/null "
            "| tail -10",
            is_local=local,
        )
        result["errors"] = errors

    return result


def collect_api_status(token: str) -> dict[str, Any]:
    """Gather server API status including all adapters."""
    ok, data = _api_get("/api/status", token)
    if not ok:
        return {"reachable": False, "error": str(data)}
    return {"reachable": True, "data": data}


def collect_vivint_batteries(api_data: dict) -> list[dict[str, Any]]:
    """Extract Vivint sensor batteries, flag low ones."""
    warnings: list[dict[str, Any]] = []
    vivint: dict = api_data.get("adapters", {}).get("vivint", {})
    sensors: dict = vivint.get("sensors", {})
    for key, sensor in sensors.items():
        battery: int = sensor.get("battery", 100)
        entry: dict[str, Any] = {
            "name": sensor.get("name", key),
            "battery": battery,
            "type": sensor.get("sensor_type", "unknown"),
            "low": battery < BATTERY_WARNING_PCT,
        }
        warnings.append(entry)
    # Sort by battery level ascending so worst are first.
    warnings.sort(key=lambda x: x["battery"])
    return warnings


def collect_vivint_locks(api_data: dict) -> list[dict[str, Any]]:
    """Extract Vivint door-lock batteries. Adapter reports 0.0–1.0."""
    locks_out: list[dict[str, Any]] = []
    vivint: dict = api_data.get("adapters", {}).get("vivint", {})
    locks: dict = vivint.get("locks", {})
    for key, lock in locks.items():
        raw: Any = lock.get("battery")
        if raw is None:
            pct: int = 0
            missing: bool = True
        else:
            # Adapter normalizes lock batteries to 0.0–1.0; sensors are 0–100.
            pct = int(round(float(raw) * 100)) if float(raw) <= 1.0 else int(raw)
            missing = False
        label: str = key.replace("_", " ").title()
        locks_out.append({
            "name": label,
            "key": key,
            "battery": pct,
            "locked": bool(lock.get("lock_state")),
            "low": (not missing) and pct < LOCK_WARNING_PCT,
            "missing": missing,
        })
    locks_out.sort(key=lambda x: x["battery"])
    return locks_out


def collect_mqtt_rate(host: str, user: str,
                      seconds: int = 5,
                      is_local: bool = False) -> tuple[bool, float]:
    """Sample MQTT message rate on a host.

    Subscribes to all topics for ``seconds`` seconds and counts
    messages.  Returns (success, messages_per_second).  The count
    includes retained messages that replay on subscribe, so the
    rate is an upper bound — fine for a daily report.
    """
    ok, output = _run(
        host, user,
        f"timeout {seconds} mosquitto_sub -t '#' -v 2>&1 | wc -l",
        timeout=seconds + SSH_TIMEOUT,
        is_local=is_local,
    )
    if not ok:
        return (False, 0.0)
    try:
        count: int = int(output.strip())
        return (True, count / seconds)
    except ValueError:
        return (False, 0.0)


def collect_git_status() -> dict[str, Any]:
    """Get recent commits and branch info from the NAS bare repo."""
    result: dict[str, Any] = {"reachable": False, "log": "", "branches": ""}

    ok, log = _ssh(
        NAS_GIT_HOST, NAS_GIT_USER,
        f"git --git-dir={NAS_GIT_PATH} log --oneline --decorate -10",
    )
    if not ok:
        result["error"] = log
        return result

    result["reachable"] = True
    result["log"] = log

    _, branches = _ssh(
        NAS_GIT_HOST, NAS_GIT_USER,
        f"git --git-dir={NAS_GIT_PATH} branch -v",
    )
    result["branches"] = branches
    return result


def collect_test_results() -> dict[str, Any]:
    """Run the test suite on the dev Mac and capture results.

    Returns a dict with pass/fail counts and summary.  If the dev
    Mac is unreachable, returns a note to that effect.
    """
    result: dict[str, Any] = {"reachable": False, "summary": ""}

    # Test suite takes ~5 minutes; generous timeout.
    ok, output = _ssh(
        TEST_HOST_IP, TEST_HOST_USER,
        f"cd {TEST_REPO} && {TEST_VENV} -m pytest tests/ "
        f"--ignore=tests/boneyard -q 2>&1 | tail -5",
        timeout=600,
    )
    if not ok:
        result["error"] = output if output else "dev Mac unreachable"
        return result

    result["reachable"] = True
    result["summary"] = output
    return result


def collect_stt_state(host: str, user: str) -> dict[str, Any]:
    """Read the voice coordinator's STT engine state file from Daedalus.

    The file is written atomically by the coordinator at
    ``~/.glowup/stt_state.json`` whenever the active engine changes
    (boot, primary load success, primary load failure with fallback,
    both-engines-down).  Schema::

        {
          "engine":          "mlx-whisper",      # currently active
          "primary_engine":  "mlx-whisper",      # configured primary
          "fallback_reason": "",                 # non-empty if degraded
          "since":           "2026-04-20T15:42:03+00:00"
        }

    Returns a dict with:
        - reachable: bool     (False if SSH or JSON parse failed)
        - degraded:  bool     (True if engine != primary or reason set)
        - engine, primary_engine, fallback_reason, since
        - error: str          (only present if reachable is False)
    """
    ok, out = _ssh(
        host, user,
        "cat ~/.glowup/stt_state.json 2>/dev/null",
    )
    if not ok or not out.strip():
        return {
            "reachable": False,
            "error": (
                "state file missing at ~/.glowup/stt_state.json "
                "— coordinator may be down, never started, or running "
                "an older build without the state writer"
            ),
        }
    try:
        data: dict = json.loads(out)
    except json.JSONDecodeError as exc:
        return {
            "reachable": False,
            "error": f"state file not valid JSON: {exc}",
        }
    engine: str = data.get("engine", "unknown")
    primary: str = data.get("primary_engine", engine)
    reason: str = data.get("fallback_reason", "")
    degraded: bool = (engine != primary) or bool(reason)
    return {
        "reachable": True,
        "degraded": degraded,
        "engine": engine,
        "primary_engine": primary,
        "fallback_reason": reason,
        "since": data.get("since", ""),
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

# Inline CSS — email clients strip <link> and <style> in <head>,
# so everything must be inline.  Keep it readable and professional.
CSS: str = """
body {
    font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    background: #f5f5f5; color: #222; margin: 0; padding: 20px;
    line-height: 1.5;
}
.container { max-width: 960px; margin: 0 auto; background: #fff;
    border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.1);
    overflow: hidden; }
.header { background: #1a1a2e; color: #e0e0e0; padding: 20px 24px; }
.header h1 { margin: 0; font-size: 22px; font-weight: 600; }
.header .date { color: #aaa; font-size: 14px; margin-top: 4px; }
.section { padding: 16px 24px; border-bottom: 1px solid #eee; }
.section:last-child { border-bottom: none; }
.section h2 { margin: 0 0 12px 0; font-size: 16px; color: #1a1a2e;
    text-transform: uppercase; letter-spacing: 0.5px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; padding: 6px 8px; background: #f0f0f0;
    font-weight: 600; border-bottom: 2px solid #ddd; }
td { padding: 6px 8px; border-bottom: 1px solid #eee;
    vertical-align: top; }
.ok { color: #2e7d32; font-weight: 600; }
.warn { color: #e65100; font-weight: 600; }
.fail { color: #c62828; font-weight: 600; }
.muted { color: #888; font-size: 12px; }
pre { background: #f8f8f8; padding: 10px; border-radius: 4px;
    font-size: 12px; overflow-x: auto; white-space: pre-wrap;
    word-wrap: break-word; margin: 8px 0; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600; }
.badge-green { background: #e8f5e9; color: #2e7d32; }
.badge-yellow { background: #fff3e0; color: #e65100; }
.badge-red { background: #ffebee; color: #c62828; }
.summary-grid { display: flex; gap: 12px; flex-wrap: wrap; }
.summary-card { flex: 1; min-width: 150px; padding: 12px;
    border-radius: 6px; border: 1px solid #eee; }
"""


# ---------------------------------------------------------------------------
# Fleet code drift — compare each host's on-disk files to the NAS bare repo.
# Uses git blob hashes (sha1("blob <size>\0<content>")) so the host side
# can run plain Python and the reference side runs `git ls-tree -r HEAD`.
# Files missing on a host are NOT flagged — every host runs a subset. A file
# present on the host that mismatches (or is absent from the reference) is.
# ---------------------------------------------------------------------------

# Directory names to skip when walking a host's repo copy.
_DRIFT_EXCLUDE_DIRS: set[str] = {
    ".git", "__pycache__", "venv", ".venv", "env", ".env",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".tox", ".idea", ".vscode",
}

# Extensions / filename suffixes never worth diffing (generated, editor junk).
_DRIFT_SKIP_SUFFIXES: tuple[str, ...] = (
    ".pyc", ".pyo", ".log", ".tmp", ".swp", ".swo", ".DS_Store",
)

# fnmatch patterns (paths relative to the repo root on the host). Files
# matching any of these are silently dropped from the "extra" list —
# they exist on the host legitimately but are not repo contents. Add
# here, not via per-host config, because these are the same everywhere.
_DRIFT_EXTRA_IGNORE: tuple[str, ...] = (
    "DEPLOYED",            # deploy marker
    "state.db",            # runtime db
    "state.db-journal",    # sqlite journal
    "state.db-wal",        # sqlite WAL
    "state.db-shm",        # sqlite shared mem
    "ble_pairing.json",    # BLE keys
    ".claude/",            # IDE settings (trailing / = dir prefix)
    "*.local.json",        # per-host overrides
    "*.pem",               # certs
    "*.key",               # keys
    ".DS_Store",
    "*xcuserdata*",        # Xcode per-user IDE state (regenerates)
    "installer/install.py",      # installer artifact, not in git
    "installer/static/",         # installer web assets, not in git
)


def _drift_ignored(path: str) -> bool:
    """True if *path* matches any ignorelist entry.

    Entries ending in '/' are treated as directory prefixes (fnmatch
    globs don't cross '/'). Others are fnmatched against both the full
    path and the basename.
    """
    base: str = path.rsplit("/", 1)[-1]
    for pat in _DRIFT_EXTRA_IGNORE:
        if pat.endswith("/"):
            if path.startswith(pat) or ("/" + pat) in ("/" + path + "/"):
                return True
        else:
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(base, pat):
                return True
    return False

# Python script the host runs to emit "<blob-sha> <relpath>" lines.
# Reads root from stdin's argv (first line). Embedded via ssh -i stdin.
_DRIFT_HOST_SCRIPT: str = r"""
import os, sys, hashlib
root = sys.argv[1] if len(sys.argv) > 1 else "."
EX_DIRS = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else set()
EX_SUF = tuple(sys.argv[3].split(",")) if len(sys.argv) > 3 else ()
try:
    os.chdir(os.path.expanduser(root))
except OSError as e:
    print("__ERROR__", e, file=sys.stderr)
    sys.exit(2)
for dp, dn, fn in os.walk("."):
    dn[:] = [d for d in dn if d not in EX_DIRS]
    for f in fn:
        if f.endswith(EX_SUF):
            continue
        p = os.path.join(dp, f)
        try:
            size = os.path.getsize(p)
            h = hashlib.sha1()
            h.update(b"blob " + str(size).encode() + b"\0")
            with open(p, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            rel = p[2:] if p.startswith("./") else p
            print(h.hexdigest(), rel)
        except OSError:
            pass
"""


def _fetch_reference_manifest() -> tuple[dict[str, str], str]:
    """Fetch {path: blob_sha} from the NAS bare repo at HEAD.

    Returns (manifest, ref_sha). Empty manifest on failure.
    """
    cmd: str = (
        f"git --git-dir={NAS_GIT_PATH} rev-parse HEAD && "
        f"git --git-dir={NAS_GIT_PATH} ls-tree -r HEAD"
    )
    ok, out = _ssh(NAS_GIT_HOST, NAS_GIT_USER, cmd, timeout=20)
    if not ok or not out:
        logger.warning("Drift: reference manifest fetch failed: %s", out)
        return ({}, "")
    lines: list[str] = out.splitlines()
    if not lines:
        return ({}, "")
    ref_sha: str = lines[0].strip()
    manifest: dict[str, str] = {}
    for line in lines[1:]:
        # Format: "<mode> <type> <sha>\t<path>"
        try:
            meta, path = line.split("\t", 1)
        except ValueError:
            continue
        parts: list[str] = meta.split()
        if len(parts) >= 3 and parts[1] == "blob":
            manifest[path] = parts[2]
    return (manifest, ref_sha)


def _parse_host_manifest(text: str) -> dict[str, str]:
    """Parse "<sha> <path>" lines into a dict."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        sha, _, path = line.partition(" ")
        if len(sha) == 40:
            result[path] = sha
    return result


def _host_manifest(info: dict[str, Any]) -> tuple[bool, dict[str, str], str]:
    """Walk one host's repo copy and hash every file.

    Returns (ok, {path: sha}, error_text).
    """
    repo_path: str | None = info.get("repo_path")
    if not repo_path:
        return (False, {}, "no repo_path configured")

    ex_dirs: str = ",".join(sorted(_DRIFT_EXCLUDE_DIRS))
    ex_suf: str = ",".join(_DRIFT_SKIP_SUFFIXES)
    # Run: python3 - <repo_path> <ex_dirs> <ex_suf>  with script on stdin.
    argv_str: str = f"python3 - {repo_path} {ex_dirs} '{ex_suf}'"

    if info.get("local"):
        try:
            result = subprocess.run(
                ["python3", "-", repo_path, ex_dirs, ex_suf],
                input=_DRIFT_HOST_SCRIPT,
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return (False, {}, (result.stderr or "local hash failed").strip())
            return (True, _parse_host_manifest(result.stdout), "")
        except Exception as exc:
            return (False, {}, str(exc))

    ok, out = _ssh(
        info["ip"], info["user"], argv_str,
        timeout=120, stdin=_DRIFT_HOST_SCRIPT,
    )
    if not ok:
        return (False, {}, out)
    return (True, _parse_host_manifest(out), "")


def collect_drift() -> dict[str, Any]:
    """Compare each fleet host's repo tree to the NAS reference.

    Returns a dict with per-host drift/extra lists and the reference SHA.
    """
    reference, ref_sha = _fetch_reference_manifest()
    per_host: dict[str, dict[str, Any]] = {}
    if not reference:
        return {
            "reachable": False,
            "ref_sha": "",
            "hosts": per_host,
            "error": "could not fetch reference manifest from NAS",
        }

    for name, info in FLEET.items():
        if not info.get("repo_path"):
            per_host[name] = {
                "ok": True,
                "skipped": True,
                "error": "",
                "drifted": [],
                "extra": [],
                "count": 0,
            }
            continue
        ok, host_files, err = _host_manifest(info)
        if not ok:
            per_host[name] = {
                "ok": False,
                "error": err,
                "drifted": [],
                "extra": [],
                "count": 0,
            }
            continue
        drifted: list[str] = []
        extra: list[str] = []
        ignored: list[str] = []
        for path, sha in host_files.items():
            ref_sha_for_path: str | None = reference.get(path)
            if ref_sha_for_path is None:
                if _drift_ignored(path):
                    ignored.append(path)
                else:
                    extra.append(path)
            elif ref_sha_for_path != sha:
                drifted.append(path)
        per_host[name] = {
            "ok": True,
            "error": "",
            "drifted": sorted(drifted),
            "extra": sorted(extra),
            "ignored": sorted(ignored),
            "count": len(host_files),
        }
    return {
        "reachable": True,
        "ref_sha": ref_sha,
        "hosts": per_host,
        "error": "",
    }


def _svc_badge(state: str) -> str:
    """Render a service state as a colored badge."""
    if state == "active":
        return '<span class="badge badge-green">active</span>'
    elif state in ("inactive", "dead"):
        return '<span class="badge badge-yellow">inactive</span>'
    else:
        return f'<span class="badge badge-red">{_esc(state)}</span>'


def _host_badge(reachable: bool) -> str:
    """Render host reachability as a badge."""
    if reachable:
        return '<span class="badge badge-green">online</span>'
    return '<span class="badge badge-red">unreachable</span>'


def render_html(
    hosts: list[dict[str, Any]],
    api_status: dict[str, Any],
    locks: list[dict[str, Any]],
    batteries: list[dict[str, Any]],
    mqtt_rates: dict[str, tuple[bool, float]],
    git: dict[str, Any],
    tests: dict[str, Any],
    stt_state: dict[str, Any],
    now: datetime,
    drift: dict[str, Any] | None = None,
) -> str:
    """Build the full HTML email body."""
    parts: list[str] = []

    # --- Header ---
    parts.append(f"""
    <div class="header">
        <h1>GlowUp Morning Report</h1>
        <div class="date">{now.strftime('%A, %B %d, %Y  %H:%M %Z')}</div>
    </div>""")

    # --- Door Locks (first — safety critical) ---
    if locks:
        low_locks: list[dict] = [l for l in locks if l["low"] or l["missing"]]
        parts.append('<div class="section"><h2>Door Locks</h2>')
        if low_locks:
            parts.append(
                f'<p class="fail">{len(low_locks)} lock(s) at or below '
                f'{LOCK_WARNING_PCT}% — replace before they fail.</p>'
            )
        parts.append(
            "<table><tr><th>Lock</th><th>State</th>"
            "<th>Battery</th></tr>"
        )
        for l in locks:
            if l["missing"]:
                cls: str = "fail"
                batt_text: str = "?"
            elif l["battery"] < 25:
                cls = "fail"
                batt_text = f'{l["battery"]}%'
            elif l["low"]:
                cls = "warn"
                batt_text = f'{l["battery"]}%'
            else:
                cls = "ok"
                batt_text = f'{l["battery"]}%'
            state_text: str = "LOCKED" if l["locked"] else "UNLOCKED"
            state_cls: str = "ok" if l["locked"] else "warn"
            parts.append(
                f"<tr><td><strong>{_esc(l['name'])}</strong></td>"
                f'<td class="{state_cls}">{state_text}</td>'
                f'<td class="{cls}">{batt_text}</td></tr>'
            )
        parts.append("</table></div>")

    # --- Fleet Summary ---
    parts.append('<div class="section"><h2>Fleet Status</h2>')
    parts.append("<table><tr><th>Host</th><th>Role</th>"
                 "<th>Status</th><th>Uptime</th><th>Temp</th>"
                 "<th>Load</th></tr>")
    for h in hosts:
        parts.append(
            f"<tr><td><strong>{_esc(h['name'])}</strong></td>"
            f"<td class='muted'>{_esc(h['role'])}</td>"
            f"<td>{_host_badge(h['reachable'])}</td>"
            f"<td>{_esc(h.get('uptime', ''))}</td>"
            f"<td>{_esc(h.get('cpu_temp', ''))}</td>"
            f"<td>{_esc(h.get('load', ''))}</td></tr>"
        )
    parts.append("</table></div>")

    # --- Services ---
    parts.append('<div class="section"><h2>Services</h2>')
    for h in hosts:
        if not h["reachable"] or not h.get("services"):
            continue
        parts.append(f"<p><strong>{_esc(h['name'])}</strong></p>")
        parts.append("<table><tr><th>Service</th><th>State</th></tr>")
        for svc, state in h["services"].items():
            parts.append(
                f"<tr><td>{_esc(svc)}</td>"
                f"<td>{_svc_badge(state)}</td></tr>"
            )
        parts.append("</table>")
    parts.append("</div>")

    # --- Code drift across fleet ---
    if drift is not None:
        parts.append('<div class="section"><h2>Fleet Code Drift</h2>')
        if not drift.get("reachable"):
            parts.append(
                f'<p class="fail">Drift check unavailable — '
                f'{_esc(drift.get("error", "unknown"))}</p>'
            )
        else:
            ref_sha: str = drift.get("ref_sha", "")
            parts.append(
                f'<p class="muted">Reference: NAS <code>{_esc(ref_sha[:10])}</code></p>'
            )
            any_drift: bool = False
            parts.append(
                "<table><tr><th>Host</th><th>Files</th>"
                "<th>Drifted</th><th>Extra</th><th>Status</th></tr>"
            )
            for hname, hres in drift.get("hosts", {}).items():
                if not hres.get("ok"):
                    parts.append(
                        f"<tr><td><strong>{_esc(hname)}</strong></td>"
                        f"<td colspan='3' class='muted'>—</td>"
                        f"<td class='fail'>{_esc(hres.get('error', 'error'))}"
                        f"</td></tr>"
                    )
                    continue
                if hres.get("skipped"):
                    parts.append(
                        f"<tr><td><strong>{_esc(hname)}</strong></td>"
                        f"<td colspan='3' class='muted'>—</td>"
                        f"<td class='muted'>skipped (custom layout)</td></tr>"
                    )
                    continue
                d_ct: int = len(hres["drifted"])
                e_ct: int = len(hres["extra"])
                if d_ct or e_ct:
                    any_drift = True
                    status_html: str = (
                        '<span class="badge badge-red">DRIFT</span>'
                        if d_ct else
                        '<span class="badge badge-yellow">EXTRA</span>'
                    )
                else:
                    status_html = '<span class="badge badge-green">OK</span>'
                parts.append(
                    f"<tr><td><strong>{_esc(hname)}</strong></td>"
                    f"<td>{hres['count']}</td>"
                    f"<td class='{'fail' if d_ct else 'muted'}'>{d_ct}</td>"
                    f"<td class='{'warn' if e_ct else 'muted'}'>{e_ct}</td>"
                    f"<td>{status_html}</td></tr>"
                )
            parts.append("</table>")
            if any_drift:
                for hname, hres in drift.get("hosts", {}).items():
                    if not hres.get("ok"):
                        continue
                    if not hres["drifted"] and not hres["extra"]:
                        continue
                    parts.append(f"<p><strong>{_esc(hname)}</strong></p>")
                    if hres["drifted"]:
                        items_d: str = "".join(
                            f"<li class='fail'>{_esc(p)}</li>"
                            for p in hres["drifted"][:50]
                        )
                        more_d: str = (
                            f"<li class='muted'>… +{len(hres['drifted']) - 50} more</li>"
                            if len(hres["drifted"]) > 50 else ""
                        )
                        parts.append(
                            f"<p class='muted'>drifted from reference:</p>"
                            f"<ul>{items_d}{more_d}</ul>"
                        )
                    if hres["extra"]:
                        items_e: str = "".join(
                            f"<li class='warn'>{_esc(p)}</li>"
                            for p in hres["extra"][:50]
                        )
                        more_e: str = (
                            f"<li class='muted'>… +{len(hres['extra']) - 50} more</li>"
                            if len(hres["extra"]) > 50 else ""
                        )
                        parts.append(
                            f"<p class='muted'>not in reference:</p>"
                            f"<ul>{items_e}{more_e}</ul>"
                        )
        parts.append("</div>")

    # --- Voice / STT ---
    parts.append('<div class="section"><h2>Voice &middot; STT</h2>')
    if not stt_state.get("reachable"):
        parts.append(
            f'<p class="warn">STT state unavailable &mdash; '
            f'{_esc(stt_state.get("error", "unknown"))}</p>'
        )
    else:
        engine: str = stt_state["engine"]
        primary: str = stt_state["primary_engine"]
        reason: str = stt_state["fallback_reason"]
        since: str = stt_state.get("since", "")
        if stt_state["degraded"]:
            badge = (
                f'<span class="badge badge-red">DEGRADED</span>'
            )
            parts.append(
                f'<p class="fail">Daedalus STT is on the fallback engine. '
                f'Active: <strong>{_esc(engine)}</strong> &middot; '
                f'configured primary: <strong>{_esc(primary)}</strong> '
                f'{badge}</p>'
            )
            if reason:
                parts.append(
                    f'<p class="fail"><strong>Reason:</strong> '
                    f'{_esc(reason)}</p>'
                )
            if since:
                parts.append(
                    f'<p class="muted">Since {_esc(since)}</p>'
                )
        else:
            parts.append(
                f'<p>Daedalus STT: <strong>{_esc(engine)}</strong> '
                f'<span class="badge badge-green">primary</span></p>'
            )
            if since:
                parts.append(
                    f'<p class="muted">Active since {_esc(since)}</p>'
                )
    parts.append("</div>")

    # --- API / Adapters ---
    parts.append('<div class="section"><h2>Server API</h2>')
    if api_status.get("reachable"):
        data: dict = api_status["data"]
        status_text: str = data.get("status", "unknown")
        badge: str = ('<span class="badge badge-green">ready</span>'
                      if status_text == "ready"
                      else f'<span class="badge badge-red">'
                      f'{_esc(status_text)}</span>')
        parts.append(f"<p>Server: {badge}</p>")
        adapters: dict = data.get("adapters", {})
        if adapters:
            parts.append(
                "<table><tr><th>Adapter</th><th>Status</th></tr>")
            for name, info in adapters.items():
                running: bool = info.get("running", False)
                connected: bool = info.get("connected", True)
                if running and connected:
                    a_badge = '<span class="badge badge-green">ok</span>'
                elif running:
                    a_badge = ('<span class="badge badge-yellow">'
                               'running (disconnected)</span>')
                else:
                    a_badge = '<span class="badge badge-red">down</span>'
                parts.append(
                    f"<tr><td>{_esc(name)}</td>"
                    f"<td>{a_badge}</td></tr>"
                )
            parts.append("</table>")
    else:
        parts.append(
            f'<p class="fail">API unreachable: '
            f'{_esc(api_status.get("error", "unknown"))}</p>'
        )
    parts.append("</div>")

    # --- Vivint Sensor Batteries ---
    low_batt: list[dict] = [b for b in batteries if b["low"]]
    if batteries:
        parts.append('<div class="section"><h2>Vivint Sensor Batteries</h2>')
        if low_batt:
            parts.append(
                f'<p class="warn">{len(low_batt)} sensor(s) below '
                f'{BATTERY_WARNING_PCT}%</p>'
            )
        parts.append(
            "<table><tr><th>Sensor</th><th>Type</th>"
            "<th>Battery</th></tr>"
        )
        for b in batteries:
            cls: str = "fail" if b["battery"] < 25 else (
                "warn" if b["low"] else "ok"
            )
            parts.append(
                f"<tr><td>{_esc(b['name'])}</td>"
                f"<td class='muted'>{_esc(b['type'])}</td>"
                f'<td class="{cls}">{b["battery"]}%</td></tr>'
            )
        parts.append("</table></div>")

    # --- MQTT ---
    parts.append('<div class="section"><h2>MQTT</h2>')
    parts.append("<table><tr><th>Broker</th><th>Rate</th></tr>")
    for broker, (ok, rate) in mqtt_rates.items():
        if ok:
            parts.append(
                f"<tr><td>{_esc(broker)}</td>"
                f"<td>{rate:.1f} msg/sec</td></tr>"
            )
        else:
            parts.append(
                f"<tr><td>{_esc(broker)}</td>"
                f'<td class="fail">unreachable</td></tr>'
            )
    parts.append("</table></div>")

    # --- Git ---
    parts.append('<div class="section"><h2>Git Status</h2>')
    if git.get("reachable"):
        parts.append("<p><strong>Branches</strong></p>")
        parts.append(f"<pre>{_esc(git['branches'])}</pre>")
        parts.append("<p><strong>Recent commits</strong></p>")
        parts.append(f"<pre>{_esc(git['log'])}</pre>")
    else:
        parts.append(
            f'<p class="fail">NAS unreachable: '
            f'{_esc(git.get("error", "unknown"))}</p>'
        )
    parts.append("</div>")

    # --- Test Results ---
    parts.append('<div class="section"><h2>Test Suite</h2>')
    if tests.get("reachable"):
        parts.append(f"<pre>{_esc(tests['summary'])}</pre>")
    else:
        reason: str = tests.get("error", "dev Mac unreachable")
        parts.append(f'<p class="muted">Skipped: {_esc(reason)}</p>')
    parts.append("</div>")

    # --- Errors ---
    has_errors: bool = False
    for h in hosts:
        if h.get("errors"):
            has_errors = True
            break
    if has_errors:
        parts.append('<div class="section"><h2>Recent Errors</h2>')
        for h in hosts:
            errs: str = h.get("errors", "")
            if errs:
                parts.append(f"<p><strong>{_esc(h['name'])}</strong></p>")
                parts.append(f"<pre>{_esc(errs)}</pre>")
        parts.append("</div>")

    # --- Disk / Memory / ZFS detail ---
    parts.append('<div class="section"><h2>Disk &amp; Memory</h2>')
    for h in hosts:
        if not h["reachable"]:
            continue
        parts.append(f"<p><strong>{_esc(h['name'])}</strong></p>")
        detail: str = (f"Disk: {_esc(h.get('disk', ''))}\n"
                       f"Mem:  {_esc(h.get('memory', ''))}")
        zpool: str = h.get("zpool", "")
        if zpool:
            detail += f"\n\nZFS Pool:\n{_esc(zpool)}"
        cap: str = h.get("zpool_capacity", "")
        if cap:
            detail += f"\n{_esc(cap)}"
        parts.append(f"<pre>{detail}</pre>")
    parts.append("</div>")

    # --- Footer ---
    parts.append(
        '<div class="section" style="text-align:center">'
        f'<p class="muted">Generated by GlowUp Morning Report v{__version__}'
        f' on {socket.gethostname()}</p></div>'
    )

    # Assemble full HTML.
    body: str = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>{CSS}</style></head>
<body><div class="container">{body}</div></body></html>"""


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, config: dict) -> None:
    """Send an HTML email via Gmail SMTP with app password."""
    msg: MIMEMultipart = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["username"]
    msg["To"] = config["to"]

    # Plain-text fallback.
    plain: str = (
        "GlowUp Morning Report\n"
        "Open this email in an HTML-capable client for the full report."
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context: ssl.SSLContext = ssl.create_default_context()
    with smtplib.SMTP(
        config.get("smtp_host", SMTP_HOST),
        config.get("smtp_port", SMTP_PORT),
    ) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(config["username"], config["app_password"])
        server.send_message(msg)

    logger.info("Email sent to %s", config["to"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Collect all data and send the morning report."""
    now: datetime = datetime.now()
    logger.info("Morning report starting at %s", now.strftime("%H:%M:%S"))

    # Load configs.
    try:
        with open(EMAIL_CONFIG_PATH) as f:
            email_config: dict = json.load(f)
    except Exception as exc:
        logger.error("Cannot read email config %s: %s",
                     EMAIL_CONFIG_PATH, exc)
        sys.exit(1)

    # Read API token from server config.
    api_token: str = ""
    try:
        with open(SERVER_CONFIG_PATH) as f:
            server_cfg: dict = json.load(f)
        api_token = server_cfg.get("auth_token", "")
    except Exception as exc:
        logger.warning("Cannot read server config: %s", exc)

    # --- Collect fleet health ---
    logger.info("Collecting fleet health...")
    hosts: list[dict[str, Any]] = []
    for name, info in FLEET.items():
        logger.info("  %s", name)
        hosts.append(collect_host_health(name, info))

    # --- API status ---
    logger.info("Checking API status...")
    api_status: dict[str, Any] = collect_api_status(api_token)

    # --- Vivint locks + sensor batteries ---
    locks: list[dict[str, Any]] = []
    batteries: list[dict[str, Any]] = []
    if api_status.get("reachable") and api_status.get("data"):
        locks = collect_vivint_locks(api_status["data"])
        batteries = collect_vivint_batteries(api_status["data"])

    # --- MQTT rates ---
    logger.info("Sampling MQTT rates...")
    mqtt_rates: dict[str, tuple[bool, float]] = {}
    for name, info in FLEET.items():
        # Only check hosts that run mosquitto.
        svcs: list[str] = info.get("services", [])
        if "mosquitto" in svcs:
            ok, rate = collect_mqtt_rate(
                info["ip"], info["user"],
                is_local=info.get("local", False),
            )
            mqtt_rates[name] = (ok, rate)

    # --- Git status ---
    logger.info("Checking git status...")
    git: dict[str, Any] = collect_git_status()

    # --- Test suite ---
    logger.info("Running test suite (this may take several minutes)...")
    tests: dict[str, Any] = collect_test_results()

    # --- STT engine state on Daedalus ---
    logger.info("Checking Daedalus STT engine state...")
    stt_state: dict[str, Any] = collect_stt_state(
        FLEET["Daedalus (.191)"]["ip"],
        FLEET["Daedalus (.191)"]["user"],
    )

    # --- Fleet code drift ---
    logger.info("Checking fleet code drift...")
    drift: dict[str, Any] = collect_drift()

    # --- Render and send ---
    logger.info("Rendering report...")
    html: str = render_html(
        hosts, api_status, locks, batteries, mqtt_rates, git, tests,
        stt_state, now, drift=drift,
    )

    subject: str = (
        f"GlowUp Morning Report - "
        f"{now.strftime('%Y-%m-%d %H:%M %Z')}"
    )

    logger.info("Sending email...")
    try:
        send_email(subject, html, email_config)
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)
        sys.exit(1)

    logger.info("Morning report complete.")


if __name__ == "__main__":
    main()
