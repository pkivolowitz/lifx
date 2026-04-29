#!/usr/bin/env bash
#
# GlowUp installer bootstrap.
#
# This file's only job is to find a Python interpreter at the version
# documented in docs/BASIC.md (3.11+) and hand off to install.py with
# whatever flags the user passed.  All real installer logic lives in
# install.py, where stdlib JSON/argparse/subprocess/pathlib are
# available and the same code path can be tested.
#
# Why bash here at all: install.py needs Python already installed at
# the right version.  This bootstrap is the "before Python is verified"
# layer that selects an interpreter and (if none is found) prints a
# clear message in the lowest-common-denominator language available
# on every Mac and Linux system out of the box.
#
# Re-running this script is the upgrade path; install.py treats existing
# state as upgrade-in-place and only rebuilds when the Python version
# itself has changed.

set -euo pipefail

PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=11

# Prefer the highest-version interpreter we can find; fall back to bare
# python3 last.  This lets a user with both 3.11 and 3.13 installed get
# 3.13 without further prompting.
CANDIDATES=(python3.13 python3.12 python3.11 python3)

PYTHON=""
for cand in "${CANDIDATES[@]}"; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c "
import sys
need = ($PYTHON_MIN_MAJOR, $PYTHON_MIN_MINOR)
sys.exit(0 if sys.version_info[:2] >= need else 1)
" 2>/dev/null; then
            PYTHON="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    cat >&2 <<EOF
GlowUp requires Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} or newer.

  macOS (Homebrew):
      brew install python@3.13

  Debian / Ubuntu / Raspberry Pi OS:
      sudo apt update
      sudo apt install python3.11 python3.11-venv

Then re-run ./install.sh
EOF
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_PY="${SCRIPT_DIR}/install.py"

if [ ! -f "$INSTALL_PY" ]; then
    echo "GlowUp installer is incomplete: ${INSTALL_PY} not found." >&2
    echo "Re-clone the repository or check out a working commit." >&2
    exit 2
fi

# Hand off to install.py.  exec replaces this shell so the user sees
# install.py's exit code directly, no bash-wrapper noise.
exec "$PYTHON" "$INSTALL_PY" "$@"
