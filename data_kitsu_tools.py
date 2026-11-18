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
import datetime as dt
import io
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QMenu, QInputDialog,
)

APP_NAME = "Data Kitsu Tools"
APP_VERSION = "1.04"

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

COLUMNS = 5
ROLE_INDEX = int(Qt.ItemDataRole.UserRole) + 1   # shot index stored on top rows
ROLE_PATH = int(Qt.ItemDataRole.UserRole) + 2    # folder path stored on variant rows


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


@dataclass
class Shot:
    name: str
    frames: int
    scan: ScanResult | None = None
    state: str = "new"              # new | scanned | copying | done | partial | error
    done: int = 0
    force_overwrite: bool = False   # set after STOP so the next run overwrites


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


def scan_source(copy_from: Path, names) -> dict:
    """Walk the source tree once and return {shot name: [matching folders]}.

    A folder matches a shot when its name equals the shot name or starts with
    "<shot>_". That underscore stops HHCC_SC14_010 from matching HHCC_SC14_0100.
    """
    wanted = set(names)
    matches = {n: [] for n in names}
    for dirpath, dirnames, _ in os.walk(copy_from):
        dirnames[:] = [d for d in dirnames if not is_system_name(d)]
        for d in dirnames:
            parts = d.split("_")
            for k in range(1, len(parts) + 1):
                candidate = "_".join(parts[:k])
                if candidate in wanted:
                    matches[candidate].append(Path(dirpath) / d)
    return matches


def find_first_folder(root: Path, name: str):
    """Return the first folder under root matching a shot name (fast, used for previews)."""
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_system_name(d)]
        for d in dirnames:
            if d == name or d.startswith(name + "_"):
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


def unit_label(folder: Path, shot_name: str) -> str:
    """Short label for the Version column: PL01, v06 or the leftover suffix."""
    suffix = folder.name[len(shot_name):]
    m = VARIANT_RE.search(suffix)
    if m:
        return m.group(1).upper()
    m = VERSION_RE.search(suffix)
    if m:
        return f"v{int(m.group(1)):02d}"
    return suffix.lstrip("_") or "base"


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


def build_jobs(shot: Shot, mode: str, copy_from: Path, paste_to: Path, dailies: str) -> list:
    """List of (source file, destination file) pairs for one shot."""
    jobs = []
    for unit in shot.scan.units:
        if mode == MODE_LOCAL:
            base = paste_to / unit.path.relative_to(copy_from)
        elif dailies:
            base = paste_to / dailies / unit.path.name
        else:
            base = paste_to / unit.path.name
        for src in unit.copy_files:
            jobs.append((src, base / src.relative_to(unit.path)))
    return jobs


def _remove_quiet(path: Path):
    try:
        path.unlink()
    except OSError:
        pass


def copy_file(src: Path, dst: Path, overwrite: bool,
              paused: threading.Event, stop: threading.Event) -> str:
    """Copy one file in 8 MB chunks. Returns copied / skipped / stopped.

    Data is written to <name>.part first and renamed when complete, so a
    cancelled copy never leaves a half-written file behind.
    """
    if stop.is_set():
        return "stopped"
    if dst.exists() and not overwrite:
        return "skipped"
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
                n = fin.readinto(view)
                if not n:
                    break
                fout.write(view[:n])
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


def load_csv(path: Path) -> list:
    """Read the Name and Frames columns of a Kitsu CSV (';' or ',' separated)."""
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
    frames_idx = header.index("frames") if "frames" in header else None

    shots, seen = [], set()
    for row in rows[1:]:
        if len(row) <= name_idx:
            continue
        name = row[name_idx].strip()
        if not name or name in seen or "/" in name or "\\" in name:
            continue
        seen.add(name)
        frames = 0
        if frames_idx is not None and frames_idx < len(row):
            try:
                frames = int(float(row[frames_idx].strip().replace(",", ".") or 0))
            except ValueError:
                frames = 0
        shots.append(Shot(name=name, frames=frames))
    return shots


# ----------------------------------------------------------------------------
# Status / colour rules
# ----------------------------------------------------------------------------
def unit_style(found: int, frames: int) -> dict:
    if found == 0:
        return {"color": C_YELLOW}                       # no files after scan
    if found != frames:
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
    if shot.scan is not None:
        return unit_style(shot.scan.found, shot.frames)
    return {}


