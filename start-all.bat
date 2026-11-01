@echo off
rem ===================================================================
rem  一键启动：本地管理后台(8723) + 浏览器插件查询服务(8790)
rem  注意：本文件按 GBK/ANSI 保存，用记事本或 VS Code 编辑时请保持 ANSI，
rem        改成 UTF-8 会让下面的中文路径全部失效。
rem ===================================================================
title 抖音工具 · 全部启动
chcp 936 >nul
set "ROOT=%~dp0"

set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "PYW=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if not exist "%PY%" set "PY=python"
if not exist "%PYW%" set "PYW=pythonw"

rem 让本地请求绕开系统代理，否则 127.0.0.1 也会被代理走
set "PYTHONPATH="
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

if not exist "%ROOT%browser\start-browser.bat" (
    echo   [错误] 找不到：%ROOT%browser\start-browser.bat
    pause
    exit /b 1
)
if not exist "%ROOT%douyin-author-marker\local-service\bridge.py" (
    echo   [错误] 找不到：%ROOT%douyin-author-marker\local-service\bridge.py
    pause
    exit /b 1
)

echo.
echo   [1/2] 本地管理后台    http://127.0.0.1:8723
start "抖音本地库后台 8723" "%ROOT%browser\start-browser.bat"

echo   [2/2] 插件查询服务    http://127.0.0.1:8790
where curl >nul 2>&1
if errorlevel 1 (
    start "" "%PYW%" -u "%ROOT%douyin-author-marker\local-service\bridge.py"
) else (
    curl -s -o nul --noproxy 127.0.0.1 --max-time 2 http://127.0.0.1:8790/health
    if errorlevel 1 (
        start "" "%PYW%" -u "%ROOT%douyin-author-marker\local-service\bridge.py"
    ) else (
        echo         ^(已经在运行，跳过^)
    )
)

ping -n 5 127.0.0.1 >nul

echo.
where curl >nul 2>&1
if not errorlevel 1 (
    curl -s -o nul --noproxy 127.0.0.1 --max-time 3 http://127.0.0.1:8723/api/meta
    if errorlevel 1 (echo   [--] 8723 后台      ：还没起来，看看它自己的窗口) else (echo   [OK] 8723 后台      ：运行中)
    curl -s -o nul --noproxy 127.0.0.1 --max-time 3 http://127.0.0.1:8790/health
    if errorlevel 1 (echo   [--] 8790 查询服务  ：还没起来，看 local-service\service.log) else (echo   [OK] 8790 查询服务  ：运行中)
)

echo.
echo   都起来了。查询服务是隐藏进程（没有窗口），后台有它自己的窗口。
echo   以后不想用了：关掉 8723 的窗口 + 双击 local-service\stop-service.bat。
echo.
ping -n 6 127.0.0.1 >nul
