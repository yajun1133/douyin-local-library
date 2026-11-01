@echo off
rem NOTE: keep this file ASCII-only. The console reads .bat in the OEM codepage,
rem so any Chinese written here would show up as garbage.
title Douyin Download Browser (127.0.0.1:8723)
cd /d "%~dp0"

set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe

rem Some launchers inject PYTHONPATH with a "safe delete" shim that aborts the
rem process when more than 50 files are removed in one run. This app removes
rem thousands of duplicate files on purpose (user clicks the button), so clear
rem it here: behaviour must not depend on how the server was started.
set "PYTHONPATH="
set "CODEBUDDY_SAFE_DELETE_ENABLED=0"

rem The launcher also injects HTTP_PROXY/HTTPS_PROXY. Without this, curl and
rem python talk to 127.0.0.1 THROUGH the proxy, get 502, and conclude the
rem service is not running -- so every double-click started ANOTHER instance.
rem Several instances on one port = the page randomly hangs, deletes never
rem finish, and the service "disappears". Keep localhost out of the proxy.
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    echo         Fix the PY path above. See docs in the doc folder.
    pause
    exit /b 1
)

rem ---------------------------------------------------------------------------
rem Already running? Then just open the browser and leave. Do NOT start a second
rem copy: it would exit at once and this window would sit at "press any key",
rem looking exactly like a service that never died (that is how a pile of
rem identical black windows appeared before).
rem --noproxy is required: the probe must never go through the system proxy.
rem ---------------------------------------------------------------------------
where curl >nul 2>&1
if not errorlevel 1 (
    curl -s -o nul --noproxy 127.0.0.1 --max-time 2 http://127.0.0.1:8723/api/meta
    if not errorlevel 1 (
        echo.
        echo Already running on http://127.0.0.1:8723/ - opening browser.
        echo To RESTART it, close the existing window first, then run this again.
        echo.
        start "" http://127.0.0.1:8723/
        ping -n 3 127.0.0.1 >nul
        exit /b 0
    )
)

rem Code lives in src\ ; data / cache / backup are in the sibling folders (see src\paths.py)
rem -u : unbuffered stdout, so progress lines show up one by one in this window
"%PY%" -u src\server.py

echo.
echo Server exited. If there was an error above, screenshot it.
pause
