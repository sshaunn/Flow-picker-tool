@echo off
REM Switch to UTF-8 code page so the Chinese filenames in the copy
REM commands below are interpreted correctly by cmd.exe on Win10/11.
chcp 65001 >nul
REM =====================================================================
REM Flow Harvester — Windows bundle build
REM
REM Run on a Windows 10/11 dev machine to produce dist\FlowHarvester\
REM (a folder containing FlowHarvester.exe plus all DLLs / data files).
REM
REM Customer install becomes: zip that folder, ship the zip; customer
REM unzips and double-clicks FlowHarvester.exe. No Python required.
REM
REM Usage:
REM   build.bat          (clean build)
REM   build.bat zip      (clean build + produce FlowHarvester-<ver>.zip)
REM =====================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"
echo.
echo === Flow Harvester bundle build ===
echo Working dir: %CD%
echo.

REM --- 1. Ensure venv with build deps ---
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run setup.bat first to create it.
    pause
    exit /b 1
)
call ".venv\Scripts\activate.bat"

echo [1/5] Installing build dependencies ...
python -m pip install --upgrade pip --quiet
python -m pip install -e ".[build]" --quiet
if errorlevel 1 (
    echo [ERROR] pip install [build] failed.
    pause
    exit /b 1
)

REM --- 2. Clean previous artifacts ---
echo [2/5] Cleaning dist\ and build\ ...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"

REM --- 3. PyInstaller ---
echo [3/5] Running PyInstaller ^(this takes 1-3 minutes^) ...
pyinstaller --noconfirm flow_harvester.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

REM --- 4. Drop a fresh start.bat-equivalent next to the exe so customers
REM        get a friendly entry too (double-clicking FlowHarvester.exe
REM        already works; this is a backup if Defender flags the exe).
echo [4/5] Adding bundle launcher ...
> "dist\FlowHarvester\\Run Flow Harvester.cmd" (
    echo @echo off
    echo cd /d "%%~dp0"
    echo start "" "FlowHarvester.exe"
)

REM --- 5. Copy customer-facing .txt manuals next to the exe so they
REM        sit at the top level of the unzipped folder (CRLF + UTF-8 BOM
REM        already applied in repo so Win Notepad opens them cleanly).
echo [5/5] Adding customer manuals ...
copy /Y "docs\安装说明.txt" "dist\FlowHarvester\安装说明.txt" >nul
copy /Y "docs\使用手册.txt" "dist\FlowHarvester\使用手册.txt" >nul
if errorlevel 1 (
    echo [WARN] Failed to copy customer .txt manuals into dist\FlowHarvester\.
)

REM --- 6. (Optional) Drop a license.key into the bundle if one exists
REM        in the repo root. Generate it with:
REM          .venv\Scripts\flow-harvester gen-license --customer XXX --days 30
REM        before running build.bat. Without a license.key the bundled
REM        exe refuses to start (intentional — it's the time-limit lock).
if exist "license.key" (
    echo [6/6] Including license.key
    copy /Y "license.key" "dist\FlowHarvester\license.key" >nul
) else (
    echo [WARN] license.key not found in repo root.
    echo        Generate one before zipping or the customer's bundle won't start:
    echo          .venv\Scripts\flow-harvester gen-license --customer NAME --days 30
)

echo.
if /i "%~1"=="zip" (
    echo Packing dist\FlowHarvester into FlowHarvester-bundle.zip ...
    powershell -NoProfile -Command "Compress-Archive -Path 'dist\FlowHarvester\*' -DestinationPath 'FlowHarvester-bundle.zip' -Force"
    if errorlevel 1 (
        echo [WARN] zip step failed. dist\FlowHarvester is still ready.
    ) else (
        echo Bundle: %CD%\FlowHarvester-bundle.zip
    )
) else (
    echo Bundle ready: %CD%\dist\FlowHarvester\
    echo To produce a single zip for distribution, run:  build.bat zip
)

echo.
echo === Build complete ===
pause
