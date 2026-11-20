#!/usr/bin/env python3
"""
Data Kitsu Tools
================
Copy VFX shot data (EXR, DPX, MOV, TIF, PNG, JPG sequences) listed in a
Kitsu CSV export from a source tree into a destination tree.

LOCAL   : copy the shot folders (and their PLxx variants) from COPY FROM to
          PASTE TO, keeping the folder structure of the source.
PUBLISH : copy only the highest vNN version of each shot into
          PASTE TO/<YYMMDD>/<shot folder> (the date folder is optional).

Requires Python 3.9+ and PySide6. Runs on Windows, macOS and Linux.
"""
from __future__ import annotations

import csv
import json
import logging
from logging.handlers import RotatingFileHandler
import datetime as dt
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (QByteArray, QPointF, QSize, QStandardPaths, Qt, QStringListModel,
                            QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QIcon, QImage, QKeySequence,
                           QPainter, QPalette, QPen, QShortcut, QTextCharFormat,
                           QPixmap, QPolygonF, QTextCursor)
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QTabWidget, QTextBrowser
import kitsu_client
import retro_sfx
import shot_import
import updater  # stdlib helper next to this file: GitHub version check and update
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QMenu, QInputDialog, QDialog, QToolButton, QSpinBox,
    QCompleter,
)

APP_NAME = "Data Kitsu Tools"
APP_VERSION = "2.0.0"
WINDOWS_PATH_LIMIT = 250        # warn above this destination path length (Windows MAX_PATH is 260)
PATH_HISTORY_MAX = 10          # recent COPY FROM / PASTE TO paths kept (shared)
WALLET_BSC = "0x5517244f04a4b769224f1489cd41d3b70171161d"
AUTHOR = "Thomas Thinh Vu"
DONATE_QR = Path(__file__).resolve().parent / "assets" / "donate_qr.png"
# (kind for the icon, text shown, link opened)
CONTACTS = [
    ("artstation", "https://thomasvfx.artstation.com/", "https://thomasvfx.artstation.com/"),
    ("linkedin", "https://www.linkedin.com/in/thinh-vu-376534106/",
     "https://www.linkedin.com/in/thinh-vu-376534106/"),
    ("facebook", "https://www.facebook.com/thinhvuvfx/", "https://www.facebook.com/thinhvuvfx/"),
    ("gmail", "thomasvuvfx@gmail.com", "mailto:thomasvuvfx@gmail.com"),
]
BRAND_COLORS = {"artstation": "#13aff0", "linkedin": "#0a66c2",
                "facebook": "#1877f2", "gmail": "#ea4335"}
BRAND_LETTERS = {"artstation": "A", "linkedin": "in", "facebook": "f"}

BUFFER_SIZE = 8 * 1024 * 1024   # 8 MB chunks: fewer, larger I/O calls
MAX_WORKERS = 4                 # parallel file copies per shot

MODE_LOCAL = "LOCAL"
MODE_PUBLISH = "PUBLISH"

FILE_TYPES = {
    "EXR": (".exr",),
    "DPX": (".dpx",),
    "MOV": (".mov",),
    "TIF/TIFF": (".tif", ".tiff"),
    "PNG": (".png",),
    "JPG": (".jpg", ".jpeg"),
}
LOCAL_TYPE_KEYS = ("EXR", "DPX", "MOV", "TIF/TIFF", "PNG", "JPG")
PUBLISH_TYPE_KEYS = ("EXR", "DPX", "MOV")

SYSTEM_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
VARIANT_RE = re.compile(r"_(PL\d+)$", re.IGNORECASE)
VERSION_RE = re.compile(r"_v(\d+)$", re.IGNORECASE)
FRAME_RE = re.compile(r"(\d+)\.[^.]+$")
MAX_GAP_RANGE = 100000          # ignore frame gap checks on absurd ranges

# Row colours
C_YELLOW = "#f5d000"
C_RED = "#ff4d4d"
C_BLUE = "#3aa0ff"
C_GREY = "#8a8f98"
C_ORANGE = "#ff9f43"

COLUMNS = 6
# SHOTS LIST columns
COL_NO, COL_SEQ, COL_NAME, COL_VERSION, COL_FRAMES, COL_STATUS = range(6)
ROLE_INDEX = int(Qt.ItemDataRole.UserRole) + 1   # shot index stored on top rows
ROLE_PATH = int(Qt.ItemDataRole.UserRole) + 2    # folder path stored on variant rows


def session_file() -> Path:
    """Per-user session file. Windows: %APPDATA%, macOS: ~/Library/Application Support,
    Linux: ~/.local/share (the Qt AppDataLocation for the app name)."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    root = Path(base) if base else Path.home() / ".data-kitsu-tools"
    return root / "session.json"


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------
@dataclass
class Unit:
    """One folder that gets copied for a shot (a PL variant or a version)."""
    path: Path
    label: str
    found: int                      # files directly inside the best folder
    gaps: list                      # missing frame numbers
    copy_files: list                # every file to copy (recursive)


@dataclass
class ScanResult:
    mode: str
    units: list
    found: int
    version_text: str
    missing: list
    copied: bool = False            # every file already exists at the destination
    dest: str = ""                  # destination folder that holds the copy
    source: str = ""                # COPY FROM folder that was searched


@dataclass
class Shot:
    name: str
    frames: int | None              # None = the CSV has no Frames value for this shot
    scan: ScanResult | None = None
    state: str = "new"              # new | scanned | copying | done | partial | error
    done: int = 0
    force_overwrite: bool = False   # set after STOP so the next run overwrites
    force_copy: bool = False        # user chose "Force copy": copy despite a frame mismatch, overwrite
    seq: str = ""                   # Sequence column of the CSV (shown in the SEQ column)
    kitsu_id: str = ""              # Kitsu shot ID when imported from Kitsu


def frames_text(frames) -> str:
    return "?" if frames is None else str(frames)


def frames_match(found: int, frames) -> bool:
    """True when the file count fits the CSV. Without a CSV count, any count fits."""
    return frames is None or found == frames


# ----------------------------------------------------------------------------
# File-system helpers (pure functions, no GUI)
# ----------------------------------------------------------------------------
class _Stopped(Exception):
    """Raised inside copy_file when the user pressed STOP."""


def is_system_name(name: str) -> bool:
    """True for OS metadata (macOS AppleDouble '._' files, .DS_Store, Thumbs.db...)."""
    return name.startswith("._") or name.lower() in SYSTEM_NAMES


def direct_files(folder: Path, exts, skip_system: bool) -> list:
    """Files directly inside folder (not recursive), filtered by extension."""
    result = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if skip_system and is_system_name(entry.name):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if exts and os.path.splitext(entry.name)[1].lower() not in exts:
                    continue
                result.append(Path(entry.path))
    except OSError:
        pass
    return sorted(result)


def tree_files(folder: Path, exts, skip_system: bool) -> list:
    """Every matching file under folder, recursively."""
    result = []
    for dirpath, dirnames, filenames in os.walk(folder):
        if skip_system:
            dirnames[:] = [d for d in dirnames if not is_system_name(d)]
        for fname in filenames:
            if skip_system and is_system_name(fname):
                continue
            if exts and os.path.splitext(fname)[1].lower() not in exts:
                continue
            result.append(Path(dirpath) / fname)
    return sorted(result)


REEL_TOKEN_RE = re.compile(r"^r\d+$")          # reel tag such as R1 (lower-cased)
NUMBER_TOKEN_RE = re.compile(r"^(\d+)(\.\d+)?([a-z]?)$")


def name_tokens(name: str, keep_reel: bool = True, strict: bool = False) -> tuple:
    """Split a shot or folder name into tokens that can be compared.

    - "-" and "_" both separate tokens: "010-add02" -> ("10", "add02")
    - strict=False: numbers lose their leading zeros ("010" == "10")
    - strict=True: numbers keep their zeros ("050" != "0050"), used first so that
      two shots written differently in the same CSV never swap folders
    - a single letter after a number is merged: "01 A" / "01_A" -> "1a"
    - keep_reel=False drops the reel tag in position 1 (HHCC_R1_SC04_020 -> HHCC_SC04_020)
    """
    raw = [t for t in re.split(r"[_\-\s]+", name.strip().casefold()) if t]
    tokens = []
    for i, tok in enumerate(raw):
        if i == 1 and not keep_reel and REEL_TOKEN_RE.match(tok) and raw[0].isalpha():
            continue
        m = NUMBER_TOKEN_RE.match(tok)
        if m and not strict:
            tok = str(int(m.group(1))) + (m.group(2) or "") + m.group(3)
        if len(tok) == 1 and tok.isalpha() and tokens and re.fullmatch(r"\d+(\.\d+)?", tokens[-1]):
            tokens[-1] += tok                   # "10" + "a" -> "10a"
            continue
        tokens.append(tok)
    return tuple(tokens)


class ShotIndex:
    """Looks up which shot names a folder belongs to, using the naming rules above.

    A folder belongs to a shot when the shot's tokens are the first tokens of the
    folder name. Rules, in order:
    - the reel tag is compared first; without it, the shot must be the only one
      with that key (HHCC_R1_010 and HHCC_R2_010 both exist, so a folder with no reel
      tag matches nothing)
    - the longest match wins: HHCC_SC07_010_PL01 goes to HHCC_SC07_010_PL01 when
      that shot is in the CSV, and to HHCC_SC07_010 otherwise
    - exact spelling (zeros kept) beats the normalised spelling at the same length
    """

    def __init__(self, names):
        self.tables = {}
        for strict in (True, False):
            for keep_reel in (True, False):
                table = {}
                for name in names:
                    table.setdefault(name_tokens(name, keep_reel, strict), []).append(name)
                self.tables[(strict, keep_reel)] = table

    def match_folder(self, folder_name: str) -> list:
        hits = []                           # (token count, exact spelling, shot name)
        for strict in (True, False):
            for keep_reel in (True, False):
                tokens = name_tokens(folder_name, keep_reel, strict)
                table = self.tables[(strict, keep_reel)]
                for k in range(1, len(tokens) + 1):
                    found = table.get(tokens[:k])
                    if not found or (not keep_reel and len(found) != 1):
                        continue
                    hits.extend((k, strict, shot) for shot in found)
        if not hits:
            return []
        best = max((k, strict) for k, strict, _ in hits)
        return sorted({shot for k, strict, shot in hits if (k, strict) == best})


def folder_matches(folder_name: str, shot_name: str) -> bool:
    """True when a folder belongs to a shot (checked on its own, no other shots)."""
    return bool(ShotIndex([shot_name]).match_folder(folder_name))


def scan_source(copy_from: Path, names) -> dict:
    """Walk the source tree once and return {shot name: [matching folders]}."""
    names = list(dict.fromkeys(names))
    index = ShotIndex(names)
    matches = {n: [] for n in names}
    for dirpath, dirnames, _ in os.walk(copy_from):
        dirnames[:] = [d for d in dirnames if not is_system_name(d)]
        for d in dirnames:
            for shot in index.match_folder(d):
                matches[shot].append(Path(dirpath) / d)
    return matches


def find_first_folder(root: Path, name: str, names=None):
    """Return the first folder under root that belongs to a shot (used for previews)."""
    index = ShotIndex(names or [name])
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_system_name(d)]
        for d in dirnames:
            if name in index.match_folder(d):
                return Path(dirpath) / d
    return None


def latest_version(paths):
    """Folder with the highest _vNN suffix, or (None, -1)."""
    best, best_v = None, -1
    for p in paths:
        m = VERSION_RE.search(p.name)
        if m and int(m.group(1)) > best_v:
            best, best_v = p, int(m.group(1))
    return best, best_v


def unit_label(folder: Path, shot_name: str = "") -> str:
    """Short label for the Version column: PL01, v06, -add02 or base."""
    name = folder.name
    m = VARIANT_RE.search(name)
    if m:
        return m.group(1).upper()
    m = VERSION_RE.search(name)
    if m:
        return f"v{int(m.group(1)):02d}"
    if "-" in name:
        return name.rsplit("-", 1)[1]
    return "base"


def frame_gaps(files) -> list:
    """Frame numbers missing between the first and last frame of a sequence."""
    numbers = set()
    for f in files:
        m = FRAME_RE.search(f.name)
        if m:
            numbers.add(int(m.group(1)))
    if len(numbers) < 2:
        return []
    lo, hi = min(numbers), max(numbers)
    if hi - lo > MAX_GAP_RANGE:
        return []
    return [n for n in range(lo, hi + 1) if n not in numbers]


def analyze_shot(name: str, matched, mode: str, exts, skip_system: bool) -> ScanResult:
    """Turn the matched folders of one shot into a ScanResult."""
    direct = {d: direct_files(d, exts, skip_system) for d in matched}

    if mode == MODE_PUBLISH:
        best, _ = latest_version(matched)
        if best is None:
            return ScanResult(mode, [], 0, "-", [])
        files = direct[best]
        unit = Unit(best, unit_label(best, name), len(files), frame_gaps(files),
                    tree_files(best, exts, skip_system))
        return ScanResult(mode, [unit], unit.found, unit.label, unit.gaps)

    # LOCAL: every top-most matched folder is one unit (PL variants, plain folders)
    matched_set = set(matched)
    tops = [p for p in matched if not any(a in matched_set for a in p.parents)]
    units = []
    for top in sorted(tops, key=lambda p: p.name):
        inside = [d for d in matched if d == top or top in d.parents]
        best = max(inside, key=lambda d: len(direct[d]))
        units.append(Unit(top, unit_label(top, name), len(direct[best]),
                          frame_gaps(direct[best]), tree_files(top, exts, skip_system)))
    if not units:
        return ScanResult(mode, [], 0, "-", [])
    best_unit = max(units, key=lambda u: u.found)
    labels = []
    for u in units:
        if u.label not in labels:
            labels.append(u.label)
    return ScanResult(mode, units, best_unit.found, ", ".join(labels), best_unit.gaps)


def destination_base(unit: Unit, mode: str, copy_from: Path, paste_to: Path, dailies: str) -> Path:
    """Folder on the destination that receives one unit."""
    if mode == MODE_LOCAL:
        return paste_to / unit.path.relative_to(copy_from)
    if dailies:
        return paste_to / dailies / unit.path.name
    return paste_to / unit.path.name


def build_jobs(shot: Shot, mode: str, copy_from: Path, paste_to: Path, dailies: str) -> list:
    """List of (source file, destination file) pairs for one shot."""
    jobs = []
    for unit in shot.scan.units:
        base = destination_base(unit, mode, copy_from, paste_to, dailies)
        for src in unit.copy_files:
            jobs.append((src, base / src.relative_to(unit.path)))
    return jobs


def mark_destination(result: ScanResult, name: str, mode: str, copy_from: Path,
                     paste_to: Path, dailies: str):
    """Flag a shot whose files already exist on the destination.

    The Version column then shows the version found there, e.g. "v02-copied".
    """
    if not result.units:
        return
    labels, first = [], None
    for unit in result.units:
        base = destination_base(unit, mode, copy_from, paste_to, dailies)
        pairs = [(src, base / src.relative_to(unit.path)) for src in unit.copy_files]
        if not pairs or not all(same_size(src, dst) for src, dst in pairs):
            return                          # something is missing: the shot is copied as usual
        first = first or base
        label = unit_label(base, name)
        if label not in labels:
            labels.append(label)
    result.copied = True
    result.dest = str(first)
    result.version_text = ", ".join(f"{label}-copied" for label in labels)


def same_size(src: Path, dst: Path) -> bool:
    """True when dst exists and has the same size as src, i.e. a complete copy."""
    try:
        return dst.stat().st_size == src.stat().st_size
    except OSError:
        return False


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def preflight(plans_jobs: list, paste_to: Path) -> dict:
    """Checks made before a copy starts.

    plans_jobs: list of (jobs, overwrite); jobs is a list of (source, destination).
    """
    need = to_copy = empty = long_paths = leftovers = 0
    for jobs, overwrite in plans_jobs:
        for src, dst in jobs:
            try:
                src_size = src.stat().st_size
            except OSError:
                continue                    # missing source: reported as an error during copy
            if src_size == 0:
                empty += 1
            if overwrite or not same_size(src, dst):
                need += src_size
                to_copy += 1
            if len(str(dst)) > WINDOWS_PATH_LIMIT:
                long_paths += 1
            if dst.with_name(dst.name + ".part").exists():
                leftovers += 1
    target = paste_to
    while not target.exists() and target.parent != target:
        target = target.parent              # nearest folder that exists
    exists = target.exists()
    return {
        "need": need, "to_copy": to_copy, "empty": empty, "long_paths": long_paths,
        "leftovers": leftovers,
        "free": shutil.disk_usage(target).free if exists else None,
        "writable": exists and os.access(target, os.W_OK),
    }


class IoStats:
    """Shared by all copy threads: bytes moved and time with disk I/O in progress."""

    def __init__(self):
        self._lock = threading.Lock()
        self.bytes = 0
        self._busy = 0.0            # finished busy time, seconds
        self._active = 0            # I/O calls running right now
        self._since = 0.0           # start of the current busy stretch

    def begin_io(self):
        with self._lock:
            if self._active == 0:
                self._since = time.monotonic()
            self._active += 1

    def end_io(self, nbytes: int):
        with self._lock:
            self.bytes += nbytes
            self._active -= 1
            if self._active == 0:
                self._busy += time.monotonic() - self._since

    def snapshot(self):
        """(total bytes, total busy seconds, counting a busy stretch still running)."""
        with self._lock:
            busy = self._busy
            if self._active:
                busy += time.monotonic() - self._since
            return self.bytes, busy


def _remove_quiet(path: Path):
    try:
        path.unlink()
    except OSError:
        pass


def copy_file(src: Path, dst: Path, overwrite: bool,
              paused: threading.Event, stop: threading.Event, stats=None) -> str:
    """Copy one file in 8 MB chunks. Returns copied / skipped / stopped.

    Data is written to <name>.part first and renamed when complete, so a
    cancelled copy never leaves a half-written file behind.
    """
    if stop.is_set():
        return "stopped"
    if dst.exists() and not overwrite and same_size(src, dst):
        return "skipped"                    # complete copy already there; a short one is redone
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    try:
        buffer = bytearray(BUFFER_SIZE)
        view = memoryview(buffer)
        with open(src, "rb", buffering=BUFFER_SIZE) as fin, \
                open(tmp, "wb", buffering=BUFFER_SIZE) as fout:
            while True:
                while paused.is_set() and not stop.is_set():
                    time.sleep(0.2)
                if stop.is_set():
                    raise _Stopped()
                if stats is not None:
                    stats.begin_io()
                n = 0
                try:
                    n = fin.readinto(view) or 0
                    if n:
                        fout.write(view[:n])
                finally:
                    if stats is not None:
                        stats.end_io(n)
                if not n:
                    break
            fout.flush()
            os.fsync(fout.fileno())         # data must reach the disk before the rename
        try:
            shutil.copystat(src, tmp)   # keep timestamps, like a copy in Explorer
        except OSError:
            pass
        os.replace(tmp, dst)
        return "copied"
    except _Stopped:
        _remove_quiet(tmp)
        return "stopped"
    except BaseException:
        _remove_quiet(tmp)
        raise


# ----------------------------------------------------------------------------
# CSV loading
# ----------------------------------------------------------------------------
def read_text(path: Path) -> str:
    """Decode a CSV exported on any OS (UTF-8 with or without BOM, UTF-16, cp1258)."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1258"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


