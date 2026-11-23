#!/usr/bin/env bash
# Data Kitsu Tools launcher for Linux and macOS.
# Launches immediately; checks/repairs Python & PySide6 only on error or --check.
set -e
cd "$(dirname "$0")"

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

if [ "$1" = "--check" ] || [ "$2" = "--check" ]; then
    "$PY" launcher_check.py --check
    echo "[3/3] All checks passed (--check mode, GUI not started)."
    exit 0
fi

if ! "$PY" data_kitsu_tools.py; then
    echo "App exited with an error. Running library check/repair..."
    "$PY" launcher_check.py --repair
    exec "$PY" data_kitsu_tools.py
fi
