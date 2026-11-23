#!/usr/bin/env python3
"""
On-demand environment check & repair for Data Kitsu Tools launchers.

Normal launches skip library checks completely for instant startup.
Runs `import PySide6` + auto-install only when `--check` or `--repair` is passed
(for example, when `data_kitsu_tools.py` exits with an error).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def check_and_install() -> int:
    if sys.version_info < (3, 9):
        print(f"ERROR: Python 3.9 or newer is required (found {sys.version.split()[0]}).")
        return 1

    print(f"[1/2] Checking Python... OK: Python {sys.version.split()[0]}")
    print("[2/2] Checking PySide6...")
    try:
        import PySide6  # noqa: F401
        print("      OK: PySide6 is installed")
        return 0
    except ImportError:
        print("      PySide6 not found, installing from requirements.txt ...")
        req = APP_DIR / "requirements.txt"
        cmd = [sys.executable, "-m", "pip", "install", "--user", "-r", str(req)]
        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            res = subprocess.run(cmd + ["--break-system-packages"], check=False)
        try:
            import PySide6  # noqa: F401
            print("      OK: PySide6 installed")
            return 0
        except ImportError:
            print("ERROR: PySide6 installation failed.")
            return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Run environment check")
    parser.add_argument("--repair", action="store_true", help="Check & repair after launch failure")
    args = parser.parse_args()
    if args.check or args.repair:
        return check_and_install()
    return 0 if sys.version_info >= (3, 9) else 1


if __name__ == "__main__":
    raise SystemExit(main())