BARE_NAME_RE = re.compile(r"^\d")                 # "010", "0043.5", "010-add02"


def load_csv(path: Path) -> list:
    """Read a Kitsu CSV: Sequence, Name and Frames ('frame' also accepted).

    A Name that is only a number ("010") takes the Sequence as its prefix when the
    Sequence holds a reel/scene tag ("R1_SC03" -> "R1_SC03_010").
    """
    text = read_text(path)
    lines = text.splitlines()
    if not lines:
        raise ValueError("The CSV file is empty.")
    delimiter = ";" if lines[0].count(";") >= lines[0].count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header = [h.strip().lower() for h in rows[0]]
    if "name" not in header:
        raise ValueError('No "Name" column found in this CSV.')
    name_idx = header.index("name")
    seq_idx = header.index("sequence") if "sequence" in header else None
    frames_idx = next((header.index(h) for h in ("frames", "frame") if h in header), None)

    def cell(row, idx):
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    shots, seen = [], set()
    for row in rows[1:]:
        if len(row) <= name_idx:
            continue
        name = row[name_idx].strip()
        seq = cell(row, seq_idx)
        if name and BARE_NAME_RE.match(name) and "_" in seq:
            name = f"{seq}_{name}"
        if not name or name in seen or "/" in name or "\\" in name:
            continue
        seen.add(name)
        frames = None
        raw = cell(row, frames_idx)
        if raw:
            try:
                frames = int(float(raw.replace(",", ".")))
            except ValueError:
                frames = None
        shots.append(Shot(name=name, frames=frames, seq=seq))
    return shots


def shots_from_kitsu(records: list[dict], include_episode_in_seq: bool = False) -> list:
    """Convert Kitsu API shot records into ``Shot`` objects.

    Applies the same Sequence + bare-number Name prefix rule as ``load_csv``
    so shots fetched directly from Kitsu match local folders identically.
    """
    shots, seen = [], set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        raw_name = str(rec.get("name") or "").strip()
        seq = str(rec.get("sequence") or "").strip()
        ep = str(rec.get("episode") or "").strip()
        if include_episode_in_seq and ep and seq:
            seq_label = f"{ep}_{seq}"
        else:
            seq_label = seq

        name = raw_name
        if name and BARE_NAME_RE.match(name) and "_" in seq_label:
            name = f"{seq_label}_{name}"
        elif name and BARE_NAME_RE.match(name) and "_" in seq:
            name = f"{seq}_{name}"
        if not name or name in seen or "/" in name or "\\" in name:
            continue
        seen.add(name)

        frames_val = rec.get("frames")
        frames = None
        if frames_val is not None:
            try:
                frames = int(frames_val)
            except (TypeError, ValueError):
                frames = None
        shots.append(
            Shot(
                name=name,
                frames=frames,
                seq=seq_label or ep,
                kitsu_id=str(rec.get("id") or ""),
            )
        )
    return shots


# ----------------------------------------------------------------------------
# Status / colour rules
# ----------------------------------------------------------------------------
def unit_style(found: int, frames) -> dict:
    if found == 0:
        return {"color": C_YELLOW}                       # no files after scan
    if not frames_match(found, frames):
        return {"color": C_RED, "bold": True}            # count mismatch, shot skipped
    return {}


def row_style(shot: Shot) -> dict:
    if shot.state == "copying":
        return {"color": C_BLUE, "bold": True}
    if shot.state == "done":
        return {"color": C_GREY, "italic": True, "strike": True}
    if shot.state == "error":
        return {"color": C_RED, "bold": True}
    if shot.state == "partial":
        return {"color": C_ORANGE}
    if shot.force_copy:
        return {}                                        # forced: normal colour, not a mismatch
    if shot.scan is not None and shot.scan.copied:
        return {"color": C_YELLOW}                       # already on the destination, skipped
    if shot.scan is not None:
        return unit_style(shot.scan.found, shot.frames)
    return {}


def status_text(shot: Shot) -> str:
    if shot.state in ("copying", "done", "partial", "error"):
        return f"{shot.done}/{frames_text(shot.frames)}"
    found = shot.scan.found if shot.scan else 0
    return f"{found}/{frames_text(shot.frames)}"


def shot_problem(shot: Shot):
    """Why a shot is yellow or red: (colour, reason, path), or None when it is fine."""
    if shot.scan is None or shot.force_copy:
        return None
    if shot.state in ("copying", "done", "partial", "error"):
        return None
    scan = shot.scan
    if scan.copied:
        return (C_YELLOW, f"Already on the destination ({scan.version_text}), shot skipped", scan.dest)
    if scan.found == 0:
        return (C_YELLOW, "No files found for this shot in COPY FROM", scan.source)
    if not frames_match(scan.found, shot.frames):
        reason = f"Frame count mismatch: found {scan.found} file(s), CSV Frames = {shot.frames}"
        if scan.missing:
            shown = ", ".join(str(n) for n in scan.missing[:60])
            if len(scan.missing) > 60:
                shown += ", ..."
            reason += f"; missing frames ({len(scan.missing)}): {shown}"
        path = "\n".join(str(u.path) for u in scan.units) or scan.source
        return (C_RED, reason, path)
    return None


def apply_style(item: QTreeWidgetItem, style: dict):
    for col in range(COLUMNS):
        if style.get("color"):
            item.setForeground(col, QBrush(QColor(style["color"])))
        else:
            item.setData(col, Qt.ItemDataRole.ForegroundRole, None)
        font = item.font(col)
        font.setBold(bool(style.get("bold")))
        font.setItalic(bool(style.get("italic")))
        font.setStrikeOut(bool(style.get("strike")))
        item.setFont(col, font)


# ----------------------------------------------------------------------------
# Worker threads
# ----------------------------------------------------------------------------
class PreviewWorker(QThread):
    """Looks up the full source folder of the first shot so the preview can show it."""
    found = Signal(str, str, str)           # copy_from, shot name, folder path ("" = none)

    def __init__(self, copy_from: Path, name: str, names=None):
        super().__init__()
        self.copy_from = copy_from
        self.name = name
        self.names = names

    def run(self):
        try:
            path = find_first_folder(self.copy_from, self.name, self.names)
        except OSError:
            path = None
        self.found.emit(str(self.copy_from), self.name, str(path) if path else "")


class ScanWorker(QThread):
    """Scans the source tree in the background so the GUI stays responsive."""
    finished_scan = Signal(object, str)     # (results list, error message)

    def __init__(self, names, copy_from: Path, mode: str, exts, skip_system: bool,
                 paste_to: Path | None = None, dailies: str = ""):
        super().__init__()
        self.names = names
        self.copy_from = copy_from
        self.mode = mode
        self.exts = tuple(exts)
        self.skip_system = skip_system
        self.paste_to = paste_to
        self.dailies = dailies

    def run(self):
        try:
            matched = scan_source(self.copy_from, self.names)
            results = [analyze_shot(n, matched[n], self.mode, self.exts, self.skip_system)
                       for n in self.names]
            for name, result in zip(self.names, results):
                result.source = str(self.copy_from)
                if self.paste_to is not None:
                    mark_destination(result, name, self.mode, self.copy_from,
                                     self.paste_to, self.dailies)
            self.finished_scan.emit(results, "")
        except Exception as exc:            # report to the GUI instead of crashing
            self.finished_scan.emit([], str(exc))


class CopyWorker(QThread):
    """Copies the planned shots one after another, files in parallel per shot."""
    shot_started = Signal(int, int)         # shot index, file count
    shot_progress = Signal(int, int)        # shot index, files done
    shot_finished = Signal(int, str)        # shot index, done / partial / error
    overall = Signal(int, int)              # files done, files total
    file_done = Signal(int, str, int, float, str, str)  # shot, file, bytes, seconds, result, error
    log = Signal(str)

    def __init__(self, plans, io: IoStats | None = None, threads: int = MAX_WORKERS):
        super().__init__()
        # plans: list of (shot index, frames, jobs, overwrite)
        self.plans = plans
        self.pause_event = threading.Event()    # set = paused
        self.stop_event = threading.Event()     # set = stop requested
        self.io = io or IoStats()
        self.threads = max(1, threads)

    def _timed_copy(self, src: Path, dst: Path, overwrite: bool):
        """copy_file with timing. Returns (result, bytes, seconds, error)."""
        start = time.monotonic()
        error = ""
        try:
            result = copy_file(src, dst, overwrite, self.pause_event, self.stop_event, self.io)
        except Exception as exc:
            result, error = "error", str(exc)
        seconds = time.monotonic() - start
        nbytes = 0
        if result == "copied":
            try:
                nbytes = src.stat().st_size
            except OSError:
                pass
        return result, nbytes, seconds, error

    def run(self):
        total = sum(len(p[2]) for p in self.plans)
        done_all = 0
        for index, _frames, jobs, overwrite in self.plans:
            if self.stop_event.is_set():
                break
            self.shot_started.emit(index, len(jobs))
            done = errors = 0
            executor = ThreadPoolExecutor(max_workers=self.threads)
            futures = {executor.submit(self._timed_copy, src, dst, overwrite): (src, dst)
                       for src, dst in jobs}
            try:
                for future in as_completed(futures):
                    if self.stop_event.is_set():
                        break
                    src, _dst = futures[future]
                    try:
                        result, nbytes, seconds, error = future.result()
                    except Exception as exc:            # cancelled or unexpected
                        result, nbytes, seconds, error = "error", 0, 0.0, str(exc)
                    if result == "stopped":
                        continue
                    self.file_done.emit(index, str(src), nbytes, seconds, result, error)
                    if error:
                        errors += 1
                        self.log.emit(f"  error: {error}")
                        continue
                    done += 1
                    done_all += 1
                    self.shot_progress.emit(index, done)
                    self.overall.emit(done_all, total)
            finally:
                for f in futures:
                    f.cancel()
                executor.shutdown(wait=True)
            if self.stop_event.is_set():
                self.shot_finished.emit(index, "partial")
                break
            self.shot_finished.emit(index, "error" if errors else "done")


# ----------------------------------------------------------------------------
# Widgets
# ----------------------------------------------------------------------------
def local_paths(mime) -> list:
    if not mime.hasUrls():
        return []
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]


