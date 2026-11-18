# Data Kitsu Tools: working rules

## Versioning (apply on every change pushed to GitHub)

The version lives in two places, and both must match:
- `VERSION` (read by `updater.py`)
- `APP_VERSION` in `data_kitsu_tools.py` (shown in the window title)

Rules:
1. Each editing session on the same day as the current version: bump the last number.
   `1.0` -> `1.01` -> `1.02` -> ...
2. First edit on a new day: bump the minor number and drop the suffix.
   `1.02` -> `1.1`
3. Further edits on that same new day: append `.N`.
   `1.1` -> `1.1.1` -> `1.1.2` -> ...
4. The next new day starts again at rule 2: `1.1.2` -> `1.2`.

Sort order: `1.0 < 1.01 < 1.02 < 1.1 < 1.1.1 < 1.1.2 < 1.2`.
`updater.py` `parse_version` implements this order. Keep it in sync if the scheme changes.

Each pushed change also needs a changelog sub-bullet under the date in both
`README.md` and `README.vi.md`, using the version as its label.

## Tests

- `python3 tests/test_core.py` must pass before pushing.
- Launcher check: `./run.sh --check` (or `run.cmd --check` on Windows).
