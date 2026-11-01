@echo off
rem Stop the local bridge (kill whoever is LISTENING on 8790). ASCII only.
setlocal enabledelayedexpansion
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8790" ^| findstr "LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
    if not errorlevel 1 set FOUND=1
)
if "!FOUND!"=="1" (
    echo   Service stopped.
) else (
    echo   Service was not running.
)
ping -n 3 127.0.0.1 >nul
