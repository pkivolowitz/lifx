#!/usr/bin/env python3
"""GlowUp installer bootstrap.

Pure-stdlib HTTP server that serves the installer UI in a browser.
No dependencies beyond Python 3.8+.
"""

__version__ = "0.1.0"

import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import venv
import webbrowser
from pathlib import Path

# --- Constants -----------------------------------------------------------

# Default port for the installer web UI; 0 means pick any free port.
DEFAULT_PORT = 0

# Directory containing static assets (HTML, CSS, JS, images).
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Minimum Python version required.
MIN_PYTHON = (3, 8)

# Public GitHub repo for sparse checkout.
GITHUB_REPO = "https://github.com/pkivolowitz/lifx.git"

# Files and directories comprising the CLI-only installation.
# Everything needed to run glowup.py from the command line with
# all effects including grid effects and the simulator.
CLI_BOM = [
    "glowup.py",
    "transport.py",
    "engine.py",
    "colorspace.py",
    "param.py",
    "network_config.py",
    "simulator.py",
    "network.json",
    "requirements.txt",
    "LICENSE",
    "effects/",
    "emitters/",
    "tools/grid_simulator.py",
]


# --- Request handler -----------------------------------------------------

class InstallerHandler(http.server.SimpleHTTPRequestHandler):
    """Serves static files from the installer/static directory.

    Also handles API POST requests for installer actions (future use).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:
        """Suppress default stderr logging to keep terminal clean."""
        pass

    def do_GET(self) -> None:
        """Handle GET requests — API routes or static files."""
        if self.path == "/api/defaults":
            home = str(Path.home())
            self._send_json({"default_dir": os.path.join(home, "glowup")})
            return
        # Fall through to static file serving.
        super().do_GET()

    def do_POST(self) -> None:
        """Handle POST requests from the installer UI."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        if self.path == "/api/shutdown":
            self._send_json({"status": "shutting_down"})
            # Schedule shutdown on a separate thread so the response completes.
            threading.Thread(target=_shutdown_server, daemon=True).start()
        elif self.path == "/api/install/cli":
            self._handle_cli_install(body)
        else:
            self.send_error(404, "Not found")

    def _handle_cli_install(self, body: bytes) -> None:
        """Run the CLI-only installation and return results."""
        try:
            params = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"status": "error", "error": "Invalid JSON"})
            return

        raw_dir = params.get("install_dir", "").strip()
        if not raw_dir:
            raw_dir = os.path.join(str(Path.home()), "glowup")
        create_venv = params.get("create_venv", True)

        result = _install_cli(raw_dir, create_venv)
        self._send_json(result)

    def _send_json(self, data: dict) -> None:
        """Send a JSON response."""
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# --- CLI installation logic ----------------------------------------------