class PathEdit(QLineEdit):
    """Path field: accepts dropped folders and shows recent paths like Explorer's address bar.

    A click on the field opens the recent-path list at once. Typing keeps working:
    the list narrows as you type, Esc closes it.
    """
    committed = Signal(str)                 # a dropped folder was put into the field
    opened = Signal()                       # the user clicked the field (show history)

    def __init__(self, placeholder: str = ""):
        super().__init__()
        self.setAcceptDrops(True)
        self.setPlaceholderText(placeholder)
        self._history = QStringListModel(self)
        self._completer = QCompleter(self._history, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.activated[str].connect(self._on_history_picked)
        self.setCompleter(self._completer)
        # Rows of the history list light up under the mouse
        popup = self._completer.popup()
        popup.setMouseTracking(True)
        popup.viewport().setMouseTracking(True)
        popup.setStyleSheet(
            "QListView::item:hover, QListView::item:selected {"
            " background-color: #3b82f6; color: white; }")

    def set_history(self, paths):
        self._history.setStringList(list(paths))

    def show_history(self):
        """Open the list with every entry (not only the ones matching the text)."""
        if self._history.rowCount() == 0:
            return
        self.setFocus()
        self._completer.setCompletionPrefix("")
        self._completer.complete()

    def _on_history_picked(self, text: str):
        self.setText(text)
        self.committed.emit(text)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.opened.emit()

    DROP_HOVER_STYLE = "QLineEdit { border: 2px solid #3b82f6; background-color: rgba(59, 130, 246, 0.15); }"

    def _set_drop_hover(self, on: bool):
        self.setStyleSheet(self.DROP_HOVER_STYLE if on else "")

    def dragEnterEvent(self, event):
        if local_paths(event.mimeData()):
            event.acceptProposedAction()
            self._set_drop_hover(True)      # highlight the box while a folder hovers over it
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dragLeaveEvent(self, event):
        self._set_drop_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_drop_hover(False)
        paths = local_paths(event.mimeData())
        if paths:
            p = paths[0]
            self.setText(str(p if p.is_dir() else p.parent))
            self.committed.emit(self.text())
            event.acceptProposedAction()


class ShotTree(QTreeWidget):
    """SHOTS LIST. Accepts dropped folders (new shots) and CSV files (more shots)."""
    dropped = Signal(list)
    deletePressed = Signal()
    emptyClicked = Signal()                 # user clicked the empty-list hint

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self._hint = QLabel("DROP CSV / EXCEL / IMAGE\n(or click to browse, Ctrl+V to paste)", self)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.setStyleSheet(
            "font-size: 20px; font-weight: bold; background: transparent;"
            "border: 2px dashed #7a7f87; border-radius: 6px;")

    def set_hint(self, visible: bool):
        self._hint.setVisible(visible)
        self._place_hint()

    def _place_hint(self):
        self._hint.setGeometry(self.viewport().geometry().adjusted(12, 12, -12, -12))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_hint()

    def mousePressEvent(self, event):
        if self.topLevelItemCount() == 0 and event.button() == Qt.MouseButton.LeftButton:
            self.emptyClicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.deletePressed.emit()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        self.dropped.emit(local_paths(event.mimeData()))
        event.acceptProposedAction()


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Long-term records, kept for later performance analysis
# ----------------------------------------------------------------------------
MAX_RUN_FOLDERS = 100                    # older run folders are deleted


LOG_DIR = Path(__file__).resolve().parent / "run_logs"    # inside the project, easy to clean


def data_dir() -> Path:
    """Per-user data folder (session file). Used for run records only if LOG_DIR is not writable."""
    return session_file().parent


def log_dir() -> Path:
    """Folder for the app log, run records and history: LOG_DIR, else the per-user folder."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        probe = LOG_DIR / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return LOG_DIR
    except OSError:
        return data_dir()


def setup_app_log() -> logging.Logger:
    """Rotating text log of everything shown in the log panel."""
    logger = logging.getLogger("data_kitsu_tools")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            path = log_dir() / "app.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5,
                                          encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
            logger.addHandler(handler)
        except OSError:
            pass                            # logging to file is optional
    return logger


class RunRecorder:
    """One folder per copy run: samples.csv (per second), files.csv (per file),
    summary.json. Each finished run also adds one line to history.csv."""

    SAMPLE_HEADER = ["time", "elapsed_s", "mb_s", "active_pct", "files_done", "threads"]
    FILE_HEADER = ["time", "shot", "file", "bytes", "seconds", "mb_s", "result", "error"]

    def __init__(self, config: dict):
        self.config = dict(config, app_version=APP_VERSION, python=sys.version.split()[0],
                           platform=sys.platform, buffer_mb=BUFFER_SIZE // (1024 * 1024))
        self.t0 = time.time()
        self.dir = log_dir() / "runs" / time.strftime("%Y%m%d_%H%M%S", time.localtime(self.t0))
        self.dir.mkdir(parents=True, exist_ok=True)
        self._samples = open(self.dir / "samples.csv", "w", newline="", encoding="utf-8")
        self._files = open(self.dir / "files.csv", "w", newline="", encoding="utf-8")
        self._sw = csv.writer(self._samples)
        self._sw.writerow(self.SAMPLE_HEADER)
        self._fw = csv.writer(self._files)
        self._fw.writerow(self.FILE_HEADER)
        self.files = 0
        self.bytes = 0
        self.errors = 0
        self.rates: list = []
        self.actives: list = []
        self.shots: list = []
        self._prune()

    @staticmethod
    def _prune():
        runs = sorted((log_dir() / "runs").glob("*"))
        for old in runs[:-MAX_RUN_FOLDERS]:
            shutil.rmtree(old, ignore_errors=True)

    def sample(self, rate: float, active: float, threads: int):
        self.rates.append(rate)
        self.actives.append(active)
        self._sw.writerow([time.strftime("%H:%M:%S"), f"{time.time() - self.t0:.1f}",
                           f"{rate:.2f}", f"{active:.1f}", self.files, threads])
        self._samples.flush()

    def file_done(self, shot: str, src: str, nbytes: int, seconds: float, result: str, error: str):
        self.files += 1
        if error:
            self.errors += 1
        if result == "copied":
            self.bytes += nbytes
        mbps = nbytes / seconds / 1048576 if (result == "copied" and seconds > 0) else 0.0
        self._fw.writerow([time.strftime("%H:%M:%S"), shot, src, nbytes, f"{seconds:.3f}",
                           f"{mbps:.2f}", result, error])
        self._files.flush()

    def shot_done(self, name: str, result: str, seconds: float):
        self.shots.append({"shot": name, "result": result, "seconds": round(seconds, 1)})

    def close(self, outcome: str):
        duration = max(time.time() - self.t0, 1e-6)
        avg_rate = self.bytes / duration / 1048576
        peak = max(self.rates, default=0.0)
        active = sum(self.actives) / len(self.actives) if self.actives else 0.0
        c = self.config
        summary = {
            "outcome": outcome,
            "start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.t0)),
            "duration_s": round(duration, 1), "files": self.files, "errors": self.errors,
            "gb": round(self.bytes / 1024 ** 3, 3), "avg_mb_s": round(avg_rate, 2),
            "peak_mb_s": round(peak, 2), "avg_active_pct": round(active, 1),
            "config": self.config, "shots": self.shots,
        }
        (self.dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
        self._samples.close()
        self._files.close()
        history = log_dir() / "history.csv"
        is_new = not history.exists()
        with open(history, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["start", "outcome", "duration_s", "mode", "threads", "overwrite",
                            "shots", "files", "errors", "gb", "avg_mb_s", "peak_mb_s",
                            "avg_active_pct", "buffer_mb", "copy_from", "paste_to", "run_folder"])
            w.writerow([summary["start"], outcome, summary["duration_s"], c.get("mode", ""),
                        c.get("threads", ""), c.get("overwrite", ""), c.get("shots", ""),
                        self.files, self.errors, summary["gb"], summary["avg_mb_s"],
                        summary["peak_mb_s"], summary["avg_active_pct"], c.get("buffer_mb", ""),
                        c.get("copy_from", ""), c.get("paste_to", ""), self.dir.name])


class StatCell(QFrame):
    """Titled readout with a 60-second history graph, styled like Task Manager."""

    def __init__(self, title: str, fixed_max: float | None = None):
        super().__init__()
        self._title = title
        self._fixed_max = fixed_max
        self._text = "0"
        self._history = deque([0.0] * 60, maxlen=60)
        self.setMinimumHeight(60)
        self.setMinimumWidth(220)

    def push(self, value: float, text: str):
        self._history.append(value)
        self._text = text
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#3a3f47")))
        painter.setBrush(QColor("#1b1d22"))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 4, 4)

        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#9aa0a8"))
        painter.drawText(12, 20, self._title)
        font.setPointSize(14)
        painter.setFont(font)
        painter.setPen(QColor("#e6e6e6"))
        painter.drawText(12, 50, self._text)

        gx0, gx1 = int(self.width() * 0.52), self.width() - 12
        gy0, gy1 = 10, self.height() - 10
        if gx1 > gx0 + 4 and gy1 > gy0 + 4:
            top = self._fixed_max or max(max(self._history) * 1.15, 1.0)
            step = (gx1 - gx0) / (len(self._history) - 1)
            pts = [QPointF(gx0 + i * step, gy1 - min(v, top) / top * (gy1 - gy0))
                   for i, v in enumerate(self._history)]
            fill = QColor(C_BLUE)
            fill.setAlpha(70)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawPolygon(QPolygonF(pts + [QPointF(gx1, gy1), QPointF(gx0, gy1)]))
            line = QPen(QColor(C_BLUE))
            line.setWidthF(1.5)
            painter.setPen(line)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QPolygonF(pts))
        painter.end()


def brand_icon(kind: str, size: int = 36) -> QIcon:
    """Rounded brand-coloured square with a letter (or an envelope for Gmail)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(BRAND_COLORS[kind]))
    painter.drawRoundedRect(0.0, 0.0, float(size), float(size), size * 0.22, size * 0.22)
    if kind == "gmail":
        painter.setBrush(QColor("white"))
        painter.drawRoundedRect(size * 0.2, size * 0.3, size * 0.6, size * 0.44, 2.0, 2.0)
        pen = QPen(QColor(BRAND_COLORS[kind]))
        pen.setWidthF(max(1.5, size / 16))
        painter.setPen(pen)
        painter.drawLine(QPointF(size * 0.2, size * 0.3), QPointF(size * 0.5, size * 0.56))
        painter.drawLine(QPointF(size * 0.8, size * 0.3), QPointF(size * 0.5, size * 0.56))
    else:
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(int(size * 0.5))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, BRAND_LETTERS[kind])
    painter.end()
    return QIcon(pix)


