#!/usr/bin/env python3
"""
Fast environment check for Data Kitsu Tools launchers (run.cmd / run.sh).

- Skips the slow `import PySide6` check when verified within the last 7 days.
- Runs full check + auto-install when:
    * `--check` or `--repair` is passed
    * more than 7 days have passed since the last successful check
    * the app previously failed to start
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CACHE_FILE = APP_DIR / "run_logs" / "env_check.json"
WEEK_SECONDS = 7 * 24 * 60 * 60


def _is_cache_fresh() -> bool:
    if not CACHE_FILE.is_file():
        return False
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("checked_at", 0))
        return 0 <= age < WEEK_SECONDS and data.get("python") == sys.executable
    except (OSError, ValueError, TypeError):
        return False


def _write_cache() -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(
            json.dumps(
                {
                    "checked_at": time.time(),
                    "python": sys.executable,
                    "version": sys.version.split()[0],
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def check_and_install(force: bool = False) -> int:
    if sys.version_info < (3, 9):
        print(f"ERROR: Python 3.9 or newer is required (found {sys.version.split()[0]}).")
        return 1

    if not force and _is_cache_fresh():
        return 0

    print(f"[1/2] Checking Python... OK: Python {sys.version.split()[0]}")
    print("[2/2] Checking PySide6...")
    try:
        import PySide6  # noqa: F401
        print("      OK: PySide6 is installed")
        _write_cache()
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
            _write_cache()
            return 0
        except ImportError:
            print("ERROR: PySide6 installation failed.")
            return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Force environment check")
    parser.add_argument("--repair", action="store_true", help="Force check & repair after failure")
    args = parser.parse_args()
    return check_and_install(force=args.check or args.repair)


if __name__ == "__main__":
    raise SystemExit(main())
