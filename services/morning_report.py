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
    "Daedalus (.191)": {
        "ip": "10.0.0.191",
        "user": "perrykivolowitz",
        "os": "macos",
        "role": "Mac Studio — voice coordinator, development",
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
         timeout: int = SSH_TIMEOUT) -> tuple[bool, str]:
    """Run a command on a remote host via SSH.

    Returns (success, output).  On failure, output contains the
    error message rather than raising.
    """
    try:
        result: subprocess.CompletedProcess = subprocess.run(
            [
                "ssh", "-n",
                "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                f"{user}@{host}", cmd,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        return (result.returncode == 0, result.stdout.strip())
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
    now: datetime,
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

    # --- Render and send ---
    logger.info("Rendering report...")
    html: str = render_html(
        hosts, api_status, locks, batteries, mqtt_rates, git, tests, now,
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