def read_changelog() -> str:
    """Return the 'My Modification update' section of README.md, shown as the change log."""
    readme = Path(__file__).resolve().parent / "README.md"
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError:
        return "The change log could not be read (README.md is missing)."
    match = re.search(r"^## My Modification update[ \t]*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else "No change log found in README.md."


class UpdateWorker(QThread):
    """Runs the GitHub version check or the update in the background, so the window stays responsive."""

    checked = Signal(object)  # latest version string, or None when GitHub cannot be reached
    updated = Signal(bool)    # True when the update succeeded

    def __init__(self, action: str, latest: str | None = None, parent=None):
        super().__init__(parent)
        self._action = action
        self._latest = latest

    def run(self):
        if self._action == "check":
            self.checked.emit(updater.fetch_latest_version())
        else:
            self.updated.emit(updater.do_update(self._latest))


class AboutDialog(QDialog):
    """Info window with two tabs: About me (contacts, donation, update check) and Change log."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Info")
        self._latest = None  # newer version found on GitHub, waiting for "Update now"
        self._worker = None  # running UpdateWorker, kept alive until it finishes

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        # Tab 1: About me
        about_page = QWidget()
        layout = QVBoxLayout(about_page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        name = QLabel(AUTHOR)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(name)
        role = QLabel("VFX Compositor")
        role.setAlignment(Qt.AlignmentFlag.AlignCenter)
        role.setStyleSheet("font-size: 13px;")
        layout.addWidget(role)

        for kind, text, url in CONTACTS:
            row = QHBoxLayout()
            button = QToolButton()
            button.setIcon(brand_icon(kind))
            button.setIconSize(QSize(36, 36))
            button.setAutoRaise(True)
            button.setToolTip(url)
            button.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            link = QLabel(f'<a href="{url}">{text}</a>')
            link.setOpenExternalLinks(True)
            row.addWidget(button)
            row.addWidget(link, 1)
            layout.addLayout(row)

        layout.addSpacing(10)
        donate = QLabel("Donate: scan with the Binance app")
        donate.setAlignment(Qt.AlignmentFlag.AlignCenter)
        donate.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(donate)
        qr = QLabel()
        qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap(str(DONATE_QR))
        if pix.isNull():
            qr.setText(f"QR image not found:\n{DONATE_QR}")
        else:
            qr.setPixmap(pix.scaled(QSize(420, 420), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(qr)

        wallet_title = QLabel("Wallet code (BSC network):")
        wallet_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wallet_title.setStyleSheet("font-size: 13px; font-weight: bold;")
        layout.addWidget(wallet_title)
        wallet_row = QHBoxLayout()
        wallet = QLineEdit(WALLET_BSC)
        wallet.setReadOnly(True)
        wallet.setCursorPosition(0)
        copy_button = QPushButton("Copy")
        copy_button.setFixedWidth(90)

        def copy_wallet():
            QApplication.clipboard().setText(WALLET_BSC)
            copy_button.setText("Copied")

        copy_button.clicked.connect(copy_wallet)
        wallet_row.addWidget(wallet, 1)
        wallet_row.addWidget(copy_button)
        layout.addLayout(wallet_row)

        layout.addSpacing(10)
        update_row = QHBoxLayout()
        self.update_status = QLabel(f"Current version: {APP_VERSION}")
        self.update_status.setWordWrap(True)
        self.update_button = QPushButton("Check for update")
        self.update_button.setMinimumWidth(160)
        self.update_button.clicked.connect(self._on_update_button)
        update_row.addWidget(self.update_status, 1)
        update_row.addWidget(self.update_button)
        layout.addLayout(update_row)
        layout.addStretch(1)
        tabs.addTab(about_page, "About me")

        # Tab 2: Change log (the README section, rendered as Markdown)
        changelog = QTextBrowser()
        changelog.setOpenExternalLinks(True)
        changelog.setMarkdown(read_changelog())
        tabs.addTab(changelog, "Change log")

        bottom = QHBoxLayout()
        logs = QPushButton("Open log folder")
        logs.clicked.connect(self._open_logs)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(logs)
        bottom.addStretch(1)
        bottom.addWidget(close)
        outer.addLayout(bottom)

    def done(self, result):
        # Do not close while a background check or update is still running.
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait()
        super().done(result)

    def _on_update_button(self):
        if self._latest:
            answer = QMessageBox.question(
                self, APP_NAME,
                f"Update to version {self._latest}?\nRestart the app after the update finishes.")
            if answer == QMessageBox.StandardButton.Yes:
                self._run_worker("update")
        else:
            self._run_worker("check")

    def _run_worker(self, action: str):
        self.update_button.setEnabled(False)
        self.update_status.setText("Checking GitHub..." if action == "check" else "Updating...")
        self._worker = UpdateWorker(action, self._latest, self)
        self._worker.checked.connect(self._on_checked)
        self._worker.updated.connect(self._on_updated)
        self._worker.start()

    def _on_checked(self, latest):
        self.update_button.setEnabled(True)
        if latest is None:
            self.update_status.setText("Cannot reach GitHub. Check your connection and try again.")
        elif updater.parse_version(latest) <= updater.parse_version(APP_VERSION):
            self.update_status.setText(f"You have the latest version ({APP_VERSION}).")
        else:
            self._latest = latest
            self.update_status.setText(f"Version {latest} is available (you have {APP_VERSION}).")
            self.update_button.setText("Update now")

    def _on_updated(self, ok: bool):
        if ok:
            self.update_status.setText(f"Updated to {self._latest}. Restart Data Kitsu Tools to use it.")
            self.update_button.setText("Updated")  # stays disabled
        else:
            self.update_status.setText("Update failed. Check your connection, or update manually from GitHub.")
            self.update_button.setEnabled(True)

    @staticmethod
    def _open_logs():
        try:
            log_dir().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))


class KitsuWorker(QThread):
    """Run Kitsu API requests off the GUI thread."""

    finished_ok = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, client: kitsu_client.KitsuClient, action: str, params: dict | None = None):
        super().__init__()
        self.client = client
        self.action = action
        self.params = params or {}

    def run(self):
        try:
            if self.action == "login":
                url = self.params["url"]
                verify_ssl = self.params.get("verify_ssl", True)
                self.client.set_host(url, verify_ssl=verify_ssl)
                if not self.client.validate_url():
                    raise kitsu_client.KitsuConnectionError(
                        f"Could not find a Kitsu server at {self.client.api_url}."
                    )
                if self.params.get("auth_mode") == "token":
                    self.client.set_token(self.params.get("token", ""))
                    user = self.client.get_current_user()
                    if not user:
                        self.client.set_token(None)
                        raise kitsu_client.KitsuAuthError("Kitsu API token is invalid or expired.")
                else:
                    self.client.login(
                        self.params.get("email", ""),
                        self.params.get("password", ""),
                        totp=self.params.get("totp") or None,
                    )
                    user = self.client.get_current_user() or self.client.user
                projects = self.client.get_projects(open_only=self.params.get("open_only", True))
                self.finished_ok.emit(self.action, {"user": user, "projects": projects})

            elif self.action == "restore":
                self.client.set_host(
                    self.params["url"],
                    verify_ssl=self.params.get("verify_ssl", True),
                )
                self.client.set_token(self.params.get("tokens"))
                user = self.client.get_current_user()
                if not user:
                    self.client.set_token(None)
                    raise kitsu_client.KitsuAuthError("Saved Kitsu session expired.")
                projects = self.client.get_projects(open_only=self.params.get("open_only", True))
                self.finished_ok.emit(self.action, {"user": user, "projects": projects})

            elif self.action == "projects":
                projects = self.client.get_projects(open_only=self.params.get("open_only", True))
                self.finished_ok.emit(self.action, {"projects": projects})

            elif self.action == "hierarchy":
                project_id = self.params["project_id"]
                episodes = self.client.get_episodes(project_id)
                sequences = self.client.get_sequences(project_id)
                self.finished_ok.emit(
                    self.action,
                    {"project_id": project_id, "episodes": episodes, "sequences": sequences},
                )

            elif self.action == "shots":
                shots = self.client.get_shots(
                    self.params["project_id"],
                    episode_id=self.params.get("episode_id"),
                    sequence_id=self.params.get("sequence_id"),
                )
                self.finished_ok.emit(self.action, {"shots": shots})

        except Exception as exc:
            self.failed.emit(self.action, str(exc))


class KitsuDialog(QDialog):
    """Connect to a Kitsu server, browse projects/episodes/sequences, and import shots."""

    import_requested = Signal(list, bool, str, str)   # (records, replace, prj_name, prj_id)

    def __init__(self, client: kitsu_client.KitsuClient, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fetch Shots from Kitsu")
        self.resize(1040, 720)
        self.client = client
        self.config = dict(config)
        self._worker: KitsuWorker | None = None
        self._projects: list[dict] = []
        self._episodes: list[dict] = []
        self._sequences: list[dict] = []
        self._records: list[dict] = []
        self._build_ui()
        self._load_config()
        if self.client.user and self.client.access_token:
            self._set_connected_state(self.client.user)
            self._start_worker("projects", {"open_only": not self.chk_closed.isChecked()})
        elif self.config.get("url") and self.config.get("session_tokens"):
            self._start_worker(
                "restore",
                {
                    "url": self.config["url"],
                    "verify_ssl": self.chk_ssl.isChecked(),
                    "tokens": self.config["session_tokens"],
                    "open_only": not self.chk_closed.isChecked(),
                },
            )

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 1. Server & Authentication
        auth_box = QGroupBox("Kitsu Connection")
        ag = QGridLayout(auth_box)

        ag.addWidget(QLabel("Server URL"), 0, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://kitsu.studio.com")
        ag.addWidget(self.url_edit, 0, 1, 1, 3)

        self.chk_ssl = QCheckBox("Verify SSL")
        self.chk_ssl.setChecked(True)
        self.chk_ssl.setToolTip("Uncheck for self-signed studio Kitsu HTTPS certificates.")
        ag.addWidget(self.chk_ssl, 0, 4)

        mode_row = QHBoxLayout()
        self.rb_password = QRadioButton("Email & Password")
        self.rb_token = QRadioButton("API Token")
        self.rb_password.setChecked(True)
        self.rb_password.toggled.connect(self._update_auth_fields)
        mode_row.addWidget(self.rb_password)
        mode_row.addWidget(self.rb_token)
        mode_row.addStretch(1)
        ag.addLayout(mode_row, 1, 1, 1, 4)

        self.lbl_email = QLabel("Email")
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("user@studio.com")
        self.lbl_pass = QLabel("Password")
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.lbl_totp = QLabel("2FA Code")
        self.totp_edit = QLineEdit()
        self.totp_edit.setPlaceholderText("Optional")
        self.totp_edit.setMaximumWidth(100)

        ag.addWidget(self.lbl_email, 2, 0)
        ag.addWidget(self.email_edit, 2, 1)
        ag.addWidget(self.lbl_pass, 2, 2)
        ag.addWidget(self.pass_edit, 2, 3)

        totp_row = QHBoxLayout()
        totp_row.addWidget(self.lbl_totp)
        totp_row.addWidget(self.totp_edit)
        ag.addLayout(totp_row, 2, 4)

        self.lbl_token = QLabel("API Token")
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("Paste Kitsu access token")
        ag.addWidget(self.lbl_token, 3, 0)
        ag.addWidget(self.token_edit, 3, 1, 1, 4)

        btn_row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect / Sign In")
        self.btn_connect.clicked.connect(self._on_connect)
        self.btn_logout = QPushButton("Sign Out")
        self.btn_logout.clicked.connect(self._on_logout)
        self.btn_logout.setEnabled(False)
        self.status_lbl = QLabel("Not connected")
        self.status_lbl.setStyleSheet("color: #8a8f98;")
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_logout)
        btn_row.addSpacing(12)
        btn_row.addWidget(self.status_lbl, 1)
        ag.addLayout(btn_row, 4, 0, 1, 5)

        layout.addWidget(auth_box)

        # 2. Project / Episode / Sequence Filter
        filter_box = QGroupBox("Project & Shots")
        fg = QGridLayout(filter_box)

        fg.addWidget(QLabel("Project"), 0, 0)
        self.cmb_project = QComboBox()
        self.cmb_project.currentIndexChanged.connect(self._on_project_changed)
        fg.addWidget(self.cmb_project, 0, 1, 1, 2)

        self.chk_closed = QCheckBox("Include closed")
        self.chk_closed.toggled.connect(self._on_refresh_projects)
        fg.addWidget(self.chk_closed, 0, 3)

        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._on_refresh_projects)
        fg.addWidget(self.btn_refresh, 0, 4)

        fg.addWidget(QLabel("Episode"), 1, 0)
        self.cmb_episode = QComboBox()
        self.cmb_episode.currentIndexChanged.connect(self._on_episode_changed)
        fg.addWidget(self.cmb_episode, 1, 1)

        fg.addWidget(QLabel("Sequence"), 1, 2)
        self.cmb_sequence = QComboBox()
        fg.addWidget(self.cmb_sequence, 1, 3)

        self.btn_fetch = QPushButton("Fetch Shots")
        self.btn_fetch.setStyleSheet("font-weight: bold;")
        self.btn_fetch.clicked.connect(self._on_fetch_shots)
        fg.addWidget(self.btn_fetch, 1, 4)

        fg.addWidget(QLabel("Search"), 2, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter shots by name, sequence, episode or artist...")
        self.search_edit.textChanged.connect(self._apply_filter)
        fg.addWidget(self.search_edit, 2, 1, 1, 4)

        # Task & Assignment filters (Assignations, Prepare, Compositing, Other columns)
        fg.addWidget(QLabel("Assignations"), 3, 0)
        self.cmb_assignee = QComboBox()
        self.cmb_assignee.addItem("All Assignees", "")
        self.cmb_assignee.currentIndexChanged.connect(self._apply_filter)
        fg.addWidget(self.cmb_assignee, 3, 1)

        fg.addWidget(QLabel("Prepare"), 3, 2)
        self.cmb_prepare = QComboBox()
        self.cmb_prepare.addItem("All", "")
        self.cmb_prepare.currentIndexChanged.connect(self._apply_filter)
        fg.addWidget(self.cmb_prepare, 3, 3)

        comp_row = QHBoxLayout()
        comp_row.addWidget(QLabel("Compositing"))
        self.cmb_compositing = QComboBox()
        self.cmb_compositing.addItem("All", "")
        self.cmb_compositing.currentIndexChanged.connect(self._apply_filter)
        comp_row.addWidget(self.cmb_compositing, 1)
        fg.addLayout(comp_row, 3, 4)

        fg.addWidget(QLabel("Other Column"), 4, 0)
        self.cmb_other_col = QComboBox()
        self.cmb_other_col.addItem("Any Column", "")
        self.cmb_other_col.currentIndexChanged.connect(self._on_other_col_changed)
        fg.addWidget(self.cmb_other_col, 4, 1)

        fg.addWidget(QLabel("Column Status"), 4, 2)
        self.cmb_other_status = QComboBox()
        self.cmb_other_status.addItem("All", "")
        self.cmb_other_status.currentIndexChanged.connect(self._apply_filter)
        fg.addWidget(self.cmb_other_status, 4, 3, 1, 2)

        layout.addWidget(filter_box)

        # 3. Shots Preview Tree
        self.shot_tree = QTreeWidget()
        self.shot_tree.setHeaderLabels([
            "SHOT", "EPISODE", "SEQUENCE", "FRAMES",
            "ASSIGNATIONS", "PREPARE", "COMPOSITING", "INFO",
        ])
        self.shot_tree.setRootIsDecorated(False)
        self.shot_tree.setUniformRowHeights(True)
        self.shot_tree.itemChanged.connect(self._update_selected_count)
        hdr = self.shot_tree.header()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.shot_tree.setColumnWidth(1, 90)
        self.shot_tree.setColumnWidth(2, 100)
        self.shot_tree.setColumnWidth(3, 65)
        self.shot_tree.setColumnWidth(4, 150)
        self.shot_tree.setColumnWidth(5, 105)
        self.shot_tree.setColumnWidth(6, 105)
        self.shot_tree.setColumnWidth(7, 120)
        layout.addWidget(self.shot_tree, 1)

        # 4. Bottom selection + import buttons
        bottom = QHBoxLayout()
        self.btn_sel_all = QPushButton("Select All")
        self.btn_sel_none = QPushButton("Select None")
        self.btn_sel_vis = QPushButton("Select Visible")
        self.btn_sel_all.clicked.connect(lambda: self._set_checks(True, visible_only=False))
        self.btn_sel_none.clicked.connect(lambda: self._set_checks(False, visible_only=False))
        self.btn_sel_vis.clicked.connect(lambda: self._set_checks(True, visible_only=True))
        self.count_lbl = QLabel("0 / 0 selected")
        bottom.addWidget(self.btn_sel_all)
        bottom.addWidget(self.btn_sel_none)
        bottom.addWidget(self.btn_sel_vis)
        bottom.addSpacing(8)
        bottom.addWidget(self.count_lbl)
        bottom.addStretch(1)

        self.btn_append = QPushButton("Append to Shot List")
        self.btn_replace = QPushButton("Replace Shot List")
        self.btn_replace.setStyleSheet("font-weight: bold;")
        self.btn_close = QPushButton("Close")
        self.btn_append.clicked.connect(lambda: self._emit_import(replace=False))
        self.btn_replace.clicked.connect(lambda: self._emit_import(replace=True))
        self.btn_close.clicked.connect(self.accept)
        bottom.addWidget(self.btn_append)
        bottom.addWidget(self.btn_replace)
        bottom.addWidget(self.btn_close)
        layout.addLayout(bottom)

        self._update_auth_fields()
        self._set_project_controls_enabled(False)

    def _load_config(self):
        self.url_edit.setText(self.config.get("url", ""))
        self.email_edit.setText(self.config.get("email", ""))
        self.chk_ssl.setChecked(bool(self.config.get("verify_ssl", True)))
        if self.config.get("auth_mode") == "token":
            self.rb_token.setChecked(True)
        else:
            self.rb_password.setChecked(True)

    def export_config(self) -> dict:
        tokens = self.client.get_tokens()
        return {
            "url": self.url_edit.text().strip(),
            "email": self.email_edit.text().strip(),
            "verify_ssl": self.chk_ssl.isChecked(),
            "auth_mode": "token" if self.rb_token.isChecked() else "password",
            "session_tokens": tokens if tokens.get("access_token") else None,
            "project_id": self.cmb_project.currentData() or self.config.get("project_id", ""),
            "episode_id": self.cmb_episode.currentData() or "",
            "sequence_id": self.cmb_sequence.currentData() or "",
        }

    def _update_auth_fields(self):
        is_token = self.rb_token.isChecked()
        for w in (self.lbl_email, self.email_edit, self.lbl_pass, self.pass_edit,
                  self.lbl_totp, self.totp_edit):
            w.setVisible(not is_token)
        self.lbl_token.setVisible(is_token)
        self.token_edit.setVisible(is_token)

    def _set_project_controls_enabled(self, enabled: bool):
        for w in (self.cmb_project, self.chk_closed, self.btn_refresh,
                  self.cmb_episode, self.cmb_sequence, self.btn_fetch):
            w.setEnabled(enabled)

    def _set_busy(self, busy: bool, msg: str = ""):
        self.btn_connect.setEnabled(not busy)
        self.btn_fetch.setEnabled(not busy and self.cmb_project.count() > 0)
        self.btn_refresh.setEnabled(not busy and bool(self.client.access_token))
        if msg:
            self.status_lbl.setText(msg)
            self.status_lbl.setStyleSheet("color: #3aa0ff;")

    def _set_connected_state(self, user: dict | None):
        name = ""
        if isinstance(user, dict):
            name = user.get("full_name") or user.get("email") or "User"
        self.status_lbl.setText(f"Connected as {name}" if name else "Connected")
        self.status_lbl.setStyleSheet("color: #5cd67d; font-weight: bold;")
        self.btn_logout.setEnabled(True)
        self._set_project_controls_enabled(True)

    def _start_worker(self, action: str, params: dict | None = None):
        if self._worker is not None and self._worker.isRunning():
            return
        self._set_busy(True, f"Kitsu: {action}...")
        self._worker = KitsuWorker(self.client, action, params)
        self._worker.finished_ok.connect(self._on_worker_ok)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.start()

    def _on_connect(self):
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, APP_NAME, "Enter the Kitsu server URL first.")
            return
        if self.rb_token.isChecked():
            token = self.token_edit.text().strip()
            if not token:
                QMessageBox.warning(self, APP_NAME, "Enter your Kitsu API token.")
                return
            params = {
                "url": url,
                "verify_ssl": self.chk_ssl.isChecked(),
                "auth_mode": "token",
                "token": token,
                "open_only": not self.chk_closed.isChecked(),
            }
        else:
            email = self.email_edit.text().strip()
            password = self.pass_edit.text()
            if not email or not password:
                QMessageBox.warning(self, APP_NAME, "Enter both Kitsu email and password.")
                return
            params = {
                "url": url,
                "verify_ssl": self.chk_ssl.isChecked(),
                "auth_mode": "password",
                "email": email,
                "password": password,
                "totp": self.totp_edit.text().strip(),
                "open_only": not self.chk_closed.isChecked(),
            }
        self._start_worker("login", params)

    def _on_logout(self):
        self.client.logout()
        self.config["session_tokens"] = None
        self._projects = []
        self._episodes = []
        self._sequences = []
        self._records = []
        self.cmb_project.clear()
        self.cmb_episode.clear()
        self.cmb_sequence.clear()
        self.shot_tree.clear()
        self._update_selected_count()
        self.btn_logout.setEnabled(False)
        self._set_project_controls_enabled(False)
        self.status_lbl.setText("Signed out")
        self.status_lbl.setStyleSheet("color: #8a8f98;")

    def _on_refresh_projects(self):
        if not self.client.access_token:
            return
        self._start_worker("projects", {"open_only": not self.chk_closed.isChecked()})

    def _on_project_changed(self):
        project_id = self.cmb_project.currentData()
        self.cmb_episode.blockSignals(True)
        self.cmb_episode.clear()
        self.cmb_episode.blockSignals(False)
        self.cmb_sequence.clear()
        if project_id:
            self._start_worker("hierarchy", {"project_id": project_id})

    def _on_episode_changed(self):
        ep_id = self.cmb_episode.currentData()
        self.cmb_sequence.clear()
        self.cmb_sequence.addItem("All Sequences", "")
        for seq in self._sequences:
            if ep_id and seq.get("parent_id") != ep_id:
                continue
            self.cmb_sequence.addItem(seq.get("name", ""), seq.get("id", ""))

    def _on_fetch_shots(self):
        project_id = self.cmb_project.currentData()
        if not project_id:
            return
        self._start_worker(
            "shots",
            {
                "project_id": project_id,
                "episode_id": self.cmb_episode.currentData() or None,
                "sequence_id": self.cmb_sequence.currentData() or None,
            },
        )

    def _on_worker_ok(self, action: str, payload: dict):
        self._set_busy(False)
        if action in ("login", "restore"):
            self._set_connected_state(payload.get("user"))
            self._populate_projects(payload.get("projects", []))
        elif action == "projects":
            self._set_connected_state(self.client.user)
            self._populate_projects(payload.get("projects", []))
        elif action == "hierarchy":
            self._set_connected_state(self.client.user)
            self._episodes = payload.get("episodes", [])
            self._sequences = payload.get("sequences", [])
            self.cmb_episode.blockSignals(True)
            self.cmb_episode.clear()
            self.cmb_episode.addItem("All Episodes", "")
            for ep in self._episodes:
                self.cmb_episode.addItem(ep.get("name", ""), ep.get("id", ""))
            self.cmb_episode.setEnabled(bool(self._episodes))
            saved_ep = self.config.get("episode_id", "")
            if saved_ep:
                idx = self.cmb_episode.findData(saved_ep)
                if idx >= 0:
                    self.cmb_episode.setCurrentIndex(idx)
            self.cmb_episode.blockSignals(False)
            self._on_episode_changed()
            saved_seq = self.config.get("sequence_id", "")
            if saved_seq:
                idx = self.cmb_sequence.findData(saved_seq)
                if idx >= 0:
                    self.cmb_sequence.setCurrentIndex(idx)
        elif action == "shots":
            self._set_connected_state(self.client.user)
            self._records = payload.get("shots", [])
            self._populate_shots()

    def _on_worker_failed(self, action: str, message: str):
        self._set_busy(False)
        if action == "restore":
            self.status_lbl.setText("Saved session expired — please sign in")
            self.status_lbl.setStyleSheet("color: #ff9f43;")
            return
        self.status_lbl.setText(message)
        self.status_lbl.setStyleSheet("color: #ff4d4d;")

    def _populate_projects(self, projects: list[dict]):
        self._projects = projects
        self.cmb_project.blockSignals(True)
        self.cmb_project.clear()
        for prj in projects:
            self.cmb_project.addItem(prj.get("name", ""), prj.get("id", ""))
        saved_id = self.config.get("project_id", "")
        if saved_id:
            idx = self.cmb_project.findData(saved_id)
            if idx >= 0:
                self.cmb_project.setCurrentIndex(idx)
        self.cmb_project.blockSignals(False)
        self._on_project_changed()

    @staticmethod
    def _task_status_for(rec: dict, col_name: str) -> str:
        tasks = rec.get("tasks") or {}
        wanted = col_name.lower()
        for tname, tinfo in tasks.items():
            if tname.lower() == wanted:
                return str(tinfo.get("status") or tinfo.get("status_short") or "")
        return ""

    def _populate_task_filters(self):
        assignees: set[str] = set()
        prep_statuses: set[str] = set()
        comp_statuses: set[str] = set()
        other_cols: set[str] = set()

        for rec in self._records:
            for a in rec.get("assignees") or []:
                if a:
                    assignees.add(str(a))
            for tname, tinfo in (rec.get("tasks") or {}).items():
                st = str(tinfo.get("status") or tinfo.get("status_short") or "").strip()
                low = tname.lower()
                if low == "prepare" and st:
                    prep_statuses.add(st)
                elif low == "compositing" and st:
                    comp_statuses.add(st)
                else:
                    other_cols.add(tname)

        def _refill(cmb: QComboBox, items: list[tuple[str, str]]):
            prev = cmb.currentData()
            cmb.blockSignals(True)
            cmb.clear()
            for label, val in items:
                cmb.addItem(label, val)
            if prev:
                idx = cmb.findData(prev)
                if idx >= 0:
                    cmb.setCurrentIndex(idx)
            cmb.blockSignals(False)

        assignee_items = [("All Assignees", ""), ("Assigned to Me", "__ME__"), ("Unassigned", "__NONE__")]
        assignee_items.extend((a, a) for a in sorted(assignees, key=str.lower))
        _refill(self.cmb_assignee, assignee_items)
        _refill(self.cmb_prepare, [("All", "")] + [(s, s) for s in sorted(prep_statuses, key=str.lower)])
        _refill(self.cmb_compositing, [("All", "")] + [(s, s) for s in sorted(comp_statuses, key=str.lower)])
        _refill(self.cmb_other_col, [("Any Column", "")] + [(c, c) for c in sorted(other_cols, key=str.lower)])
        self._on_other_col_changed()

    def _on_other_col_changed(self):
        col = self.cmb_other_col.currentData() or ""
        statuses: set[str] = set()
        for rec in self._records:
            if col:
                st = self._task_status_for(rec, col)
                if st:
                    statuses.add(st)
            else:
                for tname, tinfo in (rec.get("tasks") or {}).items():
                    if tname.lower() not in ("prepare", "compositing"):
                        st = str(tinfo.get("status") or "").strip()
                        if st:
                            statuses.add(st)
        self.cmb_other_status.blockSignals(True)
        self.cmb_other_status.clear()
        self.cmb_other_status.addItem("All", "")
        for s in sorted(statuses, key=str.lower):
            self.cmb_other_status.addItem(s, s)
        self.cmb_other_status.blockSignals(False)
        self._apply_filter()

    def _populate_shots(self):
        self._populate_task_filters()
        self.shot_tree.blockSignals(True)
        self.shot_tree.clear()
        for idx, rec in enumerate(self._records):
            frames = rec.get("frames")
            info_parts = []
            if rec.get("fps"):
                info_parts.append(f"{rec['fps']} fps")
            if rec.get("resolution"):
                info_parts.append(str(rec["resolution"]))
            assignees_text = ", ".join(rec.get("assignees") or [])
            prep_text = self._task_status_for(rec, "Prepare")
            comp_text = self._task_status_for(rec, "Compositing")
            item = QTreeWidgetItem([
                rec.get("name", ""),
                rec.get("episode", ""),
                rec.get("sequence", ""),
                frames_text(frames),
                assignees_text,
                prep_text,
                comp_text,
                "  ".join(info_parts),
            ])
            other_tip = [
                f"{tname}: {tinfo.get('status', '')}"
                for tname, tinfo in sorted((rec.get("tasks") or {}).items())
            ]
            if other_tip:
                item.setToolTip(0, "\n".join(other_tip))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, ROLE_INDEX, idx)
            self.shot_tree.addTopLevelItem(item)
        self.shot_tree.blockSignals(False)
        self._apply_filter()
        self._update_selected_count()

    def _apply_filter(self):
        needle = self.search_edit.text().strip().lower()
        assignee_flt = self.cmb_assignee.currentData() or ""
        prep_flt = self.cmb_prepare.currentData() or ""
        comp_flt = self.cmb_compositing.currentData() or ""
        other_col = self.cmb_other_col.currentData() or ""
        other_status = self.cmb_other_status.currentData() or ""
        me_id = str((self.client.user or {}).get("id") or "")
        me_name = str((self.client.user or {}).get("full_name") or "").lower()

        for i in range(self.shot_tree.topLevelItemCount()):
            item = self.shot_tree.topLevelItem(i)
            idx = item.data(0, ROLE_INDEX)
            rec = self._records[idx] if isinstance(idx, int) and idx < len(self._records) else {}
            hidden = False

            if needle:
                haystack = " ".join(item.text(c).lower() for c in (0, 1, 2, 4, 5, 6))
                if needle not in haystack:
                    hidden = True

            if not hidden and assignee_flt:
                rec_assignees = rec.get("assignees") or []
                rec_ids = rec.get("assignee_ids") or []
                if assignee_flt == "__NONE__":
                    hidden = bool(rec_assignees)
                elif assignee_flt == "__ME__":
                    hidden = not (
                        (me_id and me_id in rec_ids)
                        or (me_name and any(a.lower() == me_name for a in rec_assignees))
                    )
                else:
                    hidden = assignee_flt not in rec_assignees

            if not hidden and prep_flt:
                hidden = self._task_status_for(rec, "Prepare") != prep_flt

            if not hidden and comp_flt:
                hidden = self._task_status_for(rec, "Compositing") != comp_flt

            if not hidden and (other_col or other_status):
                tasks = rec.get("tasks") or {}
                if other_col:
                    st = self._task_status_for(rec, other_col)
                    hidden = (st != other_status) if other_status else (other_col not in tasks)
                else:
                    hidden = not any(
                        tname.lower() not in ("prepare", "compositing")
                        and str(tinfo.get("status") or "") == other_status
                        for tname, tinfo in tasks.items()
                    )

            item.setHidden(hidden)

        # Auto-sync selection to visible (filtered) shots so hidden shots are not imported
        self.shot_tree.blockSignals(True)
        for i in range(self.shot_tree.topLevelItemCount()):
            it = self.shot_tree.topLevelItem(i)
            it.setCheckState(0, Qt.CheckState.Unchecked if it.isHidden() else Qt.CheckState.Checked)
        self.shot_tree.blockSignals(False)
        self._update_selected_count()

    def _set_checks(self, checked: bool, visible_only: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.shot_tree.blockSignals(True)
        for i in range(self.shot_tree.topLevelItemCount()):
            item = self.shot_tree.topLevelItem(i)
            if visible_only and item.isHidden():
                continue
            item.setCheckState(0, state)
        self.shot_tree.blockSignals(False)
        self._update_selected_count()

    def _update_selected_count(self, *_):
        total = self.shot_tree.topLevelItemCount()
        visible = 0
        selected = 0
        for i in range(total):
            it = self.shot_tree.topLevelItem(i)
            if not it.isHidden():
                visible += 1
            if it.checkState(0) == Qt.CheckState.Checked:
                selected += 1
        if visible < total:
            self.count_lbl.setText(f"{selected} / {visible} filtered ({total} total)")
        else:
            self.count_lbl.setText(f"{selected} / {total} selected")
        self.btn_append.setEnabled(selected > 0)
        self.btn_replace.setEnabled(selected > 0)

    def _emit_import(self, replace: bool):
        chosen = []
        for i in range(self.shot_tree.topLevelItemCount()):
            item = self.shot_tree.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                idx = item.data(0, ROLE_INDEX)
                if isinstance(idx, int) and 0 <= idx < len(self._records):
                    chosen.append(self._records[idx])
        if not chosen:
            return
        prj_name = self.cmb_project.currentText() or "Kitsu"
        prj_id = str(self.cmb_project.currentData() or "")
        self.import_requested.emit(chosen, replace, prj_name, prj_id)
        self.accept()


class KitsuPublishWorker(QThread):
    """Update a shot's Compositing status to ĐợiCheck + post path & frame-1 JPG."""

    done = Signal(str, str)     # shot_name, log_message
    failed = Signal(str, str)   # shot_name, error_message

    def __init__(
        self,
        client: kitsu_client.KitsuClient,
        project_id: str,
        shot: Shot,
        dest_path: str,
        source_files: list[Path],
    ):
        super().__init__()
        self.client = client
        self.project_id = project_id
        self.shot = shot
        self.dest_path = dest_path
        self.source_files = source_files

    def run(self):
        tmp_jpg: Path | None = None
        try:
            shot_id = self.shot.kitsu_id
            if not shot_id:
                for rec in self.client.get_shots(self.project_id, include_tasks=False):
                    converted = shots_from_kitsu([rec])
                    if rec.get("name") == self.shot.name or (converted and converted[0].name == self.shot.name):
                        shot_id = str(rec.get("id") or "")
                        break
            if not shot_id:
                raise kitsu_client.KitsuError("Shot not found in Kitsu project.")

            with tempfile.TemporaryDirectory(prefix="dkt_thumb_") as td:
                candidate = Path(td) / f"{self.shot.name}_frame1.jpg"
                tmp_jpg = shot_import.capture_first_frame_jpg(self.source_files, candidate)
                self.client.update_shot_task_on_publish(
                    project_id=self.project_id,
                    shot_id=shot_id,
                    dest_path=self.dest_path,
                    task_type_name="Compositing",
                    status_name="ĐợiCheck",
                    attachment_path=tmp_jpg,
                )
            attach_note = " + frame 1 JPG" if tmp_jpg else ""
            self.done.emit(
                self.shot.name,
                f"Kitsu updated: {self.shot.name} Compositing -> ĐợiCheck (comment path{attach_note})",
            )
        except Exception as exc:
            self.failed.emit(self.shot.name, f"Kitsu update failed for {self.shot.name}: {exc}")


class ShotImportDialog(QDialog):
    """Interactive column picker when auto-detection cannot identify the shot column."""

    def __init__(self, rows: list[list[str]], detected: dict, source_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Select Columns to Import — {source_name}")
        self.resize(960, 620)
        self.rows = rows
        self.detected = detected
        self.chosen_shots: list = []
        self.replace_mode = True
        self._build_ui()
        self._refresh_preview()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        info = QLabel(
            "Could not auto-detect the Shot Name column with full confidence. "
            "Choose the columns below and verify the preview before importing:"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #ff9f43; font-weight: bold;")
        layout.addWidget(info)

        num_cols = max((len(r) for r in self.rows), default=0)
        hdr_row = self.detected.get("header_row")
        col_labels = []
        for c in range(num_cols):
            sample = ""
            if hdr_row is not None and hdr_row < len(self.rows) and c < len(self.rows[hdr_row]):
                sample = self.rows[hdr_row][c].strip()
            if not sample:
                sample = next((r[c].strip() for r in self.rows[:5] if c < len(r) and r[c].strip()), "")
            col_labels.append(f"Col {c + 1}: {sample[:28]}" if sample else f"Col {c + 1}")

        grid = QGridLayout()

        def _make_col_combo(allow_none: bool, default_idx: int) -> QComboBox:
            cmb = QComboBox()
            if allow_none:
                cmb.addItem("(None)", -1)
            for idx, lbl in enumerate(col_labels):
                cmb.addItem(lbl, idx)
            pos = cmb.findData(default_idx)
            if pos >= 0:
                cmb.setCurrentIndex(pos)
            cmb.currentIndexChanged.connect(self._refresh_preview)
            return cmb

        self.cmb_name = _make_col_combo(False, self.detected.get("name_col", 0))
        self.cmb_seq = _make_col_combo(True, self.detected.get("seq_col", -1))
        self.cmb_frames = _make_col_combo(True, self.detected.get("frames_col", -1))
        self.cmb_start = _make_col_combo(True, self.detected.get("start_col", -1))
        self.cmb_end = _make_col_combo(True, self.detected.get("end_col", -1))
        self.spin_header = QSpinBox()
        self.spin_header.setRange(-1, max(0, len(self.rows) - 1))
        self.spin_header.setValue(-1 if hdr_row is None else hdr_row)
        self.spin_header.setSpecialValueText("No header")
        self.spin_header.valueChanged.connect(self._refresh_preview)

        grid.addWidget(QLabel("Shot Name Col *"), 0, 0)
        grid.addWidget(self.cmb_name, 0, 1)
        grid.addWidget(QLabel("Sequence Col"), 0, 2)
        grid.addWidget(self.cmb_seq, 0, 3)
        grid.addWidget(QLabel("Header Row"), 0, 4)
        grid.addWidget(self.spin_header, 0, 5)

        grid.addWidget(QLabel("Frames / Duration Col"), 1, 0)
        grid.addWidget(self.cmb_frames, 1, 1)
        grid.addWidget(QLabel("Start Frame Col"), 1, 2)
        grid.addWidget(self.cmb_start, 1, 3)
        grid.addWidget(QLabel("End Frame Col"), 1, 4)
        grid.addWidget(self.cmb_end, 1, 5)
        layout.addLayout(grid)

        self.table = QTableWidget(len(self.rows), num_cols)
        self.table.setHorizontalHeaderLabels(col_labels)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r_idx, row in enumerate(self.rows):
            for c_idx in range(num_cols):
                val = row[c_idx] if c_idx < len(row) else ""
                self.table.setItem(r_idx, c_idx, QTableWidgetItem(val))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)

        self.preview_lbl = QLabel()
        self.preview_lbl.setWordWrap(True)
        layout.addWidget(self.preview_lbl)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.btn_append = QPushButton("Append to Shot List")
        self.btn_replace = QPushButton("Replace Shot List")
        self.btn_replace.setStyleSheet("font-weight: bold;")
        btn_cancel = QPushButton("Cancel")
        self.btn_append.clicked.connect(lambda: self._finish(False))
        self.btn_replace.clicked.connect(lambda: self._finish(True))
        btn_cancel.clicked.connect(self.reject)
        btns.addWidget(self.btn_append)
        btns.addWidget(self.btn_replace)
        btns.addWidget(btn_cancel)
        layout.addLayout(btns)

    def _current_records(self) -> list[dict]:
        hdr = self.spin_header.value()
        return shot_import.extract_shots_from_table_with_mapping(
            self.rows,
            name_col=self.cmb_name.currentData(),
            seq_col=self.cmb_seq.currentData(),
            frames_col=self.cmb_frames.currentData(),
            start_col=self.cmb_start.currentData(),
            end_col=self.cmb_end.currentData(),
            header_row=None if hdr < 0 else hdr,
        )

    def _refresh_preview(self):
        recs = self._current_records()
        sample = ", ".join(
            f"{r['name']} [{frames_text(r['frames'])}]" for r in recs[:6]
        )
        more = f" ... (+{len(recs) - 6} more)" if len(recs) > 6 else ""
        self.preview_lbl.setText(f"Preview ({len(recs)} shot(s)): {sample}{more}")
        self.btn_append.setEnabled(bool(recs))
        self.btn_replace.setEnabled(bool(recs))

    def _finish(self, replace: bool):
        recs = self._current_records()
        if not recs:
            return
        self.chosen_shots = [
            Shot(name=r["name"], frames=r["frames"], seq=r.get("seq", "")) for r in recs
        ]
        self.replace_mode = replace
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} by {AUTHOR}")
        self.resize(1480, 860)
        self.shots: list = []
        self.items: dict = {}
        self.current_mode = MODE_LOCAL
        self.scan_config = None         # (mode, copy_from, exts, skip_system) of last scan
        self.scan_worker = None
        self.worker = None
        self._expanded: set = set()
        self._preview_cache: dict = {}      # (copy_from, shot name) -> Path or None
        self._preview_worker = None
        self.io = IoStats()                     # bytes and busy time from the copy threads
        self._logger = setup_app_log()
        self.recorder: RunRecorder | None = None
        self._shot_started_at = time.time()
        self._io_last = (0, 0.0, time.monotonic())
        self._io_timer = QTimer(self)
        self._io_timer.timeout.connect(self._sample_io)
        self._io_timer.start(1000)
        self._progress_block = None         # log line that is updated while copying
        self._progress_prefix = ""
        self._progress_total = 0
        self._progress_stamp = ""
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._run_preview_search)
        self._scanning = False
        self._running = False
        self._kitsu_client = kitsu_client.KitsuClient()
        self._kitsu_config: dict = {}
        self._kitsu_project_id: str = ""
        self._kitsu_pub_workers: list = []
        self._sfx = retro_sfx.RetroSfx(self)
        self._build_ui()
        self._update_previews()
        self._update_controls()
        self.refresh_tree()
        if self.restore_session():
            self.log(f"Previous session restored: {len(self.shots)} shot(s)")
        else:
            self.log(f"{APP_NAME} {APP_VERSION} ready. Drop a CSV file to start.")
        self._autosave = QTimer(self)
        self._autosave.timeout.connect(self.save_session)
        self._autosave.start(20000)         # save every 20 seconds
        self._paste_shortcut = QShortcut(QKeySequence.StandardKey.Paste, self)
        self._paste_shortcut.activated.connect(self.paste_from_clipboard)

    # ---------- layout ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        body = QHBoxLayout()
        outer.addLayout(body, 1)

        # Left column: CSV drop zone, paths, mode boxes and action buttons
        left = QVBoxLayout()
        body.addLayout(left, 0)

        paths_box = QGroupBox()
        grid = QGridLayout(paths_box)
        self.copy_edit = PathEdit("Drag a source folder here, or type the path")
        self.copy_btn = QPushButton("...")
        self.copy_btn.setFixedWidth(44)
        self.paste_edit = PathEdit("Drag a destination folder here, or type the path")
        self.paste_btn = QPushButton("...")
        self.paste_btn.setFixedWidth(44)
        path_height = self.copy_edit.sizeHint().height() * 2
        for w in (self.copy_edit, self.paste_edit, self.copy_btn, self.paste_btn):
            w.setFixedHeight(path_height)
        self.path_history: list = []        # shared by COPY FROM and PASTE TO, newest first
        self.copy_edit.opened.connect(lambda e=self.copy_edit: self._show_history(e))
        self.paste_edit.opened.connect(lambda e=self.paste_edit: self._show_history(e))
        self.copy_edit.editingFinished.connect(lambda e=self.copy_edit: self.remember_path(e.text()))
        self.paste_edit.editingFinished.connect(lambda e=self.paste_edit: self.remember_path(e.text()))
        self.copy_edit.committed.connect(self.remember_path)
        self.paste_edit.committed.connect(self.remember_path)
        self.copy_preview = QLabel()
        self.paste_preview = QLabel()
        for lab in (self.copy_preview, self.paste_preview):
            lab.setWordWrap(True)
            lab.setStyleSheet("font-size: 11px;")
        grid.addWidget(QLabel("COPY FROM"), 0, 0)
        grid.addWidget(self.copy_edit, 0, 1)
        grid.addWidget(self.copy_btn, 0, 2)
        grid.addWidget(self.copy_preview, 1, 1, 1, 2)
        grid.addWidget(QLabel("PASTE TO"), 2, 0)
        grid.addWidget(self.paste_edit, 2, 1)
        grid.addWidget(self.paste_btn, 2, 2)
        grid.addWidget(self.paste_preview, 3, 1, 1, 2)
        self.btn_swap = QPushButton("⇅")
        self.btn_swap.setFixedSize(36, path_height * 2 + 18)
        self.btn_swap.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.btn_swap.setToolTip("Swap COPY FROM and PASTE TO paths")
        self.btn_swap.clicked.connect(self.swap_paths)
        grid.addWidget(self.btn_swap, 0, 3, 3, 1)
        left.addWidget(paths_box)
        self.copy_edit.textChanged.connect(self._update_previews)
        self.paste_edit.textChanged.connect(self._update_previews)
        self.copy_btn.clicked.connect(lambda: self.browse_dir(self.copy_edit))
        self.paste_btn.clicked.connect(lambda: self.browse_dir(self.paste_edit))

        row = QHBoxLayout()
        row.setSpacing(12)
        left.addLayout(row)

        # Unified SCAN box (replaces separate LOCAL and PUBLISH buttons)
        scan_box = QGroupBox()
        sv = QVBoxLayout(scan_box)
        sv.setSpacing(8)
        self.btn_scan = self._mode_button("SCAN")
        self.btn_scan.clicked.connect(lambda: self.start_scan(self._effective_mode()))
        sv.addWidget(self.btn_scan)

        self.chk_dailies = QCheckBox("MAKE DAILIES FOLDER")
        self.chk_dailies.toggled.connect(self._update_previews)
        sv.addWidget(self.chk_dailies)

        self.chk_consistent_path = QCheckBox("Consitant path")
        self.chk_consistent_path.setToolTip(
            "Preserve source folder & subpath structure (like LOCAL mode), "
            "mirroring subpaths from COPY FROM to PASTE TO."
        )
        self.chk_consistent_path.toggled.connect(self._on_consistent_toggled)
        sv.addWidget(self.chk_consistent_path)

        self.chk_kitsu_publish = QCheckBox("UPDATE KITSU (ĐợiCheck)")
        self.chk_kitsu_publish.setChecked(True)
        self.chk_kitsu_publish.setToolTip(
            "After PUBLISH, set Kitsu Compositing to ĐợiCheck, post destination path, and attach frame 1 JPG."
        )
        sv.addWidget(self.chk_kitsu_publish)

        types_grid = QGridLayout()
        types_grid.setSpacing(6)
        self.scan_checks: dict = {}
        for idx, key in enumerate(LOCAL_TYPE_KEYS):
            cb = QCheckBox(key)
            cb.setChecked(key == "EXR")
            self.scan_checks[key] = cb
            types_grid.addWidget(cb, idx // 2, idx % 2)
        sv.addLayout(types_grid)
        sv.addStretch(1)
        row.addWidget(scan_box, 1)

        # COPY MODE box
        mode_box = QGroupBox()
        mv = QVBoxLayout(mode_box)
        mv.setSpacing(10)
        mode_title = QLabel("COPY MODE")
        mode_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mode_title.setStyleSheet("font-size: 14px; font-weight: bold;")
        mv.addWidget(mode_title)
        self.chk_skip = QCheckBox("SKIP MODE")
        self.chk_skip.setChecked(True)
        self.chk_overwrite = QCheckBox("OVERWRITE")
        self.chk_skip_sys = QCheckBox("SKIP SYSTEM FILES")
        self.chk_skip_sys.setChecked(True)
        self.chk_sound = QCheckBox("8-BIT SOUND")
        self.chk_sound.setChecked(True)
        self.chk_sound.toggled.connect(self._on_sound_toggled)
        mv.addWidget(self.chk_skip)
        mv.addWidget(self.chk_overwrite)
        mv.addWidget(self.chk_skip_sys)
        mv.addWidget(self.chk_sound)
        thread_row = QHBoxLayout()
        thread_row.addWidget(QLabel("THREADS / SHOT"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        self.spin_threads.setValue(MAX_WORKERS)
        self.spin_threads.setToolTip("Parallel file copies per shot. Try 8-16 for network drives.")
        thread_row.addWidget(self.spin_threads)
        mv.addLayout(thread_row)
        mv.addStretch(1)
        self.chk_skip.toggled.connect(self._on_skip_toggled)
        self.chk_overwrite.toggled.connect(self._on_overwrite_toggled)
        row.addWidget(mode_box, 1)

        # Action buttons: same size, stacked in one box
        actions_box = QGroupBox()
        av = QVBoxLayout(actions_box)
        av.setSpacing(10)
        self.btn_start = self._action_button("START")
        self.btn_pause = self._action_button("PAUSE")
        self.btn_stop = self._action_button("STOP")
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            av.addWidget(b)
        av.addStretch(1)
        row.addWidget(actions_box, 0)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause.clicked.connect(self.on_pause)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)

        # Right column: SHOTS LIST
        right = QVBoxLayout()
        body.addLayout(right, 1)
        title_row = QHBoxLayout()
        title = QLabel("SHOTS LIST")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.kitsu_status_lbl = QLabel("Kitsu: not connected")
        self.kitsu_status_lbl.setStyleSheet("color: #8a8f98; font-size: 11px;")
        self.btn_kitsu = QPushButton("KITSU...")
        self.btn_kitsu.setMinimumHeight(28)
        self.btn_kitsu.setToolTip("Connect to a Kitsu server and fetch shots directly")
        self.btn_kitsu.clicked.connect(self.open_kitsu_dialog)
        title_row.addWidget(self.kitsu_status_lbl)
        title_row.addSpacing(8)
        title_row.addWidget(self.btn_kitsu)
        right.addLayout(title_row)
        self.tree = ShotTree()
        self.tree.setHeaderLabels(["NO", "SEQ", "NAME", "VERSION", "FRAMES", "STATUS"])
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.dropped.connect(self.on_tree_dropped)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemCollapsed.connect(self._on_collapsed)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(COL_NO, 60)
        self.tree.setColumnWidth(COL_SEQ, 90)
        self.tree.setColumnWidth(COL_VERSION, 140)
        self.tree.setColumnWidth(COL_FRAMES, 80)
        self.tree.setColumnWidth(COL_STATUS, 120)
        right.addWidget(self.tree, 1)

        tree_buttons = QHBoxLayout()
        self.btn_clear = QPushButton("CLEAR")
        self.btn_clear.clicked.connect(self.clear_shots)
        self.btn_info = QPushButton("INFO")
        self.btn_info.clicked.connect(self.show_about)
        self.btn_add = QPushButton("ADD")
        add_menu = QMenu(self.btn_add)
        add_menu.addAction("Fetch from Kitsu...", self.open_kitsu_dialog)
        add_menu.addAction("Import CSV / Excel / Image...", self.browse_csv)
        add_menu.addAction("Paste Screenshot (Ctrl+V)", self.paste_from_clipboard)
        add_menu.addSeparator()
        add_menu.addAction("Add shot folder...", self.add_folder_shot)
        add_menu.addAction("Add shot by name...", self.add_named_shot)
        self.btn_add.setMenu(add_menu)
        self.btn_remove = QPushButton("REMOVE")
        self.btn_remove.clicked.connect(self.remove_selected)
        for b in (self.btn_add, self.btn_remove, self.btn_clear, self.btn_info):
            b.setMinimumHeight(34)
            tree_buttons.addWidget(b)
        self.stat_active = StatCell("ACTIVE TIME", fixed_max=100.0)
        self.stat_rate = StatCell("TRANSFER RATE")
        tree_buttons.addSpacing(12)
        tree_buttons.addWidget(self.stat_active, 1)
        tree_buttons.addSpacing(8)
        tree_buttons.addWidget(self.stat_rate, 1)
        right.addLayout(tree_buttons)

        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_tree_context_menu)
        self.tree.deletePressed.connect(self.remove_selected)
        self.tree.emptyClicked.connect(self.browse_csv)

        # Bottom: progress and log
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m files")
        self.progress.setValue(0)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMaximumHeight(140)
        outer.addWidget(self.progress)
        outer.addWidget(self.log_view)

    @staticmethod
    def _mode_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(44)
        button.setStyleSheet("font-size: 14px; font-weight: bold;")
        return button

    @staticmethod
    def _action_button(text: str) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumSize(150, 46)
        button.setStyleSheet("font-size: 14px; font-weight: bold;")
        return button

    def _type_checks(self, keys, layout) -> dict:
        grid = QGridLayout()
        layout.addLayout(grid)
        checks = {}
        for i, key in enumerate(keys):
            cb = QCheckBox(key)
            checks[key] = cb
            grid.addWidget(cb, i // 2, i % 2)
        return checks

    def _on_skip_toggled(self, on: bool):
        if on:
            self.chk_overwrite.setChecked(False)
        elif not self.chk_overwrite.isChecked():
            self.chk_skip.setChecked(True)

    def _on_overwrite_toggled(self, on: bool):
        if on:
            self.chk_skip.setChecked(False)
        elif not self.chk_skip.isChecked():
            self.chk_overwrite.setChecked(True)

    # ---------- helpers ----------
    def log(self, msg: str, color: str | None = None):
        """Append one log line. An optional colour marks the line (yellow / red shots)."""
        document = self.log_view.document()
        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not document.isEmpty():
            cursor.insertBlock()
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(f"{time.strftime('%H:%M:%S')}  {msg}")
        self._logger.info(msg)

    def _busy(self) -> bool:
        return self._scanning or self._running

    def _copy_from(self):
        text = self.copy_edit.text().strip().strip('"').strip()
        return Path(text) if text else None

    def _paste_to(self):
        text = self.paste_edit.text().strip().strip('"').strip()
        return Path(text) if text else None

    def _effective_mode(self) -> str:
        return MODE_LOCAL if self.chk_consistent_path.isChecked() else MODE_PUBLISH

    def _on_consistent_toggled(self, checked: bool):
        self.current_mode = MODE_LOCAL if checked else MODE_PUBLISH
        self.chk_dailies.setEnabled(not checked and not self._running)
        self.chk_kitsu_publish.setEnabled(not checked and not self._running)
        self._update_previews()

    def _on_sound_toggled(self, checked: bool):
        self._sfx.enabled = checked

    def swap_paths(self):
        if self._running:
            return
        src = self.copy_edit.text()
        dst = self.paste_edit.text()
        self.copy_edit.setText(dst)
        self.paste_edit.setText(src)
        self._preview_cache.clear()
        self._update_previews()
        self.log("Swapped COPY FROM and PASTE TO paths.")

    def _selected_exts(self, mode: str) -> tuple:
        exts = set()
        for key, cb in self.scan_checks.items():
            if cb.isChecked():
                exts.update(FILE_TYPES[key])
        return tuple(sorted(exts))      # empty tuple = every file type

    def _current_config(self, mode: str) -> tuple:
        copy_from = self._copy_from()
        return (mode, str(copy_from) if copy_from else "",
                self._selected_exts(mode), self.chk_skip_sys.isChecked())

    @staticmethod
    def _is_inside(child: Path, parent: Path) -> bool:
        child, parent = child.resolve(), parent.resolve()
        return child == parent or parent in child.parents

    @staticmethod
    def _dailies_name() -> str:
        return dt.date.today().strftime("%y%m%d")

    def _update_controls(self):
        busy = self._busy()
        self.btn_scan.setEnabled(not busy)
        self.btn_start.setEnabled(not busy)
        self.btn_pause.setEnabled(self._running)
        self.btn_stop.setEnabled(self._running)
        self.btn_clear.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.btn_kitsu.setEnabled(not busy)
        self.btn_add.setEnabled(not self._scanning)
        self.btn_swap.setEnabled(not self._running)
        consistent = self.chk_consistent_path.isChecked()
        for w in (self.copy_edit, self.paste_edit, self.copy_btn, self.paste_btn,
                  self.spin_threads, self.chk_consistent_path,
                  self.chk_skip, self.chk_overwrite, self.chk_skip_sys, self.chk_sound,
                  *self.scan_checks.values()):
            w.setEnabled(not self._running)
        self.chk_dailies.setEnabled(not self._running and not consistent)
        self.chk_kitsu_publish.setEnabled(not self._running and not consistent)

    # ---------- CSV / Excel / Image and shot list ----------
    def browse_csv(self):
        if self._busy():
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Shot List (CSV, Excel, Image)",
            "",
            "Shot Lists (*.csv *.xlsx *.xlsm *.xls *.png *.jpg *.jpeg);;"
            "CSV files (*.csv);;Excel files (*.xlsx *.xlsm *.xls);;"
            "Images (*.png *.jpg *.jpeg);;All files (*)",
        )
        if path:
            self.load_import_path(Path(path), replace=True)

    def browse_dir(self, edit: PathEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select folder", edit.text().strip())
        if folder:
            edit.setText(folder)
            self.remember_path(folder)

    def _show_history(self, edit: PathEdit):
        edit.set_history(self.path_history)
        edit.show_history()

    def remember_path(self, text: str):
        """Put a folder at the top of the shared history (duplicates move up)."""
        text = text.strip().strip('"').strip()
        if not text:
            return
        text = str(Path(text))
        if text in self.path_history:
            self.path_history.remove(text)
        self.path_history.insert(0, text)
        del self.path_history[PATH_HISTORY_MAX:]

    def _apply_loaded_shots(self, new_shots: list, replace: bool, source_desc: str):
        if replace:
            self.shots = new_shots
            self.scan_config = None
            self.current_mode = MODE_LOCAL
            added = len(new_shots)
        else:
            known = {s.name for s in self.shots}
            fresh = [s for s in new_shots if s.name not in known]
            self.shots.extend(fresh)
            added = len(fresh)
        self.log(f"Loaded {added} shot(s) from {source_desc}")
        self.refresh_tree()
        self._update_previews()

    def load_csv_path(self, path: Path, replace: bool):
        self.load_import_path(path, replace=replace)

    def _import_from_table_or_text(
        self,
        rows: list[list[str]],
        text: str,
        replace: bool,
        source_desc: str,
    ):
        if rows:
            info = shot_import.detect_table_columns(rows)
            if info["confident"]:
                recs = shot_import.extract_shots_from_table_with_mapping(
                    rows,
                    name_col=info["name_col"],
                    seq_col=info["seq_col"],
                    frames_col=info["frames_col"],
                    start_col=info["start_col"],
                    end_col=info["end_col"],
                    header_row=info["header_row"],
                )
                if recs:
                    new_shots = [Shot(name=r["name"], frames=r["frames"], seq=r.get("seq", "")) for r in recs]
                    self._apply_loaded_shots(new_shots, replace=replace, source_desc=source_desc)
                    return
            dlg = ShotImportDialog(rows, info, source_desc, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen_shots:
                self._apply_loaded_shots(dlg.chosen_shots, replace=dlg.replace_mode, source_desc=source_desc)
            return

        recs = shot_import.parse_shots_from_text(text)
        if recs:
            new_shots = [Shot(name=r["name"], frames=r["frames"], seq=r.get("seq", "")) for r in recs]
            self._apply_loaded_shots(new_shots, replace=replace, source_desc=source_desc)
            return
        lines = [[ln.strip()] for ln in (text or "").splitlines() if ln.strip()]
        if lines:
            dlg = ShotImportDialog(
                lines,
                {"confident": False, "header_row": None, "name_col": 0, "seq_col": -1,
                 "frames_col": -1, "start_col": -1, "end_col": -1},
                source_desc,
                parent=self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen_shots:
                self._apply_loaded_shots(dlg.chosen_shots, replace=dlg.replace_mode, source_desc=source_desc)
            return
        raise ValueError("No text or shot names were recognized.")

    def load_import_path(self, path: Path, replace: bool):
        if self._busy():
            self.log("Cannot load a shot list while scanning or copying.")
            return
        ext = path.suffix.lower()
        try:
            if ext == ".csv":
                new_shots = load_csv(path)
                self._apply_loaded_shots(new_shots, replace=replace, source_desc=path.name)
                return
            if ext in (".xlsx", ".xlsm", ".xls"):
                rows = shot_import.read_excel_rows(path)
                self._import_from_table_or_text(rows, "", replace=replace, source_desc=path.name)
                return
            if ext in (".png", ".jpg", ".jpeg"):
                text, rows = shot_import.extract_ocr_data_from_image(path)
                self._import_from_table_or_text(rows, text, replace=replace, source_desc=path.name)
                return
            raise ValueError(f"Unsupported file type: {ext}")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not read {path.name}:\n{exc}")

    def paste_from_clipboard(self):
        focus = QApplication.focusWidget()
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return

        if mime.hasImage():
            if self._busy():
                self.log("Cannot import a screenshot while scanning or copying.")
                return
            img = QImage(mime.imageData())
            if img.isNull():
                return
            with tempfile.TemporaryDirectory(prefix="dkt_clip_") as td:
                tmp_png = Path(td) / "clipboard.png"
                if not img.save(str(tmp_png), "PNG"):
                    return
                try:
                    text, rows = shot_import.extract_ocr_data_from_image(tmp_png)
                    self._import_from_table_or_text(
                        rows, text, replace=False, source_desc="Clipboard Screenshot"
                    )
                except Exception as exc:
                    QMessageBox.critical(self, APP_NAME, f"Could not read clipboard image:\n{exc}")
            return

        if isinstance(focus, (QLineEdit, QPlainTextEdit, QSpinBox)):
            if hasattr(focus, "paste"):
                focus.paste()
            return

        if mime.hasUrls():
            paths = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
            if paths:
                self.on_tree_dropped(paths)
                return

        if mime.hasText():
            text = mime.text()
            if not text.strip():
                return
            tab_rows = [
                [c.strip() for c in ln.split("\t")]
                for ln in text.splitlines()
                if "\t" in ln and ln.strip()
            ]
            try:
                self._import_from_table_or_text(
                    tab_rows, text, replace=False, source_desc="Clipboard Text"
                )
            except Exception as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not parse clipboard text:\n{exc}")

    def _update_kitsu_badge(self):
        user = self._kitsu_client.user
        if user and self._kitsu_client.access_token:
            name = user.get("full_name") or user.get("email") or "Connected"
            self.kitsu_status_lbl.setText(f"Kitsu: {name}")
            self.kitsu_status_lbl.setStyleSheet("color: #5cd67d; font-size: 11px;")
        elif self._kitsu_config.get("url"):
            self.kitsu_status_lbl.setText("Kitsu: saved server")
            self.kitsu_status_lbl.setStyleSheet("color: #8a8f98; font-size: 11px;")
        else:
            self.kitsu_status_lbl.setText("Kitsu: not connected")
            self.kitsu_status_lbl.setStyleSheet("color: #8a8f98; font-size: 11px;")

    def open_kitsu_dialog(self):
        if self._busy():
            self.log("Wait for the current scan or copy to finish before fetching from Kitsu.")
            return
        dlg = KitsuDialog(self._kitsu_client, self._kitsu_config, parent=self)
        dlg.import_requested.connect(self._on_kitsu_import)
        dlg.exec()
        self._kitsu_config = dlg.export_config()
        self._update_kitsu_badge()
        self.save_session()

    def _on_kitsu_import(self, records: list, replace: bool, project_name: str, project_id: str = ""):
        if project_id:
            self._kitsu_project_id = project_id
        new_shots = shots_from_kitsu(records)
        self._apply_loaded_shots(new_shots, replace=replace, source_desc=f"Kitsu ({project_name})")

    def on_tree_dropped(self, paths: list):
        if self._scanning:
            self.log("Wait for the scan to finish before adding shots.")
            return
        known = {s.name for s in self.shots}
        for p in paths:
            if p.is_dir():
                if p.name in known:
                    continue
                frames = len(direct_files(p, (), True))
                self.shots.append(Shot(name=p.name, frames=frames))
                known.add(p.name)
                self.log(f"Added shot folder {p.name} ({frames} frames)")
            elif p.suffix.lower() in (".csv", ".xlsx", ".xlsm", ".xls", ".png", ".jpg", ".jpeg"):
                self.load_import_path(p, replace=False)
        self.refresh_tree()
        self._update_previews()

    # ---------- add / remove / clear / context menu ----------
    def add_folder_shot(self):
        if self._scanning:
            return
        folder = QFileDialog.getExistingDirectory(self, "Select a shot folder")
        if folder:
            self.on_tree_dropped([Path(folder)])

    def add_named_shot(self):
        if self._scanning:
            return
        name, ok = QInputDialog.getText(self, APP_NAME, "Shot name (e.g. HHCC_SC07_0250):")
        name = name.strip() if ok else ""
        if not name:
            return
        if "/" in name or "\\" in name:
            QMessageBox.warning(self, APP_NAME, "Shot name cannot contain / or \\.")
            return
        if any(s.name == name for s in self.shots):
            self.log(f"Shot {name} is already in the list.")
            return
        frames, ok = QInputDialog.getInt(self, APP_NAME, f"Frames for {name}:", 0, 0, 1000000)
        if not ok:
            return
        self.shots.append(Shot(name=name, frames=frames))
        self.log(f"Added shot {name} ({frames} frames)")
        self.refresh_tree()
        self._update_previews()

    def remove_selected(self):
        if self._busy():
            self.log("Cannot remove shots while scanning or copying.")
            return
        indices = set()
        for item in self.tree.selectedItems():
            owner = item if item.parent() is None else item.parent()
            index = owner.data(COL_NAME, ROLE_INDEX)
            if index is not None:
                indices.add(index)
        if not indices:
            self.log("Select one or more shots first.")
            return
        if len(indices) > 1:
            names = [self.shots[i].name for i in sorted(indices)]
            preview = "\n".join(names[:10]) + ("\n..." if len(names) > 10 else "")
            answer = QMessageBox.question(self, APP_NAME,
                                          f"Remove {len(indices)} shot(s) from the list?\n\n{preview}")
            if answer != QMessageBox.StandardButton.Yes:
                return
        for index in sorted(indices, reverse=True):
            del self.shots[index]
        self.log(f"Removed {len(indices)} shot(s)")
        self.refresh_tree()
        self._update_previews()

    def clear_shots(self):
        """CLEAR: empty the shot list and the log view (the app log file is kept)."""
        if self._busy():
            return
        has_log = bool(self.log_view.toPlainText().strip())
        if not self.shots and not has_log:
            return
        question = "Clear the whole shot list and the log?" if self.shots else "Clear the log?"
        answer = QMessageBox.question(self, APP_NAME, question)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._sfx.play("clear")
        self.log_view.clear()
        if self.shots:
            self.shots = []
            self.scan_config = None
            self.current_mode = MODE_LOCAL
            self._expanded.clear()
            self.refresh_tree()
            self._update_previews()

    def _sample_io(self):
        """Once a second: transfer rate (MB/s) and active time (%) from the copy threads."""
        now = time.monotonic()
        total_bytes, busy = self.io.snapshot()
        last_bytes, last_busy, last_time = self._io_last
        elapsed = max(now - last_time, 1e-6)
        rate = (total_bytes - last_bytes) / elapsed / (1024 * 1024)
        active = min(100.0, max(0.0, (busy - last_busy) / elapsed * 100.0))
        self._io_last = (total_bytes, busy, now)
        self.stat_active.push(active, f"{active:.0f} %")
        self.stat_rate.push(rate, f"{rate:.1f} MB/s")
        if self.recorder is not None:
            self.recorder.sample(rate, active, self.spin_threads.value())

    def _shot_folder(self, index: int):
        """Full source folder of a shot: from the scan, or from a quick search."""
        shot = self.shots[index]
        if shot.scan and shot.scan.units:
            return shot.scan.units[0].path
        copy_from = self._copy_from()
        if copy_from is None or not copy_from.is_dir():
            return None
        key = (str(copy_from), shot.name)
        if key not in self._preview_cache:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._preview_cache[key] = find_first_folder(
                    copy_from, shot.name, [s.name for s in self.shots])
            finally:
                QApplication.restoreOverrideCursor()
        return self._preview_cache[key]

    def _item_path(self, item: QTreeWidgetItem):
        """Folder that a row points to: the variant folder, or the shot's folder."""
        if item.parent() is not None:
            value = item.data(COL_NAME, ROLE_PATH)
            return Path(value) if value else None
        index = item.data(COL_NAME, ROLE_INDEX)
        if index is None or index >= len(self.shots):
            return None
        return self._shot_folder(index)

    def on_tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        path = self._item_path(item)
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        act_open = menu.addAction("Open in Explorer")
        act_copy = menu.addAction("Copy path")
        if item.parent() is None:
            index = item.data(COL_NAME, ROLE_INDEX)
            if index is not None and index < len(self.shots):
                shot = self.shots[index]
                act_force = menu.addAction("Force copy")
                act_force.setCheckable(True)
                act_force.setChecked(shot.force_copy)
                act_force.setEnabled(shot.scan is not None and shot.scan.found > 0
                                     and shot.state != "done" and not self._busy())
                act_force.setToolTip("Copy even if the frame count differs; existing files are overwritten")
                act_force.triggered.connect(
                    lambda checked=False, i=index: self.set_force_copy(i, checked))
        menu.addSeparator()
        act_remove = menu.addAction("Remove")
        act_remove.setEnabled(not self._busy())
        if path is None:
            act_open.setEnabled(False)
            act_copy.setEnabled(False)
            act_open.setToolTip("Folder not found in COPY FROM")
            act_copy.setToolTip("Folder not found in COPY FROM")
        act_open.triggered.connect(lambda _=False: self.open_path(path))
        act_copy.triggered.connect(lambda _=False: self.copy_path(path))
        act_remove.triggered.connect(lambda _=False: self.remove_selected())
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _start_recording(self, **config):
        try:
            self.recorder = RunRecorder(config)
            self.log(f"Run record: {self.recorder.dir}")
        except OSError as exc:
            self.recorder = None
            self.log(f"Run record not written: {exc}", C_YELLOW)

    def _finish_recording(self, outcome: str):
        if self.recorder is None:
            return
        try:
            self.recorder.close(outcome)
        except OSError as exc:
            self.log(f"Run record could not be finished: {exc}", C_YELLOW)
        self.recorder = None

    def _on_file_done(self, index: int, src: str, nbytes: int, seconds: float,
                      result: str, error: str):
        if self.recorder is not None and index < len(self.shots):
            self.recorder.file_done(self.shots[index].name, src, nbytes, seconds, result, error)

    def show_about(self):
        AboutDialog(self).exec()

    def set_force_copy(self, index: int, checked: bool):
        shot = self.shots[index]
        shot.force_copy = checked
        self._refresh_row(index)
        state = "on (frame mismatch allowed, files overwritten)" if checked else "off"
        self.log(f"Force copy {state}: {shot.name}")

    def copy_path(self, path: Path):
        QApplication.clipboard().setText(str(path))
        self.log(f"Copied path: {path}")

    def open_path(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, APP_NAME, f"Folder not found:\n{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, APP_NAME, f"Could not open:\n{path}")

    def refresh_tree(self):
        self.tree.clear()
        self.items = {}
        self.tree.set_hint(not self.shots)
        for i, shot in enumerate(self.shots):
            top = QTreeWidgetItem(self.tree)
            top.setData(COL_NAME, Qt.ItemDataRole.UserRole, shot.name)
            top.setData(COL_NAME, ROLE_INDEX, i)
            self.items[i] = top
            self._fill_top(top, i, shot)
            if shot.scan and len(shot.scan.units) > 1:
                for unit in shot.scan.units:
                    child = QTreeWidgetItem(top)
                    child.setText(COL_NAME, unit.path.name)
                    child.setData(COL_NAME, ROLE_PATH, str(unit.path))
                    child.setText(COL_VERSION, unit.label)
                    child.setText(COL_FRAMES, frames_text(shot.frames))
                    child.setText(COL_STATUS, f"{unit.found}/{frames_text(shot.frames)}")
                    child.setToolTip(COL_STATUS, f"Missing: {unit.gaps[:60]}" if unit.gaps else "")
                    apply_style(child, {"color": C_YELLOW} if shot.scan.copied
                                else unit_style(unit.found, shot.frames))
                top.setExpanded(shot.name in self._expanded)

    def _fill_top(self, item: QTreeWidgetItem, index: int, shot: Shot):
        item.setText(COL_NO, str(index + 1))
        item.setText(COL_SEQ, shot.seq)
        item.setText(COL_NAME, shot.name)
        item.setText(COL_VERSION, shot.scan.version_text if shot.scan else "")
        item.setText(COL_FRAMES, frames_text(shot.frames))
        item.setText(COL_STATUS, status_text(shot))
        problem = shot_problem(shot)
        tip = f"{problem[1]}\n{problem[2]}" if problem else ""
        for col in range(COLUMNS):
            item.setToolTip(col, tip)
        apply_style(item, row_style(shot))

    def _refresh_row(self, index: int):
        item = self.items.get(index)
        if item is not None and index < len(self.shots):
            self._fill_top(item, index, self.shots[index])

    def _on_expanded(self, item: QTreeWidgetItem):
        name = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        if name:
            self._expanded.add(name)

    def _on_collapsed(self, item: QTreeWidgetItem):
        name = item.data(COL_NAME, Qt.ItemDataRole.UserRole)
        if name:
            self._expanded.discard(name)

    # ---------- scan (LOCAL / PUBLISH buttons) ----------
    def start_scan(self, mode: str):
        if self._busy():
            return
        if not self.shots:
            QMessageBox.information(self, APP_NAME, "Load a CSV file first.")
            return
        copy_from = self._copy_from()
        if copy_from is None or not copy_from.is_dir():
            QMessageBox.warning(self, APP_NAME, "COPY FROM folder was not found.")
            return
        config = self._current_config(mode)
        self.current_mode = mode
        self._scanning = True
        self._update_controls()
        self._sfx.play("scan")
        self.log(f"{mode}: scanning {len(self.shots)} shot(s) under {copy_from} ...")
        paste_to = self._paste_to()
        dailies = self._dailies_name() if (mode == MODE_PUBLISH and self.chk_dailies.isChecked()) else ""
        self.scan_worker = ScanWorker([s.name for s in self.shots], copy_from,
                                      mode, config[2], config[3], paste_to, dailies)
        self.scan_worker.finished_scan.connect(
            lambda results, error, cfg=config: self._on_scan_done(results, error, cfg))
        self.scan_worker.start()

    def _on_scan_done(self, results: list, error: str, config: tuple):
        self._scanning = False
        if error:
            self.log(f"Scan failed: {error}")
            QMessageBox.critical(self, APP_NAME, f"Scan failed:\n{error}")
        else:
            for shot, res in zip(self.shots, results):
                shot.scan = res
                if shot.state not in ("done", "partial"):
                    shot.state = "scanned"
            self.scan_config = config
            copied = sum(1 for s in self.shots if s.scan and s.scan.copied)
            ok = sum(1 for s in self.shots if s.scan and not s.scan.copied
                     and frames_match(s.scan.found, s.frames) and s.scan.found)
            mism = sum(1 for s in self.shots if s.scan and not s.scan.copied
                       and s.scan.found and not frames_match(s.scan.found, s.frames))
            none = sum(1 for s in self.shots if not s.scan or (s.scan.found == 0 and not s.scan.copied))
            self.log(f"Scan done: {ok} ready, {copied} already copied (skipped), "
                     f"{mism} mismatch (skipped), {none} without files")
            for s in self.shots:
                problem = shot_problem(s)
                if problem is None:
                    continue
                colour, reason, path = problem
                tag = "YELLOW" if colour == C_YELLOW else "RED"
                self.log(f"  [{tag}] {s.name}: {reason}", colour)
                for line in path.splitlines():
                    self.log(f"      path: {line}", colour)
        self.refresh_tree()
        self._update_previews()
        self._update_controls()

    # ---------- START / PAUSE / STOP ----------
    def on_start(self):
        if self._busy():
            return
        if not self.scan_config or not any(s.scan for s in self.shots):
            QMessageBox.information(self, APP_NAME, "Scan first: press LOCAL or PUBLISH.")
            return
        mode = self.scan_config[0]
        if self._current_config(mode) != self.scan_config:
            QMessageBox.warning(self, APP_NAME,
                                "Settings changed since the last scan.\n"
                                "Press LOCAL or PUBLISH again.")
            return
        copy_from = self._copy_from()
        paste_to = self._paste_to()
        if paste_to is None:
            QMessageBox.warning(self, APP_NAME, "PASTE TO folder is empty.")
            return
        if self._is_inside(paste_to, copy_from):
            QMessageBox.warning(self, APP_NAME, "PASTE TO cannot be inside COPY FROM.")
            return

        dailies = self._dailies_name() if (mode == MODE_PUBLISH and self.chk_dailies.isChecked()) else ""
        for used in (copy_from, paste_to):
            if used is not None:
                self.remember_path(str(used))
        overwrite_all = self.chk_overwrite.isChecked()
        plans, total = [], 0
        for i, shot in enumerate(self.shots):
            # Shots with a count mismatch, no files, or already done are skipped
            if (shot.scan is None or shot.state == "done" or shot.scan.found == 0
                    or (not frames_match(shot.scan.found, shot.frames) and not shot.force_copy)
                    or (shot.scan.copied and not (overwrite_all or shot.force_copy))):
                continue
            jobs = build_jobs(shot, mode, copy_from, paste_to, dailies)
            if not jobs:
                continue
            plans.append((i, shot.frames, jobs,
                          overwrite_all or shot.force_overwrite or shot.force_copy))
            total += len(jobs)
        if not plans:
            QMessageBox.information(self, APP_NAME,
                                    "No shot is ready to copy.\n"
                                    "Shots with mismatched frames or no files are skipped.")
            return

        check = preflight([(p[2], p[3]) for p in plans], paste_to)
        if not check["writable"]:
            QMessageBox.warning(self, APP_NAME, f"PASTE TO cannot be written to:\n{paste_to}")
            return
        summary = (f"{check['to_copy']} file(s) to copy, {format_bytes(check['need'])} needed"
                   + (f", {format_bytes(check['free'])} free" if check["free"] is not None else ""))
        if check["free"] is not None and check["need"] > check["free"]:
            QMessageBox.warning(self, APP_NAME,
                                f"Not enough free space on the destination.\n\n{summary}")
            return
        warnings = []
        if check["empty"]:
            warnings.append(f"{check['empty']} source file(s) are empty (0 bytes)")
        if check["long_paths"]:
            warnings.append(f"{check['long_paths']} destination path(s) are longer than "
                            f"{WINDOWS_PATH_LIMIT} characters and may fail on Windows")
        if check["leftovers"]:
            warnings.append(f"{check['leftovers']} unfinished .part file(s) on the destination "
                            "will be replaced")
        self.log(f"Pre-check: {summary}")
        for warning in warnings:
            self.log(f"Pre-check warning: {warning}", C_YELLOW)
        extra = "\n\n" + summary + ("\n" + "\n".join(warnings) if warnings else "")
        answer = QMessageBox.question(
            self, APP_NAME,
            f"{mode}: copy {len(plans)} shot(s), {total} file(s)?\n\n"
            f"Mode: {'OVERWRITE' if overwrite_all else 'SKIP existing files'}\n"
            f"To: {paste_to}{os.sep}" + (f"{dailies}{os.sep}" if dailies else "") + extra)
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self.worker = CopyWorker(plans, self.io, self.spin_threads.value())
        self.worker.shot_started.connect(self._on_shot_started)
        self.worker.shot_progress.connect(self._on_shot_progress)
        self.worker.shot_finished.connect(self._on_shot_finished)
        self.worker.overall.connect(self.progress.setValue)
        self.worker.log.connect(self.log)
        self.worker.file_done.connect(self._on_file_done)
        self._start_recording(mode=mode, threads=self.spin_threads.value(),
                              overwrite=overwrite_all, dailies=dailies,
                              copy_from=str(copy_from), paste_to=str(paste_to),
                              shots=len(plans), files_planned=total,
                              exts=list(self.scan_config[2]),
                              skip_system=self.chk_skip_sys.isChecked())
        self.worker.finished.connect(self._on_worker_finished)
        self._running = True
        self._progress_block = None
        self.btn_pause.setText("PAUSE")
        self._update_controls()
        self._sfx.play("start")
        self.log(f"Start {mode}: {len(plans)} shot(s), {total} file(s)")
        self.worker.start()

    def on_pause(self):
        if not self._running or self.worker is None:
            return
        if self.worker.pause_event.is_set():
            self.worker.pause_event.clear()
            self.btn_pause.setText("PAUSE")
            self.log("Resumed")
        else:
            self.worker.pause_event.set()
            self.btn_pause.setText("RESUME")
            self.log("Paused")

    def on_stop(self):
        if not self._running or self.worker is None:
            return
        self._sfx.play("stop")
        self.worker.stop_event.set()
        self.worker.pause_event.clear()
        self.btn_pause.setText("PAUSE")
        self.log("Stopping... the file in progress will be removed and "
                 "the shot re-copied with overwrite on the next START.")

    # ---------- worker callbacks (run in the GUI thread) ----------
    def _set_log_line(self, block, text: str):
        """Replace the text of one log line in place (used for live copy progress)."""
        if block is None or not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(QTextCharFormat())     # progress text is never coloured
        cursor.insertText(f"{self._progress_stamp}  {text}")

    def _on_shot_started(self, index: int, total: int):
        shot = self.shots[index]
        self._shot_started_at = time.time()
        shot.state = "copying"
        shot.done = 0
        self._refresh_row(index)
        self._progress_stamp = time.strftime("%H:%M:%S")
        self._progress_prefix = f"Copying {shot.name}"
        self._progress_total = total
        if self._progress_block is None or not self._progress_block.isValid():
            self.log(f"{self._progress_prefix} (0/{total} file(s))")
            self._progress_block = self.log_view.document().lastBlock()
        else:
            self._set_log_line(self._progress_block, f"{self._progress_prefix} (0/{total} file(s))")

    def _on_shot_progress(self, index: int, done: int):
        shot = self.shots[index]
        shot.done = done
        self._refresh_row(index)
        self._set_log_line(self._progress_block,
                           f"{self._progress_prefix} ({done}/{self._progress_total} file(s))")

    def _on_shot_finished(self, index: int, result: str):
        shot = self.shots[index]
        if self.recorder is not None:
            self.recorder.shot_done(shot.name, result, time.time() - self._shot_started_at)
        shot.state = result
        if result == "done":
            shot.force_overwrite = False
        elif result == "partial":
            shot.force_overwrite = True
        self._refresh_row(index)
        label = {"done": "done", "partial": "stopped", "error": "finished with errors"}.get(result, result)
        self._set_log_line(self._progress_block,
                           f"{self._progress_prefix} ({shot.done}/{self._progress_total} file(s)) - {label}")
        if (
            result == "done"
            and self.current_mode == MODE_PUBLISH
            and self.chk_kitsu_publish.isChecked()
            and self._kitsu_client.access_token
        ):
            self._queue_kitsu_publish_update(shot)

    def _queue_kitsu_publish_update(self, shot: Shot):
        project_id = self._kitsu_project_id or self._kitsu_config.get("project_id", "")
        if not project_id:
            self.log(f"Kitsu skip ({shot.name}): no Kitsu project selected.", C_ORANGE)
            return
        if not shot.scan or not shot.scan.units:
            return
        unit = shot.scan.units[0]
        dest_base = Path(shot.scan.dest) if shot.scan.dest else None
        if dest_base is None:
            paste_to = self._paste_to()
            if paste_to is None:
                return
            dailies = self._dailies_name() if self.chk_dailies.isChecked() else ""
            dest_base = destination_base(paste_to, MODE_PUBLISH, dailies)
        dest_path = str(dest_base / unit.path.name)

        worker = KitsuPublishWorker(
            client=self._kitsu_client,
            project_id=project_id,
            shot=shot,
            dest_path=dest_path,
            source_files=list(unit.copy_files),
        )
        worker.done.connect(lambda _name, msg: self.log(msg))
        worker.failed.connect(lambda _name, msg: self.log(msg, C_ORANGE))
        worker.finished.connect(
            lambda w=worker: self._kitsu_pub_workers.remove(w)
            if w in self._kitsu_pub_workers else None
        )
        self._kitsu_pub_workers.append(worker)
        worker.start()

    def _on_worker_finished(self):
        stopped = self.worker.stop_event.is_set() if self.worker else False
        self._finish_recording("stopped" if stopped else "finished")
        self._running = False
        if not stopped:
            self._sfx.play("done")
        self.log("Stopped by user." if stopped else "All shots finished.")
        self.refresh_tree()
        self._update_controls()

    # ---------- session save / restore ----------
    def _collect_session(self) -> dict:
        selected_types = [k for k, cb in self.scan_checks.items() if cb.isChecked()]
        return {
            "format": 1,
            "app_version": APP_VERSION,
            "copy_from": self.copy_edit.text(),
            "path_history": self.path_history,
            "paste_to": self.paste_edit.text(),
            "current_mode": self.current_mode,
            "consistent_path": self.chk_consistent_path.isChecked(),
            "scan_types": selected_types,
            "local_types": selected_types,
            "publish_types": selected_types,
            "dailies": self.chk_dailies.isChecked(),
            "kitsu_publish": self.chk_kitsu_publish.isChecked(),
            "sound": self.chk_sound.isChecked(),
            "overwrite": self.chk_overwrite.isChecked(),
            "skip_system": self.chk_skip_sys.isChecked(),
            "threads": self.spin_threads.value(),
            "geometry": bytes(self.saveGeometry().toBase64()).decode("ascii"),
            "kitsu": self._kitsu_config,
            "kitsu_project_id": self._kitsu_project_id,
            "shots": [
                {"name": s.name, "frames": s.frames, "seq": s.seq, "state": s.state,
                 "done": s.done, "force_overwrite": s.force_overwrite,
                 "force_copy": s.force_copy, "kitsu_id": s.kitsu_id}
                for s in self.shots
            ],
            "log": self.log_view.toPlainText(),
        }

    def save_session(self):
        try:
            path = session_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(self._collect_session(), ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, path)           # atomic: a crash never leaves a half-written file
        except (OSError, ValueError) as exc:
            self.log(f"Could not save session: {exc}")

    def restore_session(self) -> bool:
        path = session_file()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.log(f"Could not read the saved session, starting fresh: {exc}")
            return False

        self.path_history = [str(p) for p in data.get("path_history", []) if p][:PATH_HISTORY_MAX]
        self.copy_edit.setText(data.get("copy_from", ""))
        self.paste_edit.setText(data.get("paste_to", ""))
        saved_types = data.get("scan_types")
        if saved_types is None:
            saved_types = data.get("local_types") or data.get("publish_types") or ["EXR"]
        for key, cb in self.scan_checks.items():
            cb.setChecked(key in saved_types)
        self.chk_dailies.setChecked(bool(data.get("dailies", False)))
        self.chk_kitsu_publish.setChecked(bool(data.get("kitsu_publish", True)))
        consistent = data.get("consistent_path")
        if consistent is None:
            consistent = (data.get("current_mode", MODE_LOCAL) == MODE_LOCAL)
        self.chk_consistent_path.setChecked(bool(consistent))
        self.chk_sound.setChecked(bool(data.get("sound", True)))
        self._sfx.enabled = self.chk_sound.isChecked()
        self.chk_skip_sys.setChecked(bool(data.get("skip_system", True)))
        self.spin_threads.setValue(int(data.get("threads", MAX_WORKERS)))
        if data.get("overwrite"):
            self.chk_overwrite.setChecked(True)
        else:
            self.chk_skip.setChecked(True)
        self.current_mode = data.get("current_mode", MODE_LOCAL)
        geometry = data.get("geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))

        shots = []
        for item in data.get("shots", []):
            frames = item.get("frames")
            shot = Shot(name=str(item["name"]),
                        frames=None if frames is None else int(frames),
                        seq=str(item.get("seq", "")),
                        kitsu_id=str(item.get("kitsu_id", "")))
            state = item.get("state", "new")
            if state == "copying":          # app was closed mid-copy: resume with overwrite
                state = "partial"
            if state in ("done", "partial", "error"):
                shot.state = state
                shot.done = int(item.get("done", 0))
            shot.force_overwrite = bool(item.get("force_overwrite", False)) or state == "partial"
            shot.force_copy = bool(item.get("force_copy", False))
            shots.append(shot)
        self.shots = shots

        log_text = data.get("log", "")
        if log_text:
            self.log_view.setPlainText(log_text)
        kitsu_cfg = data.get("kitsu")
        if isinstance(kitsu_cfg, dict):
            self._kitsu_config = kitsu_cfg
        self._kitsu_project_id = str(data.get("kitsu_project_id") or self._kitsu_config.get("project_id") or "")
        self._update_kitsu_badge()
        self.refresh_tree()
        self._update_previews()
        return True

    def closeEvent(self, event):
        if self._running and self.worker is not None:
            self.worker.stop_event.set()
            self.worker.pause_event.clear()
            self.worker.wait(10000)
        self._finish_recording("closed")
        self.save_session()
        event.accept()

    # ---------- path previews ----------
    def _set_preview(self, label: QLabel, text: str, warning: str = ""):
        if warning:
            label.setText(warning)
            label.setStyleSheet("font-size: 11px; color: #ff4d4d;")
        else:
            label.setText(text)
            label.setStyleSheet("font-size: 11px;")

    def _schedule_preview_search(self):
        self._preview_timer.start(400)      # debounce while the user is typing

    def _run_preview_search(self):
        copy_from = self._copy_from()
        if self._busy() or copy_from is None or not copy_from.is_dir() or not self.shots:
            return
        key = (str(copy_from), self.shots[0].name)
        if key in self._preview_cache:
            return
        if self._preview_worker is not None and self._preview_worker.isRunning():
            return                          # the finished worker will trigger the next search
        self._preview_worker = PreviewWorker(copy_from, self.shots[0].name,
                                             [s.name for s in self.shots])
        self._preview_worker.found.connect(self._on_preview_found)
        self._preview_worker.start()

    def _on_preview_found(self, copy_from_text: str, name: str, path_text: str):
        self._preview_cache[(copy_from_text, name)] = Path(path_text) if path_text else None
        self._update_previews()

    def _update_previews(self, *_):
        copy_from = self._copy_from()
        paste_to = self._paste_to()
        mode = self.current_mode
        dailies = self._dailies_name() if (mode == MODE_PUBLISH and self.chk_dailies.isChecked()) else ""
        sample = next((s for s in self.shots if s.scan and s.scan.units), None)
        name = self.shots[0].name if self.shots else ""

        # Full path of the sample shot folder in the source
        found = None
        src_warn = ""
        if sample:
            found = sample.scan.units[0].path
        elif copy_from and name and copy_from.is_dir():
            key = (str(copy_from), name)
            if key in self._preview_cache:
                found = self._preview_cache[key]
                if found is None:
                    src_warn = f"Folder for {name} not found in COPY FROM"
            else:
                self._schedule_preview_search()

        if copy_from is None:
            src = ""
        elif found is not None:
            src = str(found)
        else:
            src = str(copy_from)
        if copy_from and not copy_from.is_dir():
            src_warn = "Source folder not found"
        self._set_preview(self.copy_preview, src, src_warn)

        # Full destination path of the same shot
        dst, dst_warn = "", ""
        if paste_to is None:
            dst = ""
        elif found is not None and mode == MODE_PUBLISH:
            dst = os.path.join(str(paste_to), dailies, found.name) if dailies \
                else os.path.join(str(paste_to), found.name)
        elif found is not None and copy_from:
            try:
                dst = os.path.join(str(paste_to), str(found.relative_to(copy_from)))
            except ValueError:
                dst = os.path.join(str(paste_to), found.name)
        else:
            tail = name or "<SHOT>"
            dst = os.path.join(str(paste_to), dailies, tail) if dailies \
                else os.path.join(str(paste_to), tail)
        if copy_from and paste_to and self._is_inside(paste_to, copy_from):
            dst_warn = "Destination is inside the source folder"
        self._set_preview(self.paste_preview, dst, dst_warn)

def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    text = QColor(230, 230, 230)
    p.setColor(QPalette.ColorRole.Window, QColor(43, 45, 50))
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, QColor(30, 31, 35))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(43, 45, 50))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor(60, 63, 70))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(30, 31, 35))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(58, 120, 200))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
