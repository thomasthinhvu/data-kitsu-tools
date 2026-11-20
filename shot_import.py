#!/usr/bin/env python3
"""
Shot-list import helpers (Excel .xlsx/.xlsm, Image OCR .png/.jpg, smart naming)
and first-frame JPEG capture for Data Kitsu Tools 2.0.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

SHOT_HEADER_ALIASES = {
    "name", "shot", "shot code", "shot_code", "shotname", "shot name",
    "shot_name", "vfx_name", "vfx name", "code",
}
SEQ_HEADER_ALIASES = {"sequence", "seq", "scene", "phase", "reel", "episode", "ep"}
FRAME_HEADER_ALIASES = {"frames", "frame", "frame count", "frame_count", "duration", "dur", "length"}
START_FRAME_ALIASES = {"start frame", "start_frame", "frame_in", "frame in", "in", "start"}
END_FRAME_ALIASES = {"end frame", "end_frame", "frame_out", "frame out", "out", "end"}

RANGE_RE = re.compile(r"^\s*(\d+)\s*[-–—]\s*(\d+)\s*$")
SCENE_HEADER_RE = re.compile(
    r"^\s*(?:scene|seq|sequence|phase|reel)\s*[_\-\s]*(\d+[a-zA-Z]?)\b",
    re.IGNORECASE,
)
GENERIC_SHOT_RE = re.compile(r"^(?:shot[_\-\s]*\d+|\d+(?:[._\-][a-zA-Z0-9]+)?)$", re.IGNORECASE)
OCR_SHOT_TOKEN_RE = re.compile(
    r"\b(?:"
    r"\d{2,4}[_\-\s][A-Z][A-Z0-9]{1,15}[_\-\s]\d{2,5}(?:[._\-][a-zA-Z0-9]+)*"
    r"|[A-Z]{2,8}[_\-][A-Z0-9]{1,8}[_\-][A-Z0-9]{1,8}(?:[_\-]\d{2,5})?(?:[._\-][a-zA-Z0-9]+)*"
    r"|[A-Z]{1,5}\d{2,5}(?:[._\-]\d{2,4})?"
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
    """Convert the first frame of *files* to a JPEG at *out_path*.

    Uses Qt ``QImage`` for standard image formats and falls back to ``ffmpeg``
    for EXR / DPX / MOV sequences. Returns *out_path* on success, else ``None``.
    """
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


def parse_shots_from_table(rows: list[list[str]]) -> list[dict]:
    """Extract shot dicts (name, frames, seq) from 2D table rows (Excel/CSV)."""
    if not rows:
        return []

    header_idx = None
    col_map: dict[str, int] = {}
    for idx, row in enumerate(rows[:20]):
        lowered = [str(c or "").strip().lower() for c in row]
        name_col = next((i for i, h in enumerate(lowered) if h in SHOT_HEADER_ALIASES), None)
        if name_col is not None:
            header_idx = idx
            col_map["name"] = name_col
            col_map["seq"] = next((i for i, h in enumerate(lowered) if h in SEQ_HEADER_ALIASES), -1)
            col_map["frames"] = next(
                (i for i, h in enumerate(lowered) if h in FRAME_HEADER_ALIASES or h.startswith("duration")),
                -1,
            )
            col_map["start"] = next((i for i, h in enumerate(lowered) if h in START_FRAME_ALIASES), -1)
            col_map["end"] = next((i for i, h in enumerate(lowered) if h in END_FRAME_ALIASES), -1)
            break

    if header_idx is None:
        # Fallback: treat non-empty rows as free text
        return parse_shots_from_text("\n".join(" ".join(str(c) for c in r if c) for r in rows))

    def cell(row: list[str], key: str) -> str:
        cidx = col_map.get(key, -1)
        return str(row[cidx]).strip() if 0 <= cidx < len(row) else ""

    records: list[dict] = []
    seen: set[str] = set()
    current_seq = ""

    for row in rows[header_idx + 1 :]:
        raw_name = cell(row, "name")
        if not raw_name:
            continue
        seq_val = cell(row, "seq")
        if seq_val and not seq_val.isdigit():
            current_seq = seq_val
        elif seq_val and seq_val.isdigit():
            seq_val = f"R{seq_val}"

        # Detect section headers like "Scene 001 - ESTB belfast" or "phase 6"
        frames = parse_frame_value(cell(row, "frames"))
        if frames is None and col_map["start"] >= 0 and col_map["end"] >= 0:
            s_val = parse_frame_value(cell(row, "start"))
            e_val = parse_frame_value(cell(row, "end"))
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


def extract_text_from_image(path: Path) -> str:
    """Extract text from a PNG/JPG image using Windows OCR or Tesseract."""
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
            f"$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{str(path.resolve())}')) ([Windows.Storage.StorageFile])\n"
            "$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])\n"
            "$dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])\n"
            "$bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])\n"
            "$ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()\n"
            "$res = Await ($ocr.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])\n"
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "$res.Lines | ForEach-Object { $_.Text }\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tf:
            tf.write(ps_script)
            ps_path = tf.name
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout
        finally:
            try:
                os.remove(ps_path)
            except OSError:
                pass

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
            return proc.stdout
    raise RuntimeError("Could not run OCR on image (Windows OCR or Tesseract required).")