def _run(cmd: list, cwd: str = None) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing stdout and stderr."""
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,  # 5-minute ceiling for git clone
    )


def _install_cli(raw_dir: str, create_venv: bool) -> dict:
    """Perform a CLI-only installation via sparse checkout.

    Returns a dict with status, steps log, and result metadata.
    """
    steps: list = []
    install_dir = os.path.expanduser(raw_dir)

    # --- Preflight checks ------------------------------------------------

    if not shutil.which("git"):
        return {
            "status": "error",
            "error": "git is not installed or not on PATH.",
            "steps": steps,
        }

    if os.path.exists(install_dir) and os.listdir(install_dir):
        return {
            "status": "error",
            "error": f"Directory already exists and is not empty: {install_dir}",
            "steps": steps,
        }

    # --- Sparse clone ----------------------------------------------------

    steps.append(f"Creating directory: {install_dir}")
    os.makedirs(install_dir, exist_ok=True)

    steps.append("Initializing git repository...")
    r = _run(["git", "init"], cwd=install_dir)
    if r.returncode != 0:
        return {"status": "error", "error": r.stderr.strip(), "steps": steps}

    steps.append(f"Adding remote: {GITHUB_REPO}")
    r = _run(["git", "remote", "add", "origin", GITHUB_REPO], cwd=install_dir)
    if r.returncode != 0:
        return {"status": "error", "error": r.stderr.strip(), "steps": steps}

    steps.append("Configuring sparse checkout...")
    r = _run(["git", "sparse-checkout", "init", "--cone"], cwd=install_dir)
    if r.returncode != 0:
        return {"status": "error", "error": r.stderr.strip(), "steps": steps}

    # Sparse checkout: set the directories and files we want.
    # --cone mode works with directories; individual files at root are
    # included by default when they're on the cone boundary.
    # We set the directories explicitly, then fetch.
    sparse_dirs = [item.rstrip("/") for item in CLI_BOM if item.endswith("/")]
    # For tools/grid_simulator.py we need the tools directory in sparse set.
    if "tools/grid_simulator.py" in CLI_BOM:
        sparse_dirs.append("tools")

    r = _run(
        ["git", "sparse-checkout", "set"] + sparse_dirs,
        cwd=install_dir,
    )
    if r.returncode != 0:
        return {"status": "error", "error": r.stderr.strip(), "steps": steps}

    steps.append("Fetching from GitHub (this may take a moment)...")
    r = _run(
        ["git", "fetch", "--depth=1", "origin", "master"],
        cwd=install_dir,
    )
    if r.returncode != 0:
        return {"status": "error", "error": r.stderr.strip(), "steps": steps}

    steps.append("Checking out files...")
    r = _run(["git", "checkout", "origin/master"], cwd=install_dir)
    if r.returncode != 0:
        return {"status": "error", "error": r.stderr.strip(), "steps": steps}

    # Sparse checkout with --cone includes root files matching the cone
    # boundary, but some of our root .py files may not land. Verify the
    # critical ones exist and report what we got.
    missing = []
    for item in CLI_BOM:
        target = os.path.join(install_dir, item.rstrip("/"))
        if not os.path.exists(target):
            missing.append(item)

    if missing:
        steps.append(f"Warning: some expected files not found: {', '.join(missing)}")

    steps.append("Sparse checkout complete.")

    # Clean up the tools/ dir to only keep grid_simulator.py.
    tools_dir = os.path.join(install_dir, "tools")
    if os.path.isdir(tools_dir):
        for name in os.listdir(tools_dir):
            if name != "grid_simulator.py" and name != "__pycache__":
                target = os.path.join(tools_dir, name)
                if os.path.isfile(target):
                    os.remove(target)
                elif os.path.isdir(target):
                    shutil.rmtree(target)

    # --- Virtual environment (optional) ----------------------------------

    venv_path = None
    python_cmd = "python3"

    if create_venv:
        venv_path = os.path.join(install_dir, "venv")
        steps.append(f"Creating virtual environment: {venv_path}")
        try:
            venv.create(venv_path, with_pip=True)
            steps.append("Virtual environment created.")
            # Determine the python binary inside the venv.
            if platform.system() == "Windows":
                python_cmd = os.path.join(venv_path, "Scripts", "python.exe")
            else:
                python_cmd = os.path.join(venv_path, "bin", "python")
        except Exception as exc:
            steps.append(f"Warning: venv creation failed: {exc}")
            steps.append("You can still run GlowUp with your system Python.")
            venv_path = None

    # --- Done ------------------------------------------------------------

    installed_count = sum(
        1 for item in CLI_BOM
        if os.path.exists(os.path.join(install_dir, item.rstrip("/")))
    )
    steps.append(f"Installed {installed_count}/{len(CLI_BOM)} components.")

    return {
        "status": "ok",
        "install_dir": install_dir,
        "venv_path": venv_path,
        "python_cmd": python_cmd,
        "steps": steps,
    }


# --- Server lifecycle ----------------------------------------------------

_server_instance = None


def _shutdown_server() -> None:
    """Shut down the HTTP server from a background thread."""
    global _server_instance
    if _server_instance:
        _server_instance.shutdown()


def _find_free_port() -> int:
    """Bind to port 0 and let the OS assign a free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def main() -> None:
    """Entry point: start the server and open the browser."""
    global _server_instance

    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
            f"found {platform.python_version()}."
        )

    if not STATIC_DIR.is_dir():
        sys.exit(f"Static directory not found: {STATIC_DIR}")

    port = _find_free_port()
    _server_instance = http.server.HTTPServer(("127.0.0.1", port), InstallerHandler)

    url = f"http://127.0.0.1:{port}/index.html"
    print(f"GlowUp Installer running at {url}")
    print("Press Ctrl+C to quit.")

    # Open the browser after a short delay so the server is ready.
    threading.Timer(0.3, webbrowser.open, args=(url,)).start()

    try:
        _server_instance.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _server_instance.server_close()
        print("\nInstaller stopped.")


if __name__ == "__main__":
    main()
