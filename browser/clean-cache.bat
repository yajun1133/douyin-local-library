@echo off
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe

rem no args = trim to the size caps; --wipe = clear everything
rem cleans cache\thumb_cache and cache\trans_cache only; cover_cache is never touched
"%PY%" "tools\clean_cache.py" %*
pause
