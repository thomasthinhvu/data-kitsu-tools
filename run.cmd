@echo off
REM Data Kitsu Tools launcher for Windows.
REM Launches immediately; checks/repairs Python & PySide6 only on error or --check.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo ERROR: Python 3 was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

if /i "%~1"=="--check" (
    %PY% launcher_check.py --check || (pause & exit /b 1)
    echo [3/3] All checks passed ^(--check mode, GUI not started^).
    exit /b 0
)

%PY% data_kitsu_tools.py
if errorlevel 1 (
    echo App exited with an error. Running library check/repair...
    %PY% launcher_check.py --repair || (pause & exit /b 1)
    %PY% data_kitsu_tools.py
)
endlocal
