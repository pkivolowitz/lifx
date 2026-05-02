#!/usr/bin/env python3
"""GlowUp installer — standalone and server, idempotent re-run.

Implements ``docs/BASIC.md`` §Installing for both flavors:

  * **Standalone** (macOS / Linux / Windows-via-install.ps1)
    Creates a per-user venv at ``~/.glowup/venv``, drops a launcher shim at
    ``~/.local/bin/glowup``, optionally appends the shim directory to the
    user's shell rc PATH, and seeds ``~/.glowup/{devices,groups}.json`` plus
    a ``README.md`` documenting their schemas.

  * **Server** (Linux only, Debian/Ubuntu derivatives in v1)
    Creates a dedicated ``glowup`` system user, splits config (``/etc/glowup``,
    read-only) from state (``/var/lib/glowup``, dashboard-writable), seeds an
    auth token, renders a systemd unit, and starts ``glowup-server``.

Re-running this script is the **upgrade** path: it re-syncs the venv against
the current ``requirements.txt``, re-renders the systemd unit if its template
changed, and leaves user data alone.

This file is the implementation of the spec in ``docs/BASIC.md``.  When the
two disagree, the docs are authoritative — open an issue rather than letting
code drift further.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License.  See LICENSE in the project root.

__version__ = "1.0"

import argparse
import datetime
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

# pwd is POSIX-only.  We only call its functions on Linux, but importing it
# at module top would crash on Windows.  install.py rejects Windows in
# select_mode() before any pwd lookup happens, so guarding the import here
# keeps ``import install`` viable on Windows even if the install flow isn't.
try:
    import pwd
    _HAS_PWD = True
except ImportError:  # pragma: no cover — Windows path
    _HAS_PWD = False

# ---------------------------------------------------------------------------
# Named constants — no magic numbers (see CLAUDE.md / rules.md coding standards)
# ---------------------------------------------------------------------------

PYTHON_MIN_MAJOR: int = 3
PYTHON_MIN_MINOR: int = 11
# BASIC.md §What You Need — Python 3.11 floor.

DEFAULT_PORT: int = 8420
# BASIC.md §Server / Using The Dashboard — default HTTP port.

SCHEMA_VERSION: int = 1
# Bumped only when on-disk JSON file shapes change incompatibly.

AUTH_TOKEN_BYTES: int = 32
# 32 random bytes → 43-char URL-safe base64 token; ~256 bits of entropy.

PLACEHOLDER_GROUP_IP: str = "192.0.2.1"
# RFC 5737 TEST-NET-1 — guaranteed unreachable.  Lets the server boot with
# a non-empty groups dict before the operator populates real bulbs.

GLOWUP_USER_NAME: str = "glowup"
GLOWUP_GROUP_NAME: str = "glowup"
# Dedicated system user/group for the server (BASIC.md §What It Costs).

ETC_DIR: Path = Path("/etc/glowup")
VAR_LIB_DIR: Path = Path("/var/lib/glowup")
SYSTEMD_UNIT_PATH: Path = Path("/etc/systemd/system/glowup-server.service")
# FHS-canonical locations (BASIC.md §Server / Installing).

OPT_DIR: Path = Path("/opt/glowup")
RUNTIME_REPO_DIR: Path = OPT_DIR / "repo"
# The server runs as the unprivileged ``glowup`` system user with
# ``ProtectHome=true`` in its systemd unit, which makes /home invisible
# to the service regardless of filesystem permissions.  Running directly
# from the user's clone (`~/glowup`) is therefore impossible — the
# service would 200/CHDIR out instantly.  install.py syncs the clone
# tree to RUNTIME_REPO_DIR (root-owned, glowup-readable, mode 0750)
# and the systemd unit's WorkingDirectory + ExecStart point there.
# Re-running install.sh after `git pull` re-syncs, which is the
# upgrade path the BASIC.md "re-run = upgrade" promise documents.

# --- --no-prompt fallback coordinates ---
#
# When ``--no-prompt`` is set we can't ask the operator for lat/lon, so
# we have to write SOMETHING into /etc/glowup/site.json so the server
# can boot.  The choice matters: 0.0/0.0 (off the African coast) reads
# as "installer bug — broken default".  A real-but-deliberately-wrong
# place reads as "the installer chose this for you, you should change
# it".  We pick the Royal Observatory at Greenwich (51.4778°N, 0.0°)
# because:
#   - It IS the world coordinate reference (prime meridian + IERS
#     reference latitude); a meaningful default rather than arbitrary.
#   - Open-Meteo serves real weather there; NWS 404s (UK is outside
#     its coverage) and the executor's NWS→Open-Meteo fallback handles
#     that path cleanly.
#   - Distinctively not where any household operator lives, so the
#     dashboard's "London weather" reading is an immediate signal
#     to fix /etc/glowup/site.json.
NO_PROMPT_LAT: float = 51.4778
NO_PROMPT_LON: float = 0.0
NO_PROMPT_PLACE_LABEL: str = "Greenwich, UK (Royal Observatory)"

DEVICES_JSON: str = "devices.json"
GROUPS_JSON: str = "groups.json"
SCHEDULES_JSON: str = "schedules.json"
SERVER_JSON: str = "server.json"
SITE_JSON: str = "site.json"
STATE_DB: str = "state.db"
README_MD: str = "README.md"

SUPPORTED_LINUX_IDS: tuple[str, ...] = ("debian", "ubuntu", "raspbian")
# Debian/Ubuntu derivatives only for v1 (BASIC.md §What It Costs).

SHELL_RC_MAP: dict[str, tuple[str, ...]] = {
    "zsh": (".zshrc", ".zprofile"),
    "bash": (".bashrc", ".bash_profile", ".profile"),
    "fish": (".config/fish/config.fish",),
}
# Per-shell rc-file candidates, most-likely-to-be-sourced first.  We edit
# only the first existing file in the list (idempotent — see edit_shell_rc).

MANAGED_DOTFILE_MARKERS: tuple[str, ...] = (
    "# managed by chezmoi",
    "chezmoi:source",
    "managed by yadm",
    "# stowed",
    "managed by dotbot",
)
# Heuristic: if any marker appears in the user's rc file, we don't edit it
# automatically — the user would lose our edit on the next dotfile sync.

PATH_EDIT_MARKER: str = "# >>> GlowUp PATH (added by install.py) >>>"
PATH_EDIT_END_MARKER: str = "# <<< GlowUp PATH <<<"
# Idempotency: we grep for the marker before appending; re-running install.py
# never duplicates the export line.


class Mode(Enum):
    """Installation flavor selected from CLI flags or interactive prompt."""

    STANDALONE = "standalone"
    SERVER = "server"


class Color:
    """ANSI escape sequences for terminal output.

    Disabled (empty strings) when stdout is not a TTY so log captures and
    pipelines don't accumulate escape codes.
    """

    _ENABLED = sys.stdout.isatty()
    RESET = "\033[0m" if _ENABLED else ""
    BOLD = "\033[1m" if _ENABLED else ""
    DIM = "\033[2m" if _ENABLED else ""
    RED = "\033[31m" if _ENABLED else ""
    GREEN = "\033[32m" if _ENABLED else ""
    YELLOW = "\033[33m" if _ENABLED else ""
    BLUE = "\033[34m" if _ENABLED else ""
    CYAN = "\033[36m" if _ENABLED else ""


# ---------------------------------------------------------------------------
# Logging — one helper per severity, every output line goes through these so
# we have a single place to audit and one place to colorize.
# ---------------------------------------------------------------------------


def info(msg: str) -> None:
    """Print an informational message (white)."""
    print(f"  {msg}")


def step(msg: str) -> None:
    """Print a stage header (bold cyan).  Used for high-level progress."""
    print(f"\n{Color.BOLD}{Color.CYAN}==>{Color.RESET} {Color.BOLD}{msg}{Color.RESET}")


def ok(msg: str) -> None:
    """Print a success message (green check)."""
    print(f"  {Color.GREEN}✓{Color.RESET} {msg}")


def warn(msg: str) -> None:
    """Print a non-fatal warning (yellow)."""
    print(f"  {Color.YELLOW}!{Color.RESET} {msg}", file=sys.stderr)


def fail(msg: str, exit_code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    """Print an error and exit.

    Exit codes are documented inline at each call site; callers should pick
    distinct codes so CI / scripts can branch on them.
    """
    print(f"\n  {Color.RED}✗{Color.RESET} {msg}\n", file=sys.stderr)
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostInfo:
    """Snapshot of the host we're installing on.

    Captured once at the top of main() and passed down so every helper sees
    the same view of the world (no repeated platform.* calls drifting on us
    mid-install).
    """

    system: str        # "Darwin", "Linux", "Windows"
    is_macos: bool
    is_linux: bool
    is_windows: bool
    distro_id: Optional[str]      # /etc/os-release ID (lowercased), Linux only
    distro_id_like: Optional[str] # /etc/os-release ID_LIKE (lowercased), Linux only
    user: str          # SUDO_USER if set, else current user
    user_home: Path    # The invoking user's $HOME (not root's)
    clone_dir: Path    # Directory containing this install.py


def detect_host() -> HostInfo:
    """Inspect the running system and return a HostInfo.

    Resolves SUDO_USER / SUDO_HOME so a sudo'd run still seeds the *user's*
    ``~/.glowup`` directory, not root's.  This is critical for the server
    install: we want sudo for /etc and /var/lib writes, but the per-user
    standalone-style files (if any) belong to the human's home.
    """
    system = platform.system()
    is_macos = system == "Darwin"
    is_linux = system == "Linux"
    is_windows = system == "Windows"

    distro_id: Optional[str] = None
    distro_id_like: Optional[str] = None
    if is_linux:
        distro_id, distro_id_like = _read_os_release()

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and _HAS_PWD:
        # Resolve the original user's home via getpwnam — SUDO_HOME isn't
        # set on every system, but SUDO_USER is.
        try:
            user_home = Path(pwd.getpwnam(sudo_user).pw_dir)
            user = sudo_user
        except KeyError:
            user = sudo_user
            user_home = Path.home()
    else:
        user = os.environ.get("USER", "")
        user_home = Path.home()

    clone_dir = Path(__file__).resolve().parent

    return HostInfo(
        system=system,
        is_macos=is_macos,
        is_linux=is_linux,
        is_windows=is_windows,
        distro_id=distro_id,
        distro_id_like=distro_id_like,
        user=user,
        user_home=user_home,
        clone_dir=clone_dir,
    )


def _read_os_release() -> tuple[Optional[str], Optional[str]]:
    """Parse /etc/os-release for ID and ID_LIKE fields.

    Returns ``(None, None)`` if the file is missing or malformed — the caller
    treats that as "unknown distro" and refuses to proceed with the server
    install.
    """
    osrel = Path("/etc/os-release")
    if not osrel.is_file():
        return (None, None)
    fields: dict[str, str] = {}
    try:
        for line in osrel.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return (None, None)
    return (fields.get("ID", "").lower() or None,
            fields.get("ID_LIKE", "").lower() or None)


def is_supported_linux(host: HostInfo) -> bool:
    """True if /etc/os-release identifies a Debian/Ubuntu derivative.

    Checks both ID and ID_LIKE; Raspbian sets ID=raspbian ID_LIKE=debian, Pop!_OS
    sets ID=pop ID_LIKE=ubuntu, and so on.
    """
    if not host.is_linux:
        return False
    if host.distro_id and host.distro_id in SUPPORTED_LINUX_IDS:
        return True
    if host.distro_id_like:
        for tok in host.distro_id_like.split():
            if tok in SUPPORTED_LINUX_IDS:
                return True
    return False


# ---------------------------------------------------------------------------
# Subprocess wrappers — we centralize Popen/run so failure modes are uniform.
# ---------------------------------------------------------------------------


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    env: Optional[dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with our standard flags.

    ``check=True`` (default) raises on non-zero exit, with the stderr captured
    so callers can present a meaningful error.  ``capture=True`` returns
    stdout/stderr as text (UTF-8); otherwise they stream to the user's
    terminal.
    """
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=capture,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def run_sudo(cmd: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run ``sudo <cmd>``.  We always invoke sudo explicitly rather than
    relying on the script being run as root — that way the standalone code
    paths stay sudo-free."""
    return run(["sudo", *cmd], capture=capture)


def have_command(name: str) -> bool:
    """True if ``name`` is on PATH."""
    return shutil.which(name) is not None


# ---------------------------------------------------------------------------
# Python version check (defense in depth — install.sh bootstrap already
# verified this, but install.py may be invoked directly during dev).
# ---------------------------------------------------------------------------


def assert_python_version() -> None:
    """Refuse to run on Python older than the documented floor."""
    major, minor = sys.version_info[:2]
    if (major, minor) < (PYTHON_MIN_MAJOR, PYTHON_MIN_MINOR):
        fail(
            f"GlowUp requires Python {PYTHON_MIN_MAJOR}.{PYTHON_MIN_MINOR} or newer; "
            f"this interpreter is {major}.{minor}.  "
            f"Install a newer Python and re-run.",
            exit_code=10,
        )


# ---------------------------------------------------------------------------
# Venv management — per-user at ~/.glowup/venv (BASIC.md §Installing).
# Re-run is the upgrade path: same Python version → pip install -U; different
# Python version → backup the old venv to ~/.glowup/venv.bak.<ts>, recreate.
# ---------------------------------------------------------------------------


def glowup_home(host: HostInfo) -> Path:
    """The per-user GlowUp directory (``~/.glowup``)."""
    return host.user_home / ".glowup"


def venv_path(host: HostInfo) -> Path:
    """The per-user venv (``~/.glowup/venv``)."""
    return glowup_home(host) / "venv"


def venv_python(host: HostInfo) -> Path:
    """Path to the venv's python3 interpreter (POSIX only — Windows handled
    by install.ps1)."""
    return venv_path(host) / "bin" / "python3"


def ensure_venv(host: HostInfo) -> None:
    """Create or refresh the per-user venv.

    Idempotent.  If a venv exists at the canonical path, compares its Python
    version against the running interpreter; matches → reuse, mismatches →
    rename the old venv with a timestamp suffix and rebuild.
    """
    home = glowup_home(host)
    home.mkdir(parents=True, exist_ok=True)
    target = venv_path(host)

    if target.exists():
        if _venv_python_matches(target):
            ok(f"venv at {target} matches running Python; will pip install -U")
            return
        backup = target.with_name(target.name + f".bak.{_timestamp()}")
        warn(f"existing venv at {target} uses a different Python; "
             f"renaming to {backup} and rebuilding")
        target.rename(backup)

    step(f"creating venv at {target}")
    run([sys.executable, "-m", "venv", str(target)])
    ok(f"venv created (Python {sys.version_info.major}.{sys.version_info.minor})")


def _venv_python_matches(venv: Path) -> bool:
    """True if ``<venv>/bin/python3`` reports the same major.minor we're running."""
    py = venv / "bin" / "python3"
    if not py.is_file() and not py.is_symlink():
        return False
    try:
        proc = run(
            [str(py), "-c",
             "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture=True,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    expected = f"{sys.version_info.major}.{sys.version_info.minor}"
    return proc.stdout.strip() == expected


def _timestamp() -> str:
    """Compact timestamp suitable for backup-file names."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def install_requirements(host: HostInfo) -> None:
    """Install the core requirements into the per-user venv.

    Always runs ``pip install -U`` so re-runs pick up changed pins.  Uses
    the venv's pip directly (no activation needed) and pipes its output
    through to the terminal so the user sees what's happening.
    """
    req = host.clone_dir / "requirements.txt"
    if not req.is_file():
        fail(f"requirements.txt not found at {req}", exit_code=20)

    step(f"installing requirements from {req.name}")
    pip = venv_path(host) / "bin" / "pip"
    run([str(pip), "install", "--upgrade", "pip"])
    run([str(pip), "install", "--upgrade", "-r", str(req)])
    ok("requirements installed")


# ---------------------------------------------------------------------------
# Shim — ~/.local/bin/glowup invokes the venv's Python on the clone's
# glowup.py.  Captures the clone path at install time; re-running install.py
# re-renders the shim with the current clone location.
# ---------------------------------------------------------------------------


SHIM_TEMPLATE: str = """#!/usr/bin/env bash
# GlowUp launcher — auto-generated by install.py.
# Invokes the per-user venv's Python on the cloned glowup.py.
# To regenerate (e.g. after moving the clone): re-run ./install.sh.
exec {venv_python} {entry_point} "$@"
"""


def shim_dir(host: HostInfo) -> Path:
    """``~/.local/bin`` — XDG-conventional per-user binary directory."""
    return host.user_home / ".local" / "bin"


def shim_path(host: HostInfo) -> Path:
    """``~/.local/bin/glowup``."""
    return shim_dir(host) / "glowup"


def write_shim(host: HostInfo) -> None:
    """Render the launcher shim with absolute paths and chmod +x.

    Idempotent.  Overwrites any existing shim, since the clone path or venv
    path may have changed since last install.
    """
    target = shim_path(host)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = host.clone_dir / "glowup.py"
    if not entry.is_file():
        fail(f"glowup.py entry point not found at {entry}", exit_code=21)

    body = SHIM_TEMPLATE.format(
        venv_python=str(venv_python(host)),
        entry_point=str(entry),
    )
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)
    ok(f"wrote launcher {target} → {entry.name}")


# ---------------------------------------------------------------------------
# Shell rc PATH edit — permission-gated, idempotent, backed-up, with
# managed-dotfile detection (chezmoi/yadm/stow/dotbot all want untouched
# rc files).  See BASIC.md §Standalone / Installing.
# ---------------------------------------------------------------------------


def detect_shell(host: HostInfo) -> Optional[str]:
    """Return ``"bash"``/``"zsh"``/``"fish"`` or None.

    Reads ``$SHELL`` and inspects the basename — that's what login shells
    use to decide which rc files they read.
    """
    shell_env = os.environ.get("SHELL", "")
    if not shell_env:
        return None
    base = Path(shell_env).name.lower()
    if base in ("zsh", "bash", "fish"):
        return base
    return None


def find_rc_file(host: HostInfo, shell: str) -> Optional[Path]:
    """Return the first existing rc file for the given shell, or None.

    Order is "most likely to be sourced for an interactive shell" — see
    SHELL_RC_MAP.  We only edit the *first existing* file; we do not
    create one (that would surprise the user).
    """
    candidates = SHELL_RC_MAP.get(shell, ())
    for rel in candidates:
        candidate = host.user_home / rel
        if candidate.is_file():
            return candidate
    return None


def is_managed_dotfile(rc: Path) -> bool:
    """Heuristic check for a managed-dotfile setup.

    If the rc file contains any well-known marker, we refuse to edit it —
    a chezmoi/yadm/stow re-sync would clobber our edit.  The user gets
    instructions to add the export line themselves.
    """
    try:
        head = rc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for marker in MANAGED_DOTFILE_MARKERS:
        if marker.lower() in head.lower():
            return True
    return False


def shim_already_on_path(host: HostInfo) -> bool:
    """True if the shim's directory is already on ``$PATH``.

    Resolves both directories so a symlinked PATH entry still matches.
    """
    target = shim_dir(host).resolve()
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if not p:
            continue
        try:
            if Path(p).resolve() == target:
                return True
        except OSError:
            continue
    return False


def export_block(shell: str, shim_dir_str: str) -> str:
    """The block we append to the user's rc file.

    Wrapped in marker comments so re-running install.py can detect and skip
    re-appending.  Fish syntax differs from bash/zsh.
    """
    if shell == "fish":
        line = f"set -gx PATH {shim_dir_str} $PATH"
    else:
        line = f'export PATH="{shim_dir_str}:$PATH"'
    return f"\n{PATH_EDIT_MARKER}\n{line}\n{PATH_EDIT_END_MARKER}\n"


def edit_shell_rc(host: HostInfo, *, assume_yes: bool) -> None:
    """Optionally append a PATH export to the user's rc file.

    Asks permission unless ``assume_yes`` is True (driven by ``--no-prompt``).
    Backs up the rc file first.  Skips if the marker is already present
    (idempotent), if the shim dir is already on PATH (no work), or if the
    rc file looks managed (chezmoi etc.).
    """
    if shim_already_on_path(host):
        ok(f"{shim_dir(host)} already on PATH; no rc edit needed")
        return

    shell = detect_shell(host)
    if not shell:
        warn("could not identify your shell from $SHELL; skipping PATH edit. "
             f"Add {shim_dir(host)} to PATH manually.")
        return

    rc = find_rc_file(host, shell)
    if rc is None:
        warn(f"no {shell} rc file found under {host.user_home}; "
             f"add {shim_dir(host)} to PATH manually.")
        return

    if is_managed_dotfile(rc):
        warn(f"{rc} looks managed (chezmoi/yadm/stow markers found). "
             f"Add the following to your dotfiles instead:\n"
             f"    {export_block(shell, str(shim_dir(host))).strip()}")
        return

    try:
        existing = rc.read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"cannot read {rc}: {exc}; skipping PATH edit")
        return

    if PATH_EDIT_MARKER in existing:
        ok(f"PATH edit already present in {rc}; no change")
        return

    if not assume_yes:
        info(f"GlowUp wants to add {shim_dir(host)} to your PATH by appending "
             f"to {rc}.")
        info(f"A backup will be saved at {rc}.bak.<timestamp> first.")
        if not _ask_yes_no("Permit this edit?", default=True):
            info(f"Skipping rc edit.  To finish setup, run:")
            info(f"    {export_block(shell, str(shim_dir(host))).strip()}")
            return

    backup = rc.with_name(rc.name + f".bak.{_timestamp()}")
    shutil.copy2(rc, backup)
    ok(f"backed up {rc} → {backup}")

    with rc.open("a", encoding="utf-8") as fh:
        fh.write(export_block(shell, str(shim_dir(host))))
    ok(f"appended PATH export to {rc} (open a new shell or `source {rc}`)")


def _ask_yes_no(prompt: str, *, default: bool) -> bool:
    """Yes/no prompt with a clear default.  Empty input = default."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            ans = input(f"  {prompt} {suffix} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        info("Please answer y or n.")


# ---------------------------------------------------------------------------
# Standalone seed files — devices.json, groups.json, README.md.
# ---------------------------------------------------------------------------


STANDALONE_README: str = """\
# GlowUp standalone — files in this directory

This directory holds your GlowUp standalone state.  Two JSON files plus
the venv directory are all that lives here.

## `devices.json` — bulb registry

Created and updated by `glowup name` and `glowup discover`.  Maps each
bulb's MAC address to its label, IP, and product info.

Schema:

```json
{
  "<MAC>": {
    "label": "<human name>",
    "ip": "<IPv4 address>",
    "product": "<LIFX product name>"
  }
}
```

Example (do **not** copy verbatim — your bulbs have different MACs and IPs):

```json
{
  "d0:73:d5:01:23:ab": {
    "label": "Kitchen Bulb",
    "ip": "192.168.1.41",
    "product": "A19"
  }
}
```

## `groups.json` — group definitions

Created and updated by `glowup group add` / `glowup group rm`.  Maps each
group name to an ordered list of bulb references (label, MAC, or IP).

Schema:

```json
{
  "<group_name>": ["<bulb ref>", "<bulb ref>", ...]
}
```

Order matters — the first bulb is the leftmost zone of the virtual strip.

## Editing by hand

Both files are plain JSON.  You can open them in any editor, but the
runtime preserves keys starting with `_` on read and never writes new
ones.  That gives you a stable place for your own notes:

```json
{
  "_note": "PORCH STRING is the long one over the bench",
  "porch": ["String 36 Porch", "Bulb Patio"]
}
```

## venv

`venv/` is GlowUp's Python virtual environment.  The launcher at
`~/.local/bin/glowup` invokes it directly.  Don't activate it manually
unless you're debugging — the launcher does the right thing.
"""


def seed_standalone_files(host: HostInfo) -> None:
    """Drop devices.json, groups.json, README.md into ``~/.glowup`` (only if missing).

    Existing files are left untouched — re-runs of install.py never
    clobber user-edited JSON.  The README is overwritten on every install
    so doc updates land.
    """
    home = glowup_home(host)
    home.mkdir(parents=True, exist_ok=True)

    for name in (DEVICES_JSON, GROUPS_JSON):
        target = home / name
        if target.exists():
            ok(f"{target} exists; leaving alone")
            continue
        target.write_text("{}\n", encoding="utf-8")
        target.chmod(0o644)
        ok(f"seeded empty {target}")

    readme = home / README_MD
    readme.write_text(STANDALONE_README, encoding="utf-8")
    readme.chmod(0o644)
    ok(f"wrote {readme}")


# ---------------------------------------------------------------------------
# Server install — Linux only.
# ---------------------------------------------------------------------------


SERVER_README: str = """\
# GlowUp server state — files in this directory

`/var/lib/glowup/` holds the writable state for `glowup-server`.  Read-only
install-time config (port, auth token, latitude/longitude) lives next door
in `/etc/glowup/`.

## `devices.json` — bulb registry

Maintained by the server; the dashboard and `glowup name` write to it.
Same schema as standalone:

```json
{
  "<MAC>": {
    "label": "<human name>",
    "ip": "<IPv4 address>",
    "product": "<LIFX product name>"
  }
}
```

Migrate from a standalone install with:

```
sudo install -o glowup -g glowup -m 0640 \\
    ~/.glowup/devices.json /var/lib/glowup/devices.json
sudo systemctl restart glowup-server
```

## `groups.json` — group definitions

Maps group name → ordered list of bulb references (label, MAC, or IP).
The installer seeds a single placeholder group that points at
`192.0.2.1` (RFC 5737 TEST-NET-1, never reachable) so the server can
start before you populate real bulbs.

Replace the placeholder via the dashboard or
`glowup group add <name> <bulb> ...` once you've discovered your bulbs.

## `schedules.json` — schedule entries

Maintained by the server.  Each entry is `{name, group, start, stop,
effect, params}`.  Times can be wall-clock (`07:00`) or symbolic
(`sunset-30m`).  Symbolic times require latitude/longitude in
`/etc/glowup/site.json`.

## Editing by hand

Same `_`-prefixed-keys-are-preserved rule as standalone.  Server is
running while you edit — restart with `sudo systemctl restart
glowup-server` to pick up changes you made by hand (the dashboard
writes don't need a restart).
"""


def require_sudo() -> None:
    """Re-exec under sudo if we don't have root.

    The standalone path never reaches this; only the server install asks
    for it.  We re-exec rather than failing so the user gets one password
    prompt and the rest of the install just runs.
    """
    if os.geteuid() == 0:
        return
    info("server install needs sudo; re-running under sudo (you may be prompted for a password)")
    os.execvp("sudo", ["sudo", "-E", sys.executable, *sys.argv])


def ensure_glowup_user() -> None:
    """Create the dedicated ``glowup`` system user/group if missing.

    Uses ``adduser --system`` (Debian-family).  Idempotent: if the user
    exists, ``adduser`` is a no-op and returns 0.
    """
    if _HAS_PWD:
        try:
            pwd.getpwnam(GLOWUP_USER_NAME)
            ok(f"system user {GLOWUP_USER_NAME!r} already exists")
            return
        except KeyError:
            pass

    step(f"creating system user {GLOWUP_USER_NAME!r}")
    run_sudo([
        "adduser", "--system", "--group",
        "--no-create-home",
        "--home", str(VAR_LIB_DIR),
        "--shell", "/usr/sbin/nologin",
        GLOWUP_USER_NAME,
    ])
    ok(f"created {GLOWUP_USER_NAME}:{GLOWUP_GROUP_NAME}")


def ensure_etc_dir() -> None:
    """Create ``/etc/glowup`` (root-owned, 0750, group=glowup)."""
    if not ETC_DIR.is_dir():
        run_sudo(["install", "-d", "-m", "0750",
                  "-o", "root", "-g", GLOWUP_GROUP_NAME, str(ETC_DIR)])
        ok(f"created {ETC_DIR}")
    else:
        ok(f"{ETC_DIR} exists")


def ensure_var_lib_dir() -> None:
    """Create ``/var/lib/glowup`` (glowup-owned, 0750)."""
    if not VAR_LIB_DIR.is_dir():
        run_sudo(["install", "-d", "-m", "0750",
                  "-o", GLOWUP_USER_NAME, "-g", GLOWUP_GROUP_NAME, str(VAR_LIB_DIR)])
        ok(f"created {VAR_LIB_DIR}")
    else:
        ok(f"{VAR_LIB_DIR} exists")


def write_etc_json(target: Path, payload: dict, *, mode: str = "0640") -> None:
    """Write a JSON file under /etc/glowup atomically and set ownership.

    Atomic = write to ``<target>.tmp`` then rename, so we never leave a
    half-written config behind.  Ownership is root:glowup so the daemon
    can read but not write.
    """
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    # Use sudo tee since we can't open root-owned files directly without root.
    proc = subprocess.run(
        ["sudo", "tee", str(tmp)],
        input=body, text=True, capture_output=True, check=True,
    )
    del proc  # tee echoes input; we discard it.
    run_sudo(["chown", f"root:{GLOWUP_GROUP_NAME}", str(tmp)])
    run_sudo(["chmod", mode, str(tmp)])
    run_sudo(["mv", str(tmp), str(target)])
    ok(f"wrote {target}")


def write_var_lib_json(target: Path, payload: dict, *, mode: str = "0640") -> None:
    """Write a JSON file under /var/lib/glowup, owned by the service user."""
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    subprocess.run(
        ["sudo", "tee", str(tmp)],
        input=body, text=True, capture_output=True, check=True,
    )
    run_sudo(["chown", f"{GLOWUP_USER_NAME}:{GLOWUP_GROUP_NAME}", str(tmp)])
    run_sudo(["chmod", mode, str(tmp)])
    run_sudo(["mv", str(tmp), str(target)])
    ok(f"wrote {target}")


def generate_auth_token() -> str:
    """Cryptographically random URL-safe token (43 chars from 32 bytes)."""
    return secrets.token_urlsafe(AUTH_TOKEN_BYTES)


def seed_server_config(host: HostInfo, *, assume_yes: bool) -> None:
    """Seed /etc/glowup/{site,server}.json (read-only) and /var/lib/glowup/*
    (state).  Existing files are preserved; only README.md is overwritten on
    every install."""
    # /etc/glowup/server.json — port + auth_token + state-file pointers.
    # The state-file split (BASIC.md §Server / Installing) keeps
    # server.json read-only and pushes runtime data to /var/lib/glowup.
    # ``groups_file`` and ``schedule_file`` tell server.py where the
    # canonical registry and schedule live; the dashboard's PUT/POST
    # group + schedule handlers route writes there too (see
    # handlers/dashboard.py:_save_config_field).  ``state_file``
    # routes the SQLite state store (DeviceManager, LockManager,
    # occupancy operator) into the writable state directory rather
    # than the read-only /etc tree where SQLite's first connect
    # fails under ProtectHome=true.  The DB file itself is created
    # on demand by SQLite — the installer only needs to publish the
    # path; ensuring /var/lib/glowup exists with mode 0750 owned by
    # glowup is enough.
    server_json = ETC_DIR / SERVER_JSON
    if not server_json.is_file():
        token = generate_auth_token()
        write_etc_json(server_json, {
            "schema_version": SCHEMA_VERSION,
            "port": DEFAULT_PORT,
            "auth_token": token,
            "groups_file": str(VAR_LIB_DIR / GROUPS_JSON),
            "schedule_file": str(VAR_LIB_DIR / SCHEDULES_JSON),
            "state_file": str(VAR_LIB_DIR / STATE_DB),
            "device_registry_file": str(VAR_LIB_DIR / DEVICES_JSON),
        })
    else:
        ok(f"{server_json} exists; leaving alone")

    # /etc/glowup/site.json — install id + lat/lon (prompt or default).
    site_json = ETC_DIR / SITE_JSON
    if not site_json.is_file():
        lat, lon = _prompt_lat_lon(assume_yes=assume_yes)
        write_etc_json(site_json, {
            "schema_version": SCHEMA_VERSION,
            "install_id": secrets.token_hex(8),
            "latitude": lat,
            "longitude": lon,
            "timezone": _detect_timezone(),
        })
    else:
        ok(f"{site_json} exists; leaving alone")

    # /var/lib/glowup/devices.json — empty registry.
    # Schema is ``{"devices": {<MAC>: {<entry>}}}`` — DeviceRegistry.load
    # tolerates a bare ``{}`` (the old shape) via ``raw.get("devices", {})``
    # but the explicit wrapper makes the schema visible to operators
    # who open the file before the first registration.
    devices_json = VAR_LIB_DIR / DEVICES_JSON
    if not devices_json.is_file():
        write_var_lib_json(devices_json, {"devices": {}})
    else:
        ok(f"{devices_json} exists; leaving alone")

    # /var/lib/glowup/groups.json — placeholder so server can start.
    groups_json = VAR_LIB_DIR / GROUPS_JSON
    if not groups_json.is_file():
        write_var_lib_json(groups_json, {
            "_comment": (
                "Placeholder group pointing at RFC 5737 TEST-NET-1 (unreachable). "
                "Replace with real bulbs via the dashboard or `glowup group add`."
            ),
            "placeholder": [PLACEHOLDER_GROUP_IP],
        })
    else:
        ok(f"{groups_json} exists; leaving alone")

    # /var/lib/glowup/schedules.json — empty schedule wrapper.
    # server.py's schedule_file consumer expects a top-level dict with
    # ``schedule`` (and optional ``location``) keys; a bare list would
    # KeyError on ``sched_config["schedule"]``.  Write the canonical
    # shape so the server boots immediately on a fresh install.
    schedules_json = VAR_LIB_DIR / SCHEDULES_JSON
    if not schedules_json.is_file():
        write_var_lib_json(schedules_json, {"schedule": []})
    else:
        ok(f"{schedules_json} exists; leaving alone")

    # /var/lib/glowup/README.md — overwritten on every install (doc updates).
    readme = VAR_LIB_DIR / README_MD
    tmp = readme.with_suffix(".md.tmp")
    subprocess.run(
        ["sudo", "tee", str(tmp)],
        input=SERVER_README, text=True, capture_output=True, check=True,
    )
    run_sudo(["chown", f"{GLOWUP_USER_NAME}:{GLOWUP_GROUP_NAME}", str(tmp)])
    run_sudo(["chmod", "0644", str(tmp)])
    run_sudo(["mv", str(tmp), str(readme)])
    ok(f"wrote {readme}")


def _prompt_lat_lon(*, assume_yes: bool) -> tuple[float, float]:
    """Ask for latitude and longitude (decimal degrees).

    In ``--no-prompt`` mode, defaults to ``NO_PROMPT_LAT/LON``
    (Greenwich Royal Observatory; see the constant's comment for why
    that point and not 0.0/0.0).  Operator edits /etc/glowup/site.json
    after install to point at their real location.  Symbolic schedule
    times (sunset-30m, etc.) need real values; until then the server
    boots and serves the dashboard happily, just with weather pulled
    for the wrong city.
    """
    if assume_yes:
        warn(
            f"--no-prompt set; latitude/longitude default to "
            f"{NO_PROMPT_LAT},{NO_PROMPT_LON} ({NO_PROMPT_PLACE_LABEL}). "
            f"Edit /etc/glowup/site.json before using "
            f"sunrise/sunset schedules."
        )
        return (NO_PROMPT_LAT, NO_PROMPT_LON)
    info("Latitude/longitude lets the server compute sunrise and sunset for "
         "your location.  Decimal degrees, four or five digits is plenty. "
         f"Press Enter to leave both at {NO_PROMPT_PLACE_LABEL} "
         f"({NO_PROMPT_LAT}, {NO_PROMPT_LON}) and edit later.")
    return (_prompt_float("Latitude (decimal degrees)", NO_PROMPT_LAT),
            _prompt_float("Longitude (decimal degrees)", NO_PROMPT_LON))


def _prompt_float(label: str, default: float) -> float:
    """Read a float with a default.  Empty input = default; bad input → re-prompt."""
    while True:
        try:
            raw = input(f"  {label} [{default}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            info("Not a number — try again, or press Enter for the default.")


def _detect_timezone() -> str:
    """Best-effort timezone detection.

    Reads /etc/timezone if present, falls back to readlink /etc/localtime
    (common on Debian/Ubuntu), and finally returns ``UTC`` if neither
    works.  We never fail the install over a missing tz string.
    """
    tz_file = Path("/etc/timezone")
    if tz_file.is_file():
        try:
            tz = tz_file.read_text(encoding="utf-8").strip()
            if tz:
                return tz
        except OSError:
            pass
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        try:
            target = os.readlink(str(localtime))
            # Typical: ../usr/share/zoneinfo/America/Chicago
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    return "UTC"


# ---------------------------------------------------------------------------
# systemd unit — rendered fresh on every server install/upgrade.
# ---------------------------------------------------------------------------


SYSTEMD_UNIT_TEMPLATE: str = """\
[Unit]
Description=GlowUp REST API Server
Documentation=file://{clone_dir}/docs/BASIC.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={clone_dir}
ExecStart={venv_python} {entry_point}
Restart=on-failure
RestartSec=5
# StateDirectory creates /var/lib/glowup if missing and ensures it's
# owned by the service User+Group at start.  We don't use
# ConfigurationDirectory because /etc/glowup ownership is root:glowup
# (read-only to the service), not glowup:glowup.
StateDirectory=glowup
StandardOutput=journal
StandardError=journal
SyslogIdentifier=glowup-server
Environment=PYTHONUNBUFFERED=1

# Hardening — see systemd.exec(5).
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
# /etc/glowup remains read-only via ProtectSystem=strict; the service
# writes only to /var/lib/glowup, granted by StateDirectory above.
ReadWritePaths={var_lib_dir}

[Install]
WantedBy=multi-user.target
"""


def sync_runtime_repo(host: HostInfo) -> None:
    """Mirror the user's clone tree to ``RUNTIME_REPO_DIR``.

    The systemd service runs as the unprivileged ``glowup`` system
    user with ``ProtectHome=true``; it cannot read the user's
    ``$HOME``/glowup at all.  Mirror the source tree to
    ``/opt/glowup/repo`` (root-owned, glowup-readable, mode 0750)
    so the service has a discoverable WorkingDirectory + ExecStart
    that lives outside the protected /home tree.

    Re-running install.sh after a ``git pull`` re-syncs this mirror,
    which is the upgrade path BASIC.md promises ("Re-running
    ./install.sh after a git pull is the upgrade path").  ``--delete``
    on the rsync ensures files removed upstream don't linger in the
    mirror.

    Excludes ``.git`` (the mirror is a runtime artifact, not a
    development checkout — shaving git history saves ~30 MB) and
    ``__pycache__`` (stale .pyc files from a prior Python version
    can bite mid-upgrade).
    """
    if not host.clone_dir.is_dir():
        fail(
            f"clone directory not found at {host.clone_dir}; "
            "install.sh must be run from inside the cloned glowup repo.",
            exit_code=24,
        )
    if not have_command("rsync"):
        fail(
            "rsync is required for the server install (mirrors the "
            "user clone to /opt/glowup/repo so the unprivileged "
            "glowup service can read it).  Install with: "
            "sudo apt-get install -y rsync",
            exit_code=25,
        )
    # Ensure /opt/glowup exists (created with mode 0755 root:glowup
    # in ensure_server_venv too — defensive in case ordering changes).
    if not OPT_DIR.is_dir():
        run_sudo([
            "install", "-d", "-m", "0755",
            "-o", "root", "-g", GLOWUP_GROUP_NAME, str(OPT_DIR),
        ])
    if not RUNTIME_REPO_DIR.is_dir():
        run_sudo([
            "install", "-d", "-m", "0750",
            "-o", "root", "-g", GLOWUP_GROUP_NAME, str(RUNTIME_REPO_DIR),
        ])
    # Trailing slash on the source = "copy contents", not "copy the
    # directory itself".  --delete drops files removed upstream;
    # excludes match the install-side gitignore plus a defensive
    # __pycache__ scrub.
    run_sudo([
        "rsync", "-a", "--delete",
        "--exclude=.git", "--exclude=__pycache__",
        "--exclude=*.pyc", "--exclude=.vscode",
        "--exclude=.mcp.json",
        str(host.clone_dir) + "/", str(RUNTIME_REPO_DIR) + "/",
    ])
    # Re-assert ownership in case rsync preserved a non-root uid from
    # the source tree (parallels:parallels in the test VM, real-user
    # ownership in the wild).  Mode stays 0750 on the dir; files
    # inherit rsync's preserved mode bits which are fine for source
    # files.
    run_sudo([
        "chown", "-R", f"root:{GLOWUP_GROUP_NAME}", str(RUNTIME_REPO_DIR),
    ])
    ok(f"mirrored {host.clone_dir} → {RUNTIME_REPO_DIR}")


def render_systemd_unit(host: HostInfo) -> bool:
    """Write the systemd unit, daemon-reload, return True if the file changed."""
    # WorkingDirectory + ExecStart point at RUNTIME_REPO_DIR (the
    # root-owned mirror written by sync_runtime_repo) rather than
    # host.clone_dir.  See RUNTIME_REPO_DIR's comment for why /opt is
    # the only viable home given ProtectHome=true on the service.
    server_venv = Path("/opt/glowup/venv")
    entry_point = RUNTIME_REPO_DIR / "server.py"
    if not entry_point.is_file():
        fail(f"server entry point not found at {entry_point}", exit_code=22)
    body = SYSTEMD_UNIT_TEMPLATE.format(
        clone_dir=RUNTIME_REPO_DIR,
        user=GLOWUP_USER_NAME,
        group=GLOWUP_GROUP_NAME,
        venv_python=server_venv / "bin" / "python3",
        entry_point=entry_point,
        var_lib_dir=VAR_LIB_DIR,
    )

    existing = ""
    if SYSTEMD_UNIT_PATH.is_file():
        try:
            existing = SYSTEMD_UNIT_PATH.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    if existing == body:
        ok(f"{SYSTEMD_UNIT_PATH} already current; no rewrite")
        return False

    tmp = SYSTEMD_UNIT_PATH.with_suffix(".service.tmp")
    subprocess.run(
        ["sudo", "tee", str(tmp)],
        input=body, text=True, capture_output=True, check=True,
    )
    run_sudo(["chmod", "0644", str(tmp)])
    run_sudo(["mv", str(tmp), str(SYSTEMD_UNIT_PATH)])
    ok(f"rendered {SYSTEMD_UNIT_PATH}")
    run_sudo(["systemctl", "daemon-reload"])
    return True


def ensure_server_venv(host: HostInfo) -> None:
    """Create /opt/glowup/venv (root-owned, glowup-readable) and install
    requirements into it.

    The server runs as the ``glowup`` user; we keep its venv under /opt
    rather than ~glowup so the FHS layout matches what other system
    services use.
    """
    opt_dir = Path("/opt/glowup")
    venv = opt_dir / "venv"
    if not opt_dir.is_dir():
        run_sudo(["install", "-d", "-m", "0755", "-o", "root", "-g", GLOWUP_GROUP_NAME, str(opt_dir)])
    if venv.exists():
        if _venv_python_matches(venv):
            ok(f"{venv} matches running Python; will pip install -U")
        else:
            backup = venv.with_name(venv.name + f".bak.{_timestamp()}")
            warn(f"existing server venv at {venv} uses a different Python; "
                 f"renaming to {backup} and rebuilding")
            run_sudo(["mv", str(venv), str(backup)])
    if not venv.exists():
        run_sudo([sys.executable, "-m", "venv", str(venv)])
        ok(f"created server venv at {venv}")

    pip = venv / "bin" / "pip"
    # requirements.txt comes from the runtime mirror written by
    # ``sync_runtime_repo`` (run earlier in run_server) — same code
    # the service will execute, no risk of host.clone_dir / mirror
    # drift between pip and runtime.
    req = RUNTIME_REPO_DIR / "requirements.txt"
    if not req.is_file():
        fail(
            f"requirements.txt not found at {req}; sync_runtime_repo "
            f"must run before ensure_server_venv.",
            exit_code=26,
        )
    run_sudo([str(pip), "install", "--upgrade", "pip"])
    run_sudo([str(pip), "install", "--upgrade", "-r", str(req)])
    ok("server requirements installed")


def start_server() -> None:
    """``systemctl enable --now glowup-server.service``."""
    run_sudo(["systemctl", "enable", "--now", "glowup-server.service"])
    ok("glowup-server enabled and started")


# ---------------------------------------------------------------------------
# Mode selection — flag → prompt fallback → mac-default → linux-default.
# ---------------------------------------------------------------------------


def select_mode(host: HostInfo, args: argparse.Namespace) -> Mode:
    """Resolve the install mode.

    Order: explicit flag wins; then macOS forces standalone (no server flavor
    on macOS); then Linux prompts unless ``--no-prompt`` is set.
    """
    if args.standalone and args.server:
        fail("--standalone and --server are mutually exclusive", exit_code=2)
    if args.standalone:
        return Mode.STANDALONE
    if args.server:
        if not host.is_linux:
            fail("--server is supported on Linux only "
                 "(server install creates systemd units, /etc/glowup, etc.)", exit_code=3)
        return Mode.SERVER

    if host.is_macos:
        return Mode.STANDALONE

    if host.is_windows:
        fail("install.py does not support Windows.  "
             "Use install.ps1 instead (see docs/BASIC.md §Standalone).", exit_code=4)

    if host.is_linux:
        if args.no_prompt:
            return Mode.SERVER
        return _prompt_linux_mode()

    fail(f"unsupported platform: {host.system}", exit_code=5)


def _prompt_linux_mode() -> Mode:
    """Linux interactive prompt: server (default) or standalone.

    Single keystroke: Enter or s/S → server; t/T → standalone.  We accept
    the words too in case the user types a full reply.
    """
    info("This Linux install can be:")
    info("  s  Server  — systemd unit, /etc/glowup, /var/lib/glowup, "
         "runs 24/7 (default)")
    info("  t  Standalone — per-user venv only, no daemon, no sudo")
    while True:
        try:
            ans = input(f"  Choose [s/t] (default s): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return Mode.SERVER
        if ans in ("", "s", "server"):
            return Mode.SERVER
        if ans in ("t", "standalone"):
            return Mode.STANDALONE
        info("Please answer s or t.")


# ---------------------------------------------------------------------------
# Top-level flows
# ---------------------------------------------------------------------------


def run_standalone(host: HostInfo, *, assume_yes: bool) -> None:
    """End-to-end standalone install (mac/Linux)."""
    step(f"GlowUp standalone install on {host.system}")
    ensure_venv(host)
    install_requirements(host)
    write_shim(host)
    edit_shell_rc(host, assume_yes=assume_yes)
    seed_standalone_files(host)

    print()
    step("standalone install complete")
    info(f"Run {Color.BOLD}glowup discover{Color.RESET} to find your bulbs.")
    info(f"If `glowup` is not yet on PATH, open a new shell or "
         f"`source` your shell rc file.")
    info(f"Files: {glowup_home(host)}")


def run_server(host: HostInfo, *, assume_yes: bool) -> None:
    """End-to-end server install (Linux only)."""
    if not is_supported_linux(host):
        fail(f"server install supports Debian/Ubuntu derivatives only "
             f"(detected ID={host.distro_id!r}, ID_LIKE={host.distro_id_like!r}). "
             f"See docs/BASIC.md §What It Costs.", exit_code=6)

    require_sudo()  # Re-execs if needed; below this line we are root.
    step(f"GlowUp server install on {host.distro_id}")

    ensure_glowup_user()
    ensure_etc_dir()
    ensure_var_lib_dir()
    # Mirror the user's clone to /opt/glowup/repo BEFORE we install
    # requirements (ensure_server_venv pip-installs from the mirror,
    # not the user's $HOME copy) and BEFORE we render the systemd
    # unit (its WorkingDirectory + ExecStart point at the mirror).
    sync_runtime_repo(host)
    ensure_server_venv(host)
    seed_server_config(host, assume_yes=assume_yes)
    unit_changed = render_systemd_unit(host)
    # Upgrade case: unit changed and service was already running → restart so
    # the new ExecStart / hardening settings take effect.  enable --now below
    # would *not* restart an already-active service.
    if unit_changed and _service_active("glowup-server.service"):
        run_sudo(["systemctl", "restart", "glowup-server.service"])
        ok("restarted glowup-server.service to pick up unit changes")
    start_server()

    port = DEFAULT_PORT  # Could re-read /etc/glowup/server.json if user customized.
    print()
    step("server install complete")
    info(f"Dashboard: {Color.BOLD}http://<this-host>:{port}/{Color.RESET}")
    info(f"Service:   sudo systemctl status glowup-server")
    info(f"Logs:      sudo journalctl -u glowup-server -f")
    info(f"Config:    {ETC_DIR}/  (read-only)")
    info(f"State:     {VAR_LIB_DIR}/  (writable by the service)")


def _service_active(name: str) -> bool:
    """True if systemctl reports the service as active."""
    proc = run(["systemctl", "is-active", name], capture=True, check=False)
    return proc.stdout.strip() == "active"


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line flags."""
    p = argparse.ArgumentParser(
        prog="install.py",
        description="GlowUp installer (see docs/BASIC.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              ./install.sh                  # interactive, picks mode based on platform
              ./install.sh --standalone     # standalone install (per-user venv)
              ./install.sh --server         # server install (Linux, sudo)
              ./install.sh --no-prompt      # skip prompts, use defaults

            Re-running this script is the upgrade path.
        """),
    )
    p.add_argument("--standalone", action="store_true",
                   help="install standalone (per-user venv, no service)")
    p.add_argument("--server", action="store_true",
                   help="install server (Linux only, requires sudo)")
    p.add_argument("--no-prompt", action="store_true",
                   help="skip all interactive prompts; use defaults")
    p.add_argument("--version", action="version",
                   version=f"GlowUp installer {__version__}")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point used by ``install.sh`` (and direct invocation)."""
    assert_python_version()
    args = parse_args(argv)
    host = detect_host()
    mode = select_mode(host, args)
    assume_yes = bool(args.no_prompt)

    if mode is Mode.STANDALONE:
        run_standalone(host, assume_yes=assume_yes)
    elif mode is Mode.SERVER:
        run_server(host, assume_yes=assume_yes)
    else:
        fail(f"unhandled mode {mode!r}", exit_code=99)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
