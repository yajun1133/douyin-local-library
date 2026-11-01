@echo off
rem ===================================================================
rem  Douyin Author Local Bridge  --  keep this file ASCII-only.
rem  Windows console reads .bat in the OEM codepage; Chinese here would
rem  show up as garbage and can even break parsing.
rem ===================================================================
title Douyin Author Local Bridge (127.0.0.1:8790)
cd /d "%~dp0"

rem Prefer the bundled python, fall back to PATH.
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

rem Keep the launcher's environment from leaking in (proxy / safe-delete shims).
set "PYTHONPATH="
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

rem Already running? Just say so and exit - never start a second copy.
where curl >nul 2>&1
if not errorlevel 1 (
    curl -s -o nul --noproxy 127.0.0.1 --max-time 2 http://127.0.0.1:8790/health
    if not errorlevel 1 (
        echo.
        echo   Service is ALREADY running on http://127.0.0.1:8790/
        echo   Nothing to do. Close this window.
        echo.
        ping -n 5 127.0.0.1 >nul
        exit /b 0
    )
)

echo.
echo   Reading local download records (read-only) ...
echo   Keep this window open while you browse Douyin.
echo.
"%PY%" -u bridge.py

echo.
echo   Service stopped. If there was an error above, screenshot it.
pause
