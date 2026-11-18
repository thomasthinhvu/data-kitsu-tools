#!/usr/bin/env bash
# Data Kitsu Tools launcher for Linux and macOS.
# Checks Python and PySide6, installs missing requirements, then starts the GUI.
# Pass --check to only run the checks (no GUI), --no-update to skip the update check.
set -e
cd "$(dirname "$0")"

MIN_MAJOR=3
MIN_MINOR=9

echo "[1/4] Checking Python..."
PY=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: Python 3 was not found. Install it from https://www.python.org/downloads/ and run again."
    exit 1
fi
if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)"; then
    echo "ERROR: Python $MIN_MAJOR.$MIN_MINOR or newer is required. Found: $("$PY" --version 2>&1)"
    exit 1
fi
echo "      OK: $("$PY" --version 2>&1) ($PY)"

echo "[2/4] Checking for updates..."
if [ "$1" = "--no-update" ] || [ "$2" = "--no-update" ]; then
    echo "      skipped (--no-update)"
else
    "$PY" updater.py || echo "      update check failed, continuing with the local version"
fi

echo "[3/4] Checking PySide6..."
if "$PY" -c "import PySide6" >/dev/null 2>&1; then
    echo "      OK: PySide6 is installed"
else
    echo "      PySide6 not found, installing from requirements.txt ..."
    if ! "$PY" -m pip --version >/dev/null 2>&1; then
        echo "ERROR: pip is not available for $PY. Install pip and run again."
        exit 1
    fi
    "$PY" -m pip install --user -r requirements.txt || "$PY" -m pip install --user --break-system-packages -r requirements.txt
    if ! "$PY" -c "import PySide6" >/dev/null 2>&1; then
        echo "ERROR: PySide6 installation failed."
        exit 1
    fi
    echo "      OK: PySide6 installed"
fi

if [ "$1" = "--check" ] || [ "$2" = "--check" ]; then
    echo "[4/4] All checks passed (--check mode, GUI not started)."
    exit 0
fi

echo "[4/4] Starting Data Kitsu Tools..."
exec "$PY" data_kitsu_tools.py