def status_text(shot: Shot) -> str:
    if shot.state in ("copying", "done", "partial", "error"):
        return f"{shot.done}/{shot.frames}"
    found = shot.scan.found if shot.scan else 0
    return f"{found}/{shot.frames}"


def missing_tooltip(shot: Shot) -> str:
    if not shot.scan or shot.scan.found == 0:
        return ""
    if shot.scan.missing:
        shown = ", ".join(str(n) for n in shot.scan.missing[:60])
        if len(shot.scan.missing) > 60:
            shown += ", ..."
        return f"Missing frames ({len(shot.scan.missing)}): {shown}"
    if shot.scan.found != shot.frames:
        return f"Found {shot.scan.found} file(s), CSV Frames = {shot.frames}"
    return ""


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

    def __init__(self, copy_from: Path, name: str):
        super().__init__()
        self.copy_from = copy_from
        self.name = name

    def run(self):
        try:
            path = find_first_folder(self.copy_from, self.name)
        except OSError:
            path = None
        self.found.emit(str(self.copy_from), self.name, str(path) if path else "")


class ScanWorker(QThread):
    """Scans the source tree in the background so the GUI stays responsive."""
    finished_scan = Signal(object, str)     # (results list, error message)

    def __init__(self, names, copy_from: Path, mode: str, exts, skip_system: bool):
        super().__init__()
        self.names = names
        self.copy_from = copy_from
        self.mode = mode
        self.exts = tuple(exts)
        self.skip_system = skip_system

    def run(self):
        try:
            matched = scan_source(self.copy_from, self.names)
            results = [analyze_shot(n, matched[n], self.mode, self.exts, self.skip_system)
                       for n in self.names]
            self.finished_scan.emit(results, "")
        except Exception as exc:            # report to the GUI instead of crashing
            self.finished_scan.emit([], str(exc))


class CopyWorker(QThread):
    """Copies the planned shots one after another, files in parallel per shot."""
    shot_started = Signal(int, int)         # shot index, file count
    shot_progress = Signal(int, int)        # shot index, files done
    shot_finished = Signal(int, str)        # shot index, done / partial / error
    overall = Signal(int, int)              # files done, files total
    log = Signal(str)

    def __init__(self, plans):
        super().__init__()
        # plans: list of (shot index, frames, jobs, overwrite)
        self.plans = plans
        self.pause_event = threading.Event()    # set = paused
        self.stop_event = threading.Event()     # set = stop requested

    def run(self):
        total = sum(len(p[2]) for p in self.plans)
        done_all = 0
        for index, _frames, jobs, overwrite in self.plans:
            if self.stop_event.is_set():
                break
            self.shot_started.emit(index, len(jobs))
            done = errors = 0
            executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
            futures = [executor.submit(copy_file, src, dst, overwrite,
                                       self.pause_event, self.stop_event)
                       for src, dst in jobs]
            try:
                for future in as_completed(futures):
                    if self.stop_event.is_set():
                        break
                    try:
                        result = future.result()
                    except Exception as exc:
                        errors += 1
                        self.log.emit(f"  error: {exc}")
                        continue
                    if result == "stopped":
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
    """Line edit that accepts a folder dragged from Explorer / Finder / file manager."""

    def __init__(self, placeholder: str = ""):
        super().__init__()
        self.setAcceptDrops(True)
        self.setPlaceholderText(placeholder)

    def dragEnterEvent(self, event):
        if local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        paths = local_paths(event.mimeData())
        if paths:
            p = paths[0]
            self.setText(str(p if p.is_dir() else p.parent))
            event.acceptProposedAction()


class DropZone(QFrame):
    """DROP CSV FILE box: drag a CSV onto it or click to browse."""
    clicked = Signal()
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(90)
        self.setStyleSheet("#dropZone { border: 2px dashed #7a7f87; border-radius: 6px; }")
        layout = QVBoxLayout(self)
        label = QLabel("DROP CSV FILE\n(or click to browse)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("font-size: 20px; font-weight: bold; border: none;")
        layout.addWidget(label)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):
        if local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        self.dropped.emit(local_paths(event.mimeData()))
        event.acceptProposedAction()


