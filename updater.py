#!/usr/bin/env python3
"""Update checker for Data Kitsu Tools.

Used by the INFO window (Check for update) and runnable from a terminal.
It compares the local VERSION file with the one on GitHub and, when a newer
version exists, can update the app. Standard library only.

Usage:
    python updater.py            # check and ask before updating
    python updater.py --yes      # check and update without asking
    python updater.py --version  # print the local version
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO_OWNER = "thomasthinhvu"
REPO_NAME = "data-kitsu-tools"
BRANCH = "main"
API_VERSION_URL = (f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
                   f"/contents/VERSION?ref={BRANCH}")
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/VERSION"
ZIP_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/heads/{BRANCH}.zip"
APP_DIR = Path(__file__).resolve().parent
TIMEOUT = 8  # seconds; keeps the launcher fast when offline


def read_local_version() -> str:
    version_file = APP_DIR / "VERSION"
    return version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "0"


def parse_version(text: str) -> tuple:
    """Sort key for versions. Examples of the release order:
    1.0 < 1.01 < 1.02 < 1.1 < 1.1.1 < 1.1.2 < 1.2

    A segment with a leading zero after the first dot is a same-day build of
    the previous number: "01" -> 0.1, so 1.01 sorts before 1.1.
    """
    parts = []
    for i, seg in enumerate(text.strip().split(".")):
        if not seg.isdigit():
            continue
        if i > 0 and len(seg) > 1 and seg.startswith("0"):
            parts.extend([0, int(seg)])
        else:
            parts.append(int(seg))
    return tuple(parts)


def fetch_latest_version():
    """Return the version string on GitHub, or None when GitHub cannot be reached."""
    # The API is tried first; the raw URL is a fallback.
    requests = [
        urllib.request.Request(API_VERSION_URL, headers={
            "Accept": "application/vnd.github.raw",
            "User-Agent": "data-kitsu-tools-updater"}),
        urllib.request.Request(RAW_VERSION_URL, headers={"User-Agent": "data-kitsu-tools-updater"}),
    ]
    for req in requests:
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                text = resp.read().decode("utf-8").strip()
            if re.fullmatch(r"\d+(\.\d+)*", text):
                return text
        except Exception:
            continue
    return None


def install_requirements():
    req = APP_DIR / "requirements.txt"
    if not req.exists():
        return
    base = [sys.executable, "-m", "pip", "install", "--user", "-r", str(req)]
    result = subprocess.run(base)
    if result.returncode != 0:  # some Linux distros need this flag
        subprocess.run(base + ["--break-system-packages"])


def update_with_git() -> bool:
    """Fast path for a cloned repo: fast-forward pull from GitHub."""
    if not (APP_DIR / ".git").exists() or shutil.which("git") is None:
        return False
    result = subprocess.run(["git", "-C", str(APP_DIR), "pull", "--ff-only", "origin", BRANCH])
    return result.returncode == 0


def update_with_zip() -> bool:
    """Fallback for a plain download: fetch the branch zip and copy files over."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        archive = tmp / "source.zip"
        try:
            with urllib.request.urlopen(ZIP_URL, timeout=60) as resp, open(archive, "wb") as out:
                shutil.copyfileobj(resp, out)
        except Exception as exc:
            print(f"Download failed: {exc}")
            return False
        extract_dir = tmp / "extracted"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)          # zipfile blocks ../ and absolute paths
        roots = [p for p in extract_dir.iterdir() if p.is_dir()]
        if not roots:
            print("Downloaded archive is empty.")
            return False
        source = roots[0]
        for item in source.iterdir():
            if item.name == ".git":
                continue
            target = APP_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    return True


def do_update(latest: str) -> bool:
    print(f"Updating to version {latest} ...")
    ok = update_with_git() or update_with_zip()
    if not ok:
        print("Update failed. Check your internet connection or update manually from GitHub.")
        return False
    install_requirements()
    print(f"Updated to version {read_local_version()}.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Data Kitsu Tools updater")
    parser.add_argument("--yes", action="store_true", help="update without asking")
    parser.add_argument("--version", action="store_true", help="print the local version and exit")
    args = parser.parse_args()

    local = read_local_version()
    if args.version:
        print(local)
        return 0

    latest = fetch_latest_version()
    if latest is None:
        print("(Update check skipped: cannot reach GitHub.)")
        return 0
    if parse_version(latest) <= parse_version(local):
        print(f"Data Kitsu Tools {local} is up to date.")
        return 0

    print("=" * 56)
    print(f"  UPDATE AVAILABLE: {latest}   (you have {local})")
    print("=" * 56)
    if args.yes:
        answer = "y"
    elif sys.stdin.isatty():
        answer = input("Update now? [y/N]: ").strip().lower()
    else:
        answer = "n"
    if answer in ("y", "yes"):
        do_update(latest)
    else:
        print("Skipped. Run the launcher again to update later.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
