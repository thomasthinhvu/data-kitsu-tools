@echo off
REM Data Kitsu Tools launcher for Windows.
REM Checks Python and PySide6, installs missing requirements, then starts the GUI.
REM Pass --check to only run the checks (no GUI), --no-update to skip the update check.
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python...
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
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" || (
    echo ERROR: Python 3.9 or newer is required.
    %PY% --version
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version') do echo       OK: %%v

echo [2/4] Checking for updates...
if /i "%~1"=="--no-update" goto skip_update
%PY% updater.py || echo       update check failed, continuing with the local version
goto after_update
:skip_update
echo       skipped (--no-update)
:after_update

echo [3/4] Checking PySide6...
%PY% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo       PySide6 not found, installing from requirements.txt ...
    %PY% -m pip install --user -r requirements.txt
    %PY% -c "import PySide6" >nul 2>nul
    if errorlevel 1 (
        echo ERROR: PySide6 installation failed.
        pause
        exit /b 1
    )
)
echo       OK: PySide6 is installed

if /i "%~1"=="--check" (
    echo [4/4] All checks passed ^(--check mode, GUI not started^).
    exit /b 0
)

echo [4/4] Starting Data Kitsu Tools...
%PY% data_kitsu_tools.py
endlocal
