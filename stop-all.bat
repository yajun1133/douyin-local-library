@echo off
rem ===================================================================
rem  一键停止：本地管理后台(8723) + 浏览器插件查询服务(8790)
rem  注意：本文件按 GBK/ANSI 保存，用记事本/VS Code 编辑时请保持 ANSI，
rem        改成 UTF-8 会让下面的中文提示和窗口标题匹配失效。
rem ===================================================================
title 抖音工具 · 全部停止
chcp 936 >nul
setlocal enabledelayedexpansion

echo.
echo   正在停止两项服务 ...
echo.

set "S1=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8723 " ^| findstr "LISTENING"') do (
    taskkill /f /t /pid %%a >nul 2>&1
    if not errorlevel 1 set "S1=1"
)

set "S2=0"
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8790 " ^| findstr "LISTENING"') do (
    taskkill /f /t /pid %%a >nul 2>&1
    if not errorlevel 1 set "S2=1"
)

ping -n 3 127.0.0.1 >nul

if "!S1!"=="1" (echo   [OK] 8723 后台      : 已停止) else (echo   [--] 8723 后台      : 本来就没在跑)
if "!S2!"=="1" (echo   [OK] 8790 查询服务  : 已停止) else (echo   [--] 8790 查询服务  : 本来就没在跑)

rem 后台窗口（start-browser.bat 设的标题）在服务被杀后会停在 "Press any key"，顺手关掉
taskkill /f /fi "WINDOWTITLE eq Douyin Download Browser*" >nul 2>&1

echo.
where curl >nul 2>&1
if not errorlevel 1 (
    curl -s -o nul --noproxy 127.0.0.1 --max-time 2 http://127.0.0.1:8723/api/meta
    if errorlevel 1 (echo   [确认] 8723 已经关掉了) else (echo   [警告] 8723 还在响应，可能有两个实例，再运行一次本脚本)
    curl -s -o nul --noproxy 127.0.0.1 --max-time 2 http://127.0.0.1:8790/health
    if errorlevel 1 (echo   [确认] 8790 已经关掉了) else (echo   [警告] 8790 还在响应，可能有两个实例，再运行一次本脚本)
)

echo.
echo   都停了。要用的时候双击 start-all.bat。
echo.
ping -n 6 127.0.0.1 >nul
