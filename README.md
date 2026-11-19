# Data Kitsu Tools

**English** | [Tiếng Việt](README.vi.md)

> Original idea: take the shot CSV from Kitsu, pick the shots, and sync their data from the network to local, and the reverse way for publish.

A tool that copies shot data for a Kitsu pipeline. It reads the shot list from a CSV exported from Kitsu, finds the shot folders (including sub folders), checks the frame counts, and copies them to the destination while keeping the source folder structure. Setup is simple, and it runs on **Windows, macOS and Linux**.

![Data Kitsu Tools](assets/screenshot.png)

## Features

- **Read Kitsu CSV**: drag and drop, or click the *DROP CSV FILE* box, to load the shot list (the `Name` and `Frames` columns).
  Supports `;` or `,` separators, UTF-8, cp1258 (Vietnamese) and UTF-16.
- **Shots List**: shows NO, NAME, VERSION, FRAMES, STATUS. You can drag more shot folders or CSV files into the list while a copy is running.
  - Buttons under the list: **CLEAR** (empty the list), **ADD** (add a shot folder or type a shot name), **REMOVE** (remove the selected shots; Delete key works too).
  - Right-click a shot: **Open in Explorer**, **Copy path**, **Remove**.
- **Scan before copying**:
  - Finds the shot folders by name at any depth under COPY FROM.
  - Compares the real frame count with the Frames column. If they differ, the row turns red and the shot is skipped.
  - No files found: the row is yellow.
  - Hover over STATUS to see which frames are missing.
- **LOCAL**: copies shot folders and their variants (`PL01`, `PL02`, `PL03`...) and keeps the source folder structure. Variants appear as a tree you can collapse or expand, like in Explorer.
- **PUBLISH**: picks the highest version (`_v01` … `_v06` → `v06`) and copies it to `PASTE TO/<YYMMDD>/<folder name>`. Optional *MAKE DAILIES FOLDER* option.
- **File types filter**: EXR, DPX, MOV, TIF/TIFF, PNG, JPG (depends on the mode).
- **COPY MODE**: *SKIP* (keep existing files) or *OVERWRITE*. Optional skip of system files (`.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`).
- **START / PAUSE / STOP**: pause and resume are supported. On STOP, the file being copied is deleted and that shot is copied again with overwrite on the next START.
- **No half-written files**: data is written to `<name>.part` and renamed only when the copy is complete.
- **Progress**: an overall progress bar, and each shot's status changes colour as it goes.

### Status colours

| Colour | Meaning |
|---|---|
| Bold red | Frame count does not match the Frames column (shot is skipped) |
| Yellow | No files found after scan |
| Bold blue | Copying |
| Grey, italic, struck through | Copy finished |
| Orange | Stopped halfway (will overwrite on the next run) |

## Installation

Requirements: **Python 3.9 or newer** and **PySide6**.

The easiest way is to run the launcher. It checks Python, installs PySide6 if it is missing, and opens the app:

| OS | How to run |
|---|---|
| Windows | Double-click `run.cmd` (or `run.cmd --check` to only run the checks) |
| macOS | Double-click `run.command` (first time: right-click > Open, and `chmod +x run.command` if needed) |
| Linux | `./run.sh` (or `./run.sh --check`) |

Manual install if you prefer:

```bash
# Windows (PowerShell)
py -m pip install -r requirements.txt
py data_kitsu_tools.py

# macOS / Linux
python3 -m pip install -r requirements.txt
python3 data_kitsu_tools.py
```

## Quick start

1. Load the CSV with the *DROP CSV FILE* box.
2. Enter *COPY FROM* (source) and *PASTE TO* (destination). You can drag folders in from Explorer / Finder.
3. Click **LOCAL** or **PUBLISH** to scan. Check the colours and the STATUS column.
4. Click **START**, confirm, then follow the progress in the list and the log.

## Technical details

- Language: **Python 3** with a **Qt (PySide6)** GUI.
- Copies in 8 MB chunks, 4 files in parallel per shot, on a worker thread so the GUI does not freeze.
- Cache: no custom RAM cache, because the disk or network is the bottleneck. The OS already has a page cache; the app uses large buffers and parallel copies to make use of it.
- Paths use `pathlib`, so it works with `W:\...` on Windows and with `/Volumes/...` or `/mnt/...` on macOS and Linux.

### Compatibility

| OS | Status |
|---|---|
| Windows 10 / 11 | Supported (network drives and drive letters such as W: and E:) |
| macOS 12+ | Supported; ignores `._*` and `.DS_Store` files |
| Linux | Logic and GUI tested |

## Changelog

## My Modification update

- [26-11-19]
  - **Version 1.23:** each row of the recent-path list turns blue while the mouse is over it.
  - **Version 1.22:** the ▾ buttons next to the COPY FROM and PASTE TO "..." buttons are removed. The recent-path list opens by clicking the field. A path box is highlighted in blue while a folder is dragged over it.
  - **Version 1.21:** **CLEAR** also clears the log view. The confirmation asks for both the shot list and the log, or only the log when the list is empty. The app log file on disk is not touched.
  - **Version 1.20:** the shot names are matched with a naming database built from the Kitsu CSV files. The rules: the reel tag (R1, R2...) is optional, leading zeros do not matter (`010` = `10`), but `050` and `0050` are kept apart when both exist, letters (`01A`, `01_A`), decimals (`0043.5`) and hyphen variants (`010-add02`) are recognised, and when several shots fit a folder the longest name wins (`_PL01` beats the plain shot). A folder with no reel tag only matches when that shot number is unique in the CSV.
  - **Seq column:** the SHOTS LIST has a **SEQ** column between NO and NAME, filled from the CSV *Sequence* column. A Name that is only a number (`010`) takes the Sequence as prefix (`R1_SC03` -> `R1_SC03_010`).
  - **Frames:** the CSV *Frames* column can also be named *FRAME*. A CSV without a Frames column is accepted: the FRAMES cell shows `?` and any file count is accepted.
  - **Path history:** clicking COPY FROM or PASTE TO opens the recent-path list at once, like Explorer's address bar. Typing filters the list, Esc closes it. The ▾ button does the same.
  - **Version 1.19:** the automatic update check on launch is removed. The launchers now only check Python and PySide6.
  - **Update check:** the INFO window has a *Check for update* button. It compares with the version on GitHub and can update the app files. Restart the app after an update.
  - **Change log:** the INFO window has two tabs: *About me* (contacts, donation QR, wallet and update check) and *Change log* (this section of README.md).
