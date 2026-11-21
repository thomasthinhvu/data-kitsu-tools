#!/usr/bin/env python3
"""
Shot-list import helpers (Excel .xlsx/.xlsm, Image OCR .png/.jpg with 2D table
reconstruction, smart column inference, and first-frame JPEG capture) for
Data Kitsu Tools 2.0.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

SHOT_HEADER_ALIASES = {
    "name", "shot", "shot code", "shot_code", "shotcode", "shotname", "shot name",
    "shot_name", "vfx_name", "vfx name", "code",
}
SEQ_HEADER_ALIASES = {"sequence", "seq", "scene", "phase", "reel", "episode", "ep"}
FRAME_HEADER_ALIASES = {
    "frames", "frame", "frame count", "frame_count", "duration", "dur", "length",
    "cut duration", "cut_duration",
}
START_FRAME_ALIASES = {"start frame", "start_frame", "frame_in", "frame in", "cut in", "cut_in", "in", "start"}
END_FRAME_ALIASES = {"end frame", "end_frame", "frame_out", "frame out", "cut out", "cut_out", "out", "end"}

RANGE_RE = re.compile(r"^\s*(\d+)\s*[-–—]\s*(\d+)\s*$")
SCENE_HEADER_RE = re.compile(
    r"^\s*(?:scene|seq|sequence|phase|reel)\s*[_\-\s]*(\d+[a-zA-Z]?)\b",
    re.IGNORECASE,
)
GENERIC_SHOT_RE = re.compile(r"^(?:shot[_\-\s]*\d+|\d+(?:[._\-][a-zA-Z0-9]+)?)$", re.IGNORECASE)
SHOT_CELL_RE = re.compile(
    r"^(?:"
    r"[a-zA-Z]{2,12}_\d{2,5}(?:_\d{2,5})?"          # BES_010, bunny_010_0010, HHCC_010
    r"|\d{2,4}_[a-zA-Z][a-zA-Z0-9]{1,15}_\d{2,5}"   # 005_MAMMOTH_0020
    r"|[a-zA-Z]{2,8}_[a-zA-Z0-9]{1,8}_[a-zA-Z0-9]{1,8}(?:_\d{2,5})?"  # HHCC_SC01_010
    r"|[a-zA-Z]{1,5}\d{2,5}(?:[._\-]\d{2,4})?"      # MK8010, SH010
    r"|(?:shot|sh)[_\-\s]?\d{1,5}[a-zA-Z]?"         # shot_01, Shot_001
    r")$",
    re.IGNORECASE,
)
OCR_SHOT_TOKEN_RE = re.compile(
    r"\b(?:"
    r"\d{2,4}[_\-\s][a-zA-Z][a-zA-Z0-9]{1,15}[_\-\s]\d{2,5}(?:[._\-][a-zA-Z0-9]+)*"
    r"|[a-zA-Z]{2,12}[_\-]\d{2,5}(?:[_\-]\d{2,5})?(?:[._\-][a-zA-Z0-9]+)*"
    r"|[a-zA-Z]{2,8}[_\-][a-zA-Z0-9]{1,8}[_\-][a-zA-Z0-9]{1,8}(?:[_\-]\d{2,5})?(?:[._\-][a-zA-Z0-9]+)*"
    r"|[a-zA-Z]{1,5}\d{2,5}(?:[._\-]\d{2,4})?"
    r"|(?:shot|sh)[_\-\s]?\d{1,5}[a-zA-Z]?"
    r")\b",
    re.IGNORECASE,
)


def _find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if os.name == "nt":
        for pattern in (
            r"C:\ffmpeg*\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg*\bin\ffmpeg.exe",
        ):
            matches = sorted(glob.glob(pattern), reverse=True)
            if matches:
                return matches[0]
    return None


def capture_first_frame_jpg(files: list[Path], out_path: Path) -> Path | None:
    """Convert the first frame of *files* to a JPEG at *out_path*."""
    if not files:
        return None
    candidates = sorted((Path(f) for f in files), key=lambda p: p.name.lower())
    first = next((p for p in candidates if p.is_file()), None)
    if first is None:
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ext = first.suffix.lower()

    if ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"):
        img = QImage(str(first))
        if not img.isNull():
            if img.width() > 1920:
                img = img.scaledToWidth(1920, Qt.TransformationMode.SmoothTransformation)
            if img.save(str(out_path), "JPG", 90) and out_path.is_file() and out_path.stat().st_size > 0:
                return out_path

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None

    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    if ext == ".exr":
        cmd += ["-apply_trc", "iec61966_2_1"]
    cmd += ["-i", str(first), "-vframes", "1", "-vf", "scale='min(1920,iw)':-2", "-q:v", "3", str(out_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
        if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def render_preview_mp4(files: list[Path], out_path: Path, fps: float = 24.0) -> Path | None:
    """Encode an image sequence or movie into a lightweight H.264 MP4 preview for Kitsu."""
    if not files:
        return None
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None

    candidates = sorted((Path(f) for f in files if Path(f).is_file()), key=lambda p: p.name.lower())
    if not candidates:
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    first_ext = candidates[0].suffix.lower()

    if first_ext in (".mov", ".mp4", ".m4v", ".avi", ".mkv"):
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(candidates[0]),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", "scale='min(1280,iw)':-2",
            "-preset", "fast", "-crf", "23", "-an",
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=90, check=False)
            if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                return out_path
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    img_exts = {".exr", ".dpx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    seq_files = [p for p in candidates if p.suffix.lower() in img_exts]
    if not seq_files:
        return None

    dur = 1.0 / max(1.0, float(fps))
    concat_file = out_path.with_suffix(".concat.txt")
    try:
        lines = []
        for p in seq_files:
            escaped = str(p.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
            lines.append(f"duration {dur:.6f}")
        # Repeat last frame per ffmpeg concat demuxer spec
        escaped_last = str(seq_files[-1].resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped_last}'")
        concat_file.write_text("\n".join(lines), encoding="utf-8")

        cmd = [ffmpeg, "-y", "-loglevel", "error"]
        if first_ext == ".exr":
            cmd += ["-apply_trc", "iec61966_2_1"]
        cmd += [
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-vsync", "vfr",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", "scale='min(1280,iw)':-2",
            "-preset", "fast", "-crf", "23", "-an",
            "-movflags", "+faststart",
            str(out_path),
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        try:
            if concat_file.exists():
                concat_file.unlink()
        except OSError:
            pass
    return None


def parse_frame_value(raw: str) -> int | None:
    """Parse an integer frame count or a ``start-end`` range like ``1-286``."""
    text = str(raw or "").strip()
    if not text:
        return None
    m = RANGE_RE.match(text)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        return (end - start + 1) if end >= start else None
    try:
        val = int(float(text.replace(",", ".")))
        return val if val > 0 else None
    except ValueError:
        return None


def _normalize_scene_tag(text: str) -> str:
    m = SCENE_HEADER_RE.match(text)
    if not m:
        return ""
    prefix = text[: m.start(1)].strip().rstrip("_- ").split()[-1].capitalize()
    return f"{prefix}_{m.group(1)}"


def _clean_shot_token(name: str, current_seq: str) -> str:
    name = re.sub(r"\s+", "_", name.strip())
    if not name or "/" in name or "\\" in name:
        return ""
    if current_seq and GENERIC_SHOT_RE.match(name):
        if "_" in current_seq or SCENE_HEADER_RE.match(current_seq.replace("_", " ")):
            if not name.lower().startswith(current_seq.lower() + "_"):
                return f"{current_seq}_{name}"
    return name


def detect_table_columns(rows: list[list[str]]) -> dict:
    """Infer Shot Name, Sequence, Frames, Start/End columns from a 2D table.

    Returns ``confident=True`` when a clear header or high-confidence shot column
    is found; otherwise ``confident=False`` so the UI can ask the user.
    """
    if not rows:
        return {"confident": False, "header_row": None, "name_col": -1, "seq_col": -1,
                "frames_col": -1, "start_col": -1, "end_col": -1}

    for idx, row in enumerate(rows[:20]):
        lowered = [str(c or "").strip().lower() for c in row]
        name_col = next((i for i, h in enumerate(lowered) if h in SHOT_HEADER_ALIASES), None)
        if name_col is not None:
            seq_col = next((i for i, h in enumerate(lowered) if h in SEQ_HEADER_ALIASES), -1)
            frames_col = next(
                (i for i, h in enumerate(lowered) if h in FRAME_HEADER_ALIASES or h.startswith("duration")),
                -1,
            )
            start_col = next((i for i, h in enumerate(lowered) if h in START_FRAME_ALIASES), -1)
            end_col = next((i for i, h in enumerate(lowered) if h in END_FRAME_ALIASES), -1)
            return {
                "confident": True,
                "header_row": idx,
                "name_col": name_col,
                "seq_col": seq_col,
                "frames_col": frames_col,
                "start_col": start_col,
                "end_col": end_col,
            }

    # No exact header match: score columns heuristically by cell content
    num_cols = max((len(r) for r in rows), default=0)
    best_name_col, best_name_score = -1, 0
    best_frame_col, best_frame_score = -1, 0
    for c in range(num_cols):
        shot_hits = 0
        frame_hits = 0
        for r in rows:
            val = str(r[c]).strip() if c < len(r) else ""
            if not val:
                continue
            if SHOT_CELL_RE.match(val):
                shot_hits += 1
            elif parse_frame_value(val) is not None and (RANGE_RE.match(val) or val.isdigit()):
                if val.isdigit() and int(val) > 5:
                    frame_hits += 1
                elif RANGE_RE.match(val):
                    frame_hits += 2
        if shot_hits > best_name_score:
            best_name_col, best_name_score = c, shot_hits
        if frame_hits > best_frame_score:
            best_frame_col, best_frame_score = c, frame_hits

    return {
        "confident": best_name_score >= 3,
        "header_row": None,
        "name_col": best_name_col,
        "seq_col": -1,
        "frames_col": best_frame_col if best_frame_col != best_name_col else -1,
        "start_col": -1,
        "end_col": -1,
    }


def extract_shots_from_table_with_mapping(
    rows: list[list[str]],
    name_col: int,
    seq_col: int = -1,
    frames_col: int = -1,
    start_col: int = -1,
    end_col: int = -1,
    header_row: int | None = 0,
) -> list[dict]:
    """Extract shot dicts from *rows* using explicit column indices."""
    if not rows or name_col < 0:
        return []

    start_idx = (header_row + 1) if header_row is not None else 0

    def cell(row: list[str], cidx: int) -> str:
        return str(row[cidx]).strip() if 0 <= cidx < len(row) else ""

    records: list[dict] = []
    seen: set[str] = set()
    current_seq = ""

    for row in rows[start_idx:]:
        raw_name = cell(row, name_col)
        if not raw_name:
            continue
        low_name = raw_name.lower()
        if low_name in SHOT_HEADER_ALIASES:
            continue

        seq_val = cell(row, seq_col)
        if seq_val and not seq_val.isdigit():
            current_seq = seq_val
        elif seq_val and seq_val.isdigit():
            seq_val = f"R{seq_val}"

        frames = parse_frame_value(cell(row, frames_col)) if frames_col >= 0 else None
        if frames is None and start_col >= 0 and end_col >= 0:
            s_val = parse_frame_value(cell(row, start_col))
            e_val = parse_frame_value(cell(row, end_col))
            if s_val is not None and e_val is not None and e_val >= s_val:
                frames = e_val - s_val + 1

        if frames is None and SCENE_HEADER_RE.match(raw_name):
            current_seq = _normalize_scene_tag(raw_name)
            continue

        seq = seq_val or current_seq
        name = _clean_shot_token(raw_name, seq)
        if not name or name in seen:
            continue
        seen.add(name)
        records.append({"name": name, "frames": frames, "seq": seq})
    return records


def parse_shots_from_table(rows: list[list[str]]) -> list[dict]:
    """Extract shot dicts (name, frames, seq) from 2D table rows (Excel/CSV)."""
    if not rows:
        return []
    info = detect_table_columns(rows)
    if info["name_col"] >= 0:
        return extract_shots_from_table_with_mapping(
            rows,
            name_col=info["name_col"],
            seq_col=info["seq_col"],
            frames_col=info["frames_col"],
            start_col=info["start_col"],
            end_col=info["end_col"],
            header_row=info["header_row"],
        )
    return parse_shots_from_text("\n".join(" ".join(str(c) for c in r if c) for r in rows))


def parse_shots_from_text(text: str) -> list[dict]:
    """Extract shot dicts from free-form or OCR-extracted text."""
    records: list[dict] = []
    seen: set[str] = set()
    current_seq = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if SCENE_HEADER_RE.match(line) and not OCR_SHOT_TOKEN_RE.search(line):
            current_seq = _normalize_scene_tag(line)
            continue

        m = OCR_SHOT_TOKEN_RE.search(line)
        if not m:
            if SCENE_HEADER_RE.match(line):
                current_seq = _normalize_scene_tag(line)
            continue

        token = m.group(0)
        rest = line[m.end() :]
        frames = None
        range_m = re.search(r"\b(\d{1,5})\s*[-–—]\s*(\d{1,5})\b", rest)
        if range_m:
            s_val, e_val = int(range_m.group(1)), int(range_m.group(2))
            if e_val >= s_val:
                frames = e_val - s_val + 1
        else:
            nums = [int(n) for n in re.findall(r"\b(\d{1,5})\b", rest)]
            if len(nums) >= 2 and nums[0] <= nums[1] and nums[0] in (0, 1, 1001):
                frames = nums[1] - nums[0] + 1
            elif nums:
                frames = nums[-1] if nums[-1] > 0 else None

        name = _clean_shot_token(token, current_seq)
        if not name or name in seen:
            continue
        seen.add(name)
        records.append({"name": name, "frames": frames, "seq": current_seq})
    return records


def _col_letters_to_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def read_excel_rows(path: Path) -> list[list[str]]:
    """Read the first worksheet of an ``.xlsx`` / ``.xlsm`` file using stdlib XML."""
    ext = path.suffix.lower()
    if ext == ".xls":
        try:
            import xlrd  # type: ignore

            wb = xlrd.open_workbook(str(path))
            sh = wb.sheet_by_index(0)
            return [[str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)] for r in range(sh.nrows)]
        except ImportError as exc:
            raise ValueError("Legacy .xls files require xlrd, or save as .xlsx.") from exc

    with zipfile.ZipFile(path, "r") as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.iter():
                if si.tag.endswith("}si") or si.tag == "si":
                    parts = [t.text or "" for t in si.iter() if t.tag.endswith("}t") or t.tag == "t"]
                    shared.append("".join(parts))

        sheets = sorted(n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            raise ValueError("No worksheet found inside this Excel file.")

        root = ET.fromstring(zf.read(sheets[0]))
        rows: list[list[str]] = []
        for row_el in root.iter():
            if not (row_el.tag.endswith("}row") or row_el.tag == "row"):
                continue
            row_cells: dict[int, str] = {}
            max_col = -1
            for c_el in row_el:
                if not (c_el.tag.endswith("}c") or c_el.tag == "c"):
                    continue
                ref = c_el.attrib.get("r", "")
                cidx = _col_letters_to_index(ref) if ref else (max_col + 1)
                max_col = max(max_col, cidx)
                ctype = c_el.attrib.get("t", "")
                val = ""
                if ctype == "inlineStr":
                    val = "".join(t.text or "" for t in c_el.iter() if t.tag.endswith("}t") or t.tag == "t")
                else:
                    v_el = next((ch for ch in c_el if ch.tag.endswith("}v") or ch.tag == "v"), None)
                    if v_el is not None and v_el.text is not None:
                        if ctype == "s":
                            s_idx = int(v_el.text)
                            val = shared[s_idx] if 0 <= s_idx < len(shared) else ""
                        else:
                            val = v_el.text
                row_cells[cidx] = val.strip()
            if max_col >= 0:
                rows.append([row_cells.get(i, "") for i in range(max_col + 1)])
        return rows


def _prepare_ocr_image(src: Path, dst: Path) -> Path:
    """Normalize pixel format, upscale small screenshots, and convert to clean grayscale for OCR."""
    img = QImage(str(src))
    if img.isNull():
        return src
    if img.width() < 1400:
        scale = 3 if img.width() < 700 else 2
        img = img.scaledToWidth(img.width() * scale, Qt.TransformationMode.SmoothTransformation)
    elif img.width() > 3800:
        img = img.scaledToWidth(3800, Qt.TransformationMode.SmoothTransformation)
    img = img.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)
    if img.save(str(dst), "PNG"):
        return dst
    return src


def reconstruct_table_from_words(words: list[dict]) -> list[list[str]]:
    """Reconstruct 2D table rows/columns from OCR word bounding boxes."""
    if not words:
        return []

    heights = [float(w.get("h") or 12.0) for w in words if float(w.get("h") or 0) > 0]
    med_h = statistics.median(heights) if heights else 14.0
    row_tol = max(6.0, med_h * 0.65)
    cell_gap = max(8.0, med_h * 1.15)
    col_tol = max(18.0, med_h * 2.2)

    sorted_words = sorted(words, key=lambda w: (float(w["y"]) + float(w["h"]) * 0.5, float(w["x"])))
    row_groups: list[list[dict]] = []
    for w in sorted_words:
        yc = float(w["y"]) + float(w["h"]) * 0.5
        if not row_groups:
            row_groups.append([w])
            continue
        last_yc = statistics.mean(float(it["y"]) + float(it["h"]) * 0.5 for it in row_groups[-1])
        if abs(yc - last_yc) <= row_tol:
            row_groups[-1].append(w)
        else:
            row_groups.append([w])

    # Merge adjacent words in each row into cells
    row_cells: list[list[tuple[float, str]]] = []
    all_x: list[float] = []
    for rg in row_groups:
        rg.sort(key=lambda w: float(w["x"]))
        cells: list[tuple[float, float, str]] = []  # (x_start, x_end, text)
        for w in rg:
            wx, ww, txt = float(w["x"]), float(w["w"]), str(w["text"]).strip()
            if not txt:
                continue
            if cells and (wx - cells[-1][1]) <= cell_gap:
                prev_x, _prev_end, prev_txt = cells[-1]
                cells[-1] = (prev_x, wx + ww, f"{prev_txt} {txt}")
            else:
                cells.append((wx, wx + ww, txt))
        if cells:
            row_cells.append([(cx, ctxt) for cx, _ce, ctxt in cells])
            all_x.extend(cx for cx, _ce, _ctxt in cells)

    if not row_cells:
        return []

    # Cluster cell X positions into column anchors
    all_x.sort()
    col_anchors: list[float] = []
    for x in all_x:
        if not col_anchors or abs(x - col_anchors[-1]) > col_tol:
            col_anchors.append(x)
        else:
            col_anchors[-1] = (col_anchors[-1] + x) * 0.5

    table: list[list[str]] = []
    num_cols = len(col_anchors)
    for cells in row_cells:
        row_out = [""] * num_cols
        for cx, ctxt in cells:
            best_c = min(range(num_cols), key=lambda c: abs(cx - col_anchors[c]))
            row_out[best_c] = f"{row_out[best_c]} {ctxt}".strip() if row_out[best_c] else ctxt
        if any(row_out):
            table.append(row_out)
    return table


def extract_ocr_data_from_image(path: Path) -> tuple[str, list[list[str]]]:
    """Extract both plain text and reconstructed 2D table from a PNG/JPG image."""
    last_err = ""
    if os.name == "nt":
        ps_script = (
            "$ErrorActionPreference = 'Stop'\n"
            "Add-Type -AssemblyName System.Runtime.WindowsRuntime\n"
            "$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | "
            "Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and "
            "$_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]\n"
            "function Await($op, $type) { "
            "$t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); "
            "$t.Wait(); return $t.Result }\n"
            "[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null\n"
            "[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null\n"
            "[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null\n"
            "[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null\n"
            "[Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null\n"
            "$imgPath = $env:DKT_OCR_IMG\n"
            "$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])\n"
            "$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])\n"
            "$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])\n"
            "$bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])\n"
            "$ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()\n"
            "if ($null -eq $ocr) { $ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US')) }\n"
            "if ($null -eq $ocr) { "
            "$langs = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages; "
            "if ($langs.Count -gt 0) { $ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($langs[0]) } "
            "}\n"
            "if ($null -eq $ocr) { throw 'No Windows OCR language pack installed.' }\n"
            "$res = Await ($ocr.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])\n"
            "$words = @()\n"
            "foreach ($line in $res.Lines) {\n"
            "  foreach ($w in $line.Words) {\n"
            "    $r = $w.BoundingRect\n"
            "    $words += [PSCustomObject]@{ text=$w.Text; x=$r.X; y=$r.Y; w=$r.Width; h=$r.Height }\n"
            "  }\n"
            "}\n"
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "@($words) | ConvertTo-Json -Compress\n"
        )
        with tempfile.TemporaryDirectory(prefix="dkt_ocr_") as td:
            norm_img = _prepare_ocr_image(path.resolve(), Path(td) / "ocr_input.png")
            ps_path = Path(td) / "run_ocr.ps1"
            ps_path.write_text(ps_script, encoding="utf-8")
            env = dict(os.environ, DKT_OCR_IMG=str(norm_img.resolve()))
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=30,
                check=False,
            )
            if proc.returncode == 0:
                raw = (proc.stdout or "").strip()
                if not raw:
                    return "", []
                parsed = json.loads(raw)
                words = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
                table = reconstruct_table_from_words(words)
                text = "\n".join(" ".join(c for c in r if c) for r in table)
                return text, table
            last_err = (proc.stderr or proc.stdout or "").strip()

    tesseract = shutil.which("tesseract")
    if tesseract:
        proc = subprocess.run(
            [tesseract, str(path), "stdout"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            txt = proc.stdout or ""
            return txt, []
        last_err = (proc.stderr or "").strip()

    detail = f": {last_err}" if last_err else " (Windows OCR or Tesseract required)."
    raise RuntimeError(f"Could not run OCR on image{detail}")


def extract_text_from_image(path: Path) -> str:
    """Extract text from a PNG/JPG image using Windows OCR or Tesseract."""
    text, _table = extract_ocr_data_from_image(path)
    return text
