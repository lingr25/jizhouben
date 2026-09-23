@echo off
setlocal
cd /d "%~dp0"

:: 检查管理员权限
fltmc >nul 2>&1
if errorlevel 1 goto request_admin
goto start_app

:request_admin
echo ===================================================
echo   [!] PTFE Toolkits 需要管理员权限向游戏注入按键
echo   正在请求管理员提权，请在弹窗中点击 [是]...
echo ===================================================
set "TOOL_BAT=%~f0"
powershell -NoProfile -Command "Start-Process cmd -ArgumentList ('/c ""' + $env:TOOL_BAT + '""') -Verb RunAs"
if errorlevel 1 (
    echo [!] 提权请求被取消或失败。
    pause
)
exit /b

:start_app
title PTFE Toolkits

:: 寻找 Python
set "PY_EXE=%~dp0python312\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=%~dp0vendor\py312\tools\python.exe"
if not exist "%PY_EXE%" (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_EXE=python"
)

if not exist "%PY_EXE%" if not "%PY_EXE%"=="python" (
    echo [错误] 未找到 Python 解释器。
    pause
    exit /b 1
)

"%PY_EXE%" reminder_server.py --port 2607 --open-browser
if errorlevel 1 (
    echo.
    echo [!] 提示：服务异常退出（若提示端口被占用，请关闭旧终端窗口或结束后台 python.exe 进程后再试）。
    echo.
    pause
)
