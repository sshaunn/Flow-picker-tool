@echo off
REM =====================================================================
REM Flow Harvester — daily launcher
REM
REM Activates the local venv, starts the Web server on localhost:8080,
REM and opens the dashboard in the default browser. Keep this command
REM window open for as long as you want the tool running — closing it
REM stops the scheduler.
REM =====================================================================
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] virtual env not found. Run setup.bat first.
    pause
    exit /b 1
)

echo === Flow Harvester ===
echo The dashboard will open in your browser shortly.
echo Keep this window open while you use the tool.
echo Press Ctrl+C here to stop the scheduler.
echo.

REM Kick the browser open after a short delay so the server has time to
REM bind to the port. ``start ""`` runs detached so this script can move
REM on to the actual server boot.
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8080/"

call ".venv\Scripts\activate.bat"
flow-harvester serve --port 8080

REM ``flow-harvester serve`` blocks until Ctrl+C; control returns here
REM only on shutdown.
echo.
echo Flow Harvester stopped.
pause