class ShotTree(QTreeWidget):
    """SHOTS LIST. Accepts dropped folders (new shots) and CSV files (more shots)."""
    dropped = Signal(list)
    deletePressed = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.deletePressed.emit()
            return
        super().keyPressEvent(event)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

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
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
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
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._run_preview_search)
        self._scanning = False
        self._running = False
        self._build_ui()
        self._update_previews()
        self._update_controls()
        self.log(f"{APP_NAME} {APP_VERSION} ready. Drop a CSV file to start.")

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

        self.drop_zone = DropZone()
        self.drop_zone.clicked.connect(self.browse_csv)
        self.drop_zone.dropped.connect(self.on_csv_dropped)
        left.addWidget(self.drop_zone)

        paths_box = QGroupBox()
        grid = QGridLayout(paths_box)
        self.copy_edit = PathEdit("Source folder, e.g. W:\\_PROJECTS\\...\\FILM")
        self.copy_btn = QPushButton("...")
        self.copy_btn.setFixedWidth(44)
        self.paste_edit = PathEdit("Destination folder")
        self.paste_btn = QPushButton("...")
        self.paste_btn.setFixedWidth(44)
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
        left.addWidget(paths_box)
        self.copy_edit.textChanged.connect(self._update_previews)
        self.paste_edit.textChanged.connect(self._update_previews)
        self.copy_btn.clicked.connect(lambda: self.browse_dir(self.copy_edit))
        self.paste_btn.clicked.connect(lambda: self.browse_dir(self.paste_edit))

        row = QHBoxLayout()
        row.setSpacing(12)
        left.addLayout(row)

        # LOCAL box
        local_box = QGroupBox()
        lv = QVBoxLayout(local_box)
        lv.setSpacing(10)
        self.btn_local = self._mode_button("LOCAL")
        self.btn_local.clicked.connect(lambda: self.start_scan(MODE_LOCAL))
        lv.addWidget(self.btn_local)
        self.local_checks = self._type_checks(LOCAL_TYPE_KEYS, lv)
        lv.addStretch(1)
        row.addWidget(local_box, 1)

        # PUBLISH box
        publish_box = QGroupBox()
        pv = QVBoxLayout(publish_box)
        pv.setSpacing(10)
        self.btn_publish = self._mode_button("PUBLISH")
        self.btn_publish.clicked.connect(lambda: self.start_scan(MODE_PUBLISH))
        pv.addWidget(self.btn_publish)
        self.chk_dailies = QCheckBox("MAKE DAILIES FOLDER")
        self.chk_dailies.toggled.connect(self._update_previews)
        pv.addWidget(self.chk_dailies)
        self.publish_checks = self._type_checks(PUBLISH_TYPE_KEYS, pv)
        pv.addStretch(1)
        row.addWidget(publish_box, 1)

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
        mv.addWidget(self.chk_skip)
        mv.addWidget(self.chk_overwrite)
        mv.addWidget(self.chk_skip_sys)
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
        title = QLabel("SHOTS LIST")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        right.addWidget(title)
        self.tree = ShotTree()
        self.tree.setHeaderLabels(["NO", "NAME", "VERSION", "FRAMES", "STATUS"])
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.dropped.connect(self.on_tree_dropped)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemCollapsed.connect(self._on_collapsed)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 60)
        self.tree.setColumnWidth(2, 140)
        self.tree.setColumnWidth(3, 80)
        self.tree.setColumnWidth(4, 120)
        right.addWidget(self.tree, 1)

        tree_buttons = QHBoxLayout()
        self.btn_clear = QPushButton("CLEAR")
        self.btn_clear.clicked.connect(self.clear_shots)
        self.btn_add = QPushButton("ADD")
        add_menu = QMenu(self.btn_add)
        add_menu.addAction("Add shot folder...", self.add_folder_shot)
        add_menu.addAction("Add shot by name...", self.add_named_shot)
        self.btn_add.setMenu(add_menu)
        self.btn_remove = QPushButton("REMOVE")
        self.btn_remove.clicked.connect(self.remove_selected)
        for b in (self.btn_add, self.btn_remove, self.btn_clear):
            b.setMinimumHeight(34)
            tree_buttons.addWidget(b)
        tree_buttons.addStretch(1)
        right.addLayout(tree_buttons)

        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_tree_context_menu)
        self.tree.deletePressed.connect(self.remove_selected)

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
    def log(self, msg: str):
        self.log_view.appendPlainText(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _busy(self) -> bool:
        return self._scanning or self._running

    def _copy_from(self):
        text = self.copy_edit.text().strip().strip('"').strip()
        return Path(text) if text else None

    def _paste_to(self):
        text = self.paste_edit.text().strip().strip('"').strip()
        return Path(text) if text else None

    def _selected_exts(self, mode: str) -> tuple:
        checks = self.local_checks if mode == MODE_LOCAL else self.publish_checks
        exts = set()
        for key, cb in checks.items():
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
        self.btn_local.setEnabled(not busy)
        self.btn_publish.setEnabled(not busy)
        self.btn_start.setEnabled(not busy)
        self.btn_pause.setEnabled(self._running)
        self.btn_stop.setEnabled(self._running)
        self.btn_clear.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.btn_add.setEnabled(not self._scanning)
        self.drop_zone.setEnabled(not self._running)
        for w in (self.copy_edit, self.paste_edit, self.copy_btn, self.paste_btn,
                  self.chk_dailies, self.chk_skip, self.chk_overwrite, self.chk_skip_sys,
                  *self.local_checks.values(), *self.publish_checks.values()):
            w.setEnabled(not self._running)

    # ---------- CSV and shot list ----------
    def browse_csv(self):
        if self._busy():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open Kitsu CSV", "",
                                              "CSV files (*.csv);;All files (*)")
        if path:
            self.load_csv_path(Path(path), replace=True)

    def browse_dir(self, edit: PathEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select folder", edit.text().strip())
        if folder:
            edit.setText(folder)

    def on_csv_dropped(self, paths: list):
        csvs = [p for p in paths if p.suffix.lower() == ".csv"]
        if csvs:
            self.load_csv_path(csvs[0], replace=True)
        else:
            self.log("Please drop a .csv file here.")

    def load_csv_path(self, path: Path, replace: bool):
        if self._busy():
            self.log("Cannot load a CSV while scanning or copying.")
            return
        try:
            new_shots = load_csv(path)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not read CSV:\n{exc}")
            return
        if replace:
            self.shots = new_shots
            self.scan_config = None
            self.current_mode = MODE_LOCAL
        else:
            known = {s.name for s in self.shots}
            self.shots.extend(s for s in new_shots if s.name not in known)
        self.log(f"Loaded {len(new_shots)} shot(s) from {path.name}")
        self.refresh_tree()
        self._update_previews()

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
            elif p.suffix.lower() == ".csv":
                self.load_csv_path(p, replace=False)
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
            index = owner.data(1, ROLE_INDEX)
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
        if self._busy() or not self.shots:
            return
        answer = QMessageBox.question(self, APP_NAME, "Clear the whole shot list?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.shots = []
        self.scan_config = None
        self.current_mode = MODE_LOCAL
        self._expanded.clear()
        self.log("Shot list cleared.")
        self.refresh_tree()
        self._update_previews()

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
                self._preview_cache[key] = find_first_folder(copy_from, shot.name)
            finally:
                QApplication.restoreOverrideCursor()
        return self._preview_cache[key]

    def _item_path(self, item: QTreeWidgetItem):
        """Folder that a row points to: the variant folder, or the shot's folder."""
        if item.parent() is not None:
            value = item.data(1, ROLE_PATH)
            return Path(value) if value else None
        index = item.data(1, ROLE_INDEX)
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
        for i, shot in enumerate(self.shots):
            top = QTreeWidgetItem(self.tree)
            top.setData(0, Qt.ItemDataRole.UserRole, shot.name)
            top.setData(1, ROLE_INDEX, i)
            self.items[i] = top
            self._fill_top(top, i, shot)
            if shot.scan and len(shot.scan.units) > 1:
                for unit in shot.scan.units:
                    child = QTreeWidgetItem(top)
                    child.setText(1, unit.path.name)
                    child.setData(1, ROLE_PATH, str(unit.path))
                    child.setText(2, unit.label)
                    child.setText(3, str(shot.frames))
                    child.setText(4, f"{unit.found}/{shot.frames}")
                    child.setToolTip(4, f"Missing: {unit.gaps[:60]}" if unit.gaps else "")
                    apply_style(child, unit_style(unit.found, shot.frames))
                top.setExpanded(shot.name in self._expanded)

    def _fill_top(self, item: QTreeWidgetItem, index: int, shot: Shot):
        item.setText(0, str(index + 1))
        item.setText(1, shot.name)
        item.setText(2, shot.scan.version_text if shot.scan else "")
        item.setText(3, str(shot.frames))
        item.setText(4, status_text(shot))
        item.setToolTip(4, missing_tooltip(shot))
        apply_style(item, row_style(shot))

    def _refresh_row(self, index: int):
        item = self.items.get(index)
        if item is not None and index < len(self.shots):
            self._fill_top(item, index, self.shots[index])

    def _on_expanded(self, item: QTreeWidgetItem):
        name = item.data(0, Qt.ItemDataRole.UserRole)
        if name:
            self._expanded.add(name)

    def _on_collapsed(self, item: QTreeWidgetItem):
        name = item.data(0, Qt.ItemDataRole.UserRole)
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
        self.log(f"{mode}: scanning {len(self.shots)} shot(s) under {copy_from} ...")
        self.scan_worker = ScanWorker([s.name for s in self.shots], copy_from,
                                      mode, config[2], config[3])
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
            ok = sum(1 for s in self.shots if s.scan and s.scan.found == s.frames and s.scan.found)
            mism = sum(1 for s in self.shots if s.scan and s.scan.found and s.scan.found != s.frames)
            none = sum(1 for s in self.shots if not s.scan or s.scan.found == 0)
            self.log(f"Scan done: {ok} ready, {mism} mismatch (skipped), {none} without files")
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
        overwrite_all = self.chk_overwrite.isChecked()
        plans, total = [], 0
        for i, shot in enumerate(self.shots):
            # Shots with a count mismatch, no files, or already done are skipped
            if (shot.scan is None or shot.state == "done" or shot.scan.found == 0
                    or shot.scan.found != shot.frames):
                continue
            jobs = build_jobs(shot, mode, copy_from, paste_to, dailies)
            if not jobs:
                continue
            plans.append((i, shot.frames, jobs, overwrite_all or shot.force_overwrite))
            total += len(jobs)
        if not plans:
            QMessageBox.information(self, APP_NAME,
                                    "No shot is ready to copy.\n"
                                    "Shots with mismatched frames or no files are skipped.")
            return

        answer = QMessageBox.question(
            self, APP_NAME,
            f"{mode}: copy {len(plans)} shot(s), {total} file(s)?\n\n"
            f"Mode: {'OVERWRITE' if overwrite_all else 'SKIP existing files'}\n"
            f"To: {paste_to}{os.sep}" + (f"{dailies}{os.sep}" if dailies else ""))
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self.worker = CopyWorker(plans)
        self.worker.shot_started.connect(self._on_shot_started)
        self.worker.shot_progress.connect(self._on_shot_progress)
        self.worker.shot_finished.connect(self._on_shot_finished)
        self.worker.overall.connect(self.progress.setValue)
        self.worker.log.connect(self.log)
        self.worker.finished.connect(self._on_worker_finished)
        self._running = True
        self.btn_pause.setText("PAUSE")
        self._update_controls()
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
        self.worker.stop_event.set()
        self.worker.pause_event.clear()
        self.btn_pause.setText("PAUSE")
        self.log("Stopping... the file in progress will be removed and "
                 "the shot re-copied with overwrite on the next START.")

    # ---------- worker callbacks (run in the GUI thread) ----------
    def _on_shot_started(self, index: int, _total: int):
        shot = self.shots[index]
        shot.state = "copying"
        shot.done = 0
        self._refresh_row(index)
        self.log(f"Copying {shot.name} ({_total} file(s))")

    def _on_shot_progress(self, index: int, done: int):
        self.shots[index].done = done
        self._refresh_row(index)

    def _on_shot_finished(self, index: int, result: str):
        shot = self.shots[index]
        shot.state = result
        if result == "done":
            shot.force_overwrite = False
        elif result == "partial":
            shot.force_overwrite = True
        self._refresh_row(index)
        self.log(f"{shot.name}: {result}")

    def _on_worker_finished(self):
        stopped = self.worker.stop_event.is_set() if self.worker else False
        self._running = False
        self.log("Stopped by user." if stopped else "All shots finished.")
        self.refresh_tree()
        self._update_controls()

    def closeEvent(self, event):
        if self._running and self.worker is not None:
            self.worker.stop_event.set()
            self.worker.pause_event.clear()
            self.worker.wait(10000)
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
        self._preview_worker = PreviewWorker(copy_from, self.shots[0].name)
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
