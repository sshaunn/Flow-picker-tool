@echo off
REM =====================================================================
REM Flow Harvester — Windows one-time setup
REM
REM Run this once after copying the project folder to the customer machine.
REM Creates a local Python virtual env, installs dependencies, downloads
REM the patchright Chromium build that wraps the system Chrome.
REM
REM Re-runs are safe (idempotent): venv is reused, pip skips already-
REM installed packages.
REM =====================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"
echo.
echo === Flow Harvester setup ===
echo Working dir: %CD%
echo.

REM --- 1. Locate Python 3.10+ ---
set "PY_CMD="
where py >nul 2>nul
if %errorlevel%==0 (
    py -3.10 -V >nul 2>nul && set "PY_CMD=py -3.10"
    if not defined PY_CMD ( py -3.11 -V >nul 2>nul && set "PY_CMD=py -3.11" )
    if not defined PY_CMD ( py -3.12 -V >nul 2>nul && set "PY_CMD=py -3.12" )
    if not defined PY_CMD ( py -3.13 -V >nul 2>nul && set "PY_CMD=py -3.13" )
    if not defined PY_CMD ( py -3   -V >nul 2>nul && set "PY_CMD=py -3"   )
)
if not defined PY_CMD (
    where python >nul 2>nul
    if !errorlevel!==0 set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [ERROR] Python 3.10+ not found.
    echo Install from https://www.python.org/downloads/ ^(check "Add Python to PATH"^).
    pause
    exit /b 1
)
echo [1/4] Python: %PY_CMD%
%PY_CMD% --version

REM --- 2. Create venv (skip if exists) ---
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] Creating virtual env at .venv ...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
) else (
    echo [2/4] Virtual env already exists, reusing.
)

REM --- 3. Install project + deps ---
echo [3/4] Installing dependencies ^(may take 1-2 minutes^) ...
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip --quiet
python -m pip install -e . --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

REM --- 4. Patchright will use the system Chrome via channel="chrome";
REM        no separate Chromium download needed. Verify Chrome exists.
echo [4/4] Verifying Google Chrome ...
set "CHROME_PATH="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
)
if not defined CHROME_PATH if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
)
if not defined CHROME_PATH if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=%LocalAppData%\Google\Chrome\Application\chrome.exe"
)
if defined CHROME_PATH (
    echo Chrome: !CHROME_PATH!
) else (
    echo [WARNING] Chrome not found in standard locations.
    echo Install Google Chrome from https://www.google.com/chrome/ before running tasks.
)

echo.
echo === Setup complete ===
echo Next: double-click start.bat to launch the Web UI.
echo.
pause