- [26-11-18]
  - **Version 1.18:** the run records and app log go to the `run_logs/` folder inside the project, so they are easy to clean up (the folder is git-ignored). If that folder cannot be written, they go to the per-user data folder instead.
  - **Version 1.17:** the app keeps records for later analysis, in its data folder (*Open log folder* in the INFO window): `logs/app.log` (every log line, rotating), `history.csv` (one line per run: duration, speed, threads, errors, buffer size, paths), and `runs/<time>/` with `samples.csv` (every second: MB/s, active time, files, threads), `files.csv` (every file: size, seconds, MB/s, result, error) and `summary.json`. The last 100 run folders are kept.
  - **Version 1.16:** before a copy starts, the app checks the destination (writable, enough free space, shows the space needed) and warns about empty source files, paths longer than 250 characters, and unfinished `.part` files. Blocks the copy when there is not enough space. An existing file is now skipped only when its size matches; a short file is copied again. Files are flushed to disk before they are renamed. New **THREADS / SHOT** setting (1-16, default 4) for network drives.
  - **Version 1.15:** yellow and red shots write their log lines (reason and path) in yellow or red, so they are easy to follow.
  - **Version 1.14:** the About button is now **INFO**, placed right of CLEAR. The About window shows the role (VFX Compositor), the BSC wallet address with a **Copy** button (it changes to *Copied*). COPY FROM and PASTE TO have a ▾ button listing the last 10 paths, shared by both fields and saved with the session.
  - **Version 1.13:** the window title shows the author (`Data Kitsu Tools 1.13 by Thomas Thinh Vu`). New **ABOUT ME** button (bottom right) opens a window with the contact links (ArtStation, LinkedIn, Facebook, Gmail) and the donation QR code, large enough to scan.
  - **Version 1.12:** two live readouts beside ADD / REMOVE / CLEAR show the **active time** (% of time disk I/O is running) and the **transfer rate** (MB/s), with a 60-second graph. A shot whose files already exist on the destination is yellow, shows the version found there with `-copied` (e.g. `v02-copied`) and is skipped unless OVERWRITE or Force copy is on. Yellow and red shots now log the reason and the path, and their tooltip gives the same reason.
  - **Version 1.11:** a shot marked **Force copy** shows in the normal colour instead of red.
  - **Version 1.10:** right-click a shot and choose **Force copy** to copy a shot whose frame count differs from the expected count. Its existing files are overwritten. Untick it to go back to skipping the shot.
  - **Version 1.09:** settings, the shot list (with done/partial state) and the log are saved on exit and every 20 seconds, and restored on the next start. A shot that was mid-copy when the app closed comes back as partial and is re-copied with overwrite.
  - **Version 1.08:** scan matches folders with or without the reel tag (`HHCC_R1_SC04_020_Tom_comp_v03` now matches shot `HHCC_SC04_020`), so PUBLISH finds these shots instead of showing them as missing. Added a test for this.
  - **Version 1.07:** the copy log uses one line for the whole run; only its numbers change (e.g. `Copying HHCC_TLR_030 (17/42 file(s))`).
  - **Version 1.06:** the copy log shows one live line per shot, e.g. `Copying HHCC_TLR_030 (0/42 file(s))` counting up to `42/42`, and ends with `- done`.
  - **Version 1.05:** removed the left DROP CSV box; the empty SHOTS LIST now shows the drop / click hint instead. COPY FROM and PASTE TO fields are twice as tall to make drag and drop easier.
  - **Version 1.04:** shot list buttons ordered ADD / REMOVE / CLEAR, removing one shot no longer asks for confirmation, and Open in Explorer / Copy path now work for any shot (they find the folder when needed).
  - **Version 1.03:** COPY FROM / PASTE TO previews show the full path of the first shot (found in the background before a scan).
  - **Version 1.02:** Shot list buttons CLEAR / ADD / REMOVE, right-click menu (Open in Explorer, Copy path, Remove), and aligned mode and action buttons.
  - **Version 1.01:** versioning rule (same-day edits = 1.0N, next day = 1.1, then 1.1.N). Updater now compares versions in this order.
  - **Version 1.0:** first named version. Shown in the window title and in the `VERSION` file.
  - **Updater:** `updater.py` checks GitHub on each launch and shows an update notice. Answer `y` to update to the latest version from GitHub (git pull, or a zip download if the folder is not a git clone). Use `--no-update` to skip the check.
  - **English README:** added `README.en.md` and language links in both READMEs.
  - **Launchers:** added `run.cmd` (Windows), `run.sh` and `run.command` (Linux / macOS) to check Python, install PySide6 and open the GUI.
  - **Initial release:** GUI based on the mockup, reads Kitsu CSV, LOCAL / PUBLISH scan, copy with skip / overwrite, pause / resume / stop, skips system files, status colours, README.

## Updating

Open **INFO** and press **Check for update**. If a newer version is on GitHub, press **Update now**, then restart the app. The launchers no longer check for updates when they start.
