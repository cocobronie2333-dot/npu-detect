@echo off
setlocal
chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=C:\Users\sessy\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "PARAMIKO_DIR=D:\workspace\ssh\temp\python_packages"
set "PAGE_URL=http://127.0.0.1:8765/"

if not exist "%PYTHON_EXE%" (
    echo Python runtime not found:
    echo   %PYTHON_EXE%
    echo Edit PYTHON_EXE in this script to point to your Python executable.
    pause
    exit /b 1
)

if not exist "%PARAMIKO_DIR%\paramiko" (
    echo Paramiko package not found:
    echo   %PARAMIKO_DIR%\paramiko
    echo Edit PARAMIKO_DIR in this script if Paramiko is installed elsewhere.
    pause
    exit /b 1
)

set "PYTHONPATH=%PARAMIKO_DIR%;%PYTHONPATH%"

rem Reuse an existing local service if the page is already responding.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%PAGE_URL%'; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo NPU monitor is already running. Opening %PAGE_URL%
    start "" "%PAGE_URL%"
    exit /b 0
)

echo Starting local NPU monitor service...
start "NPU Monitor Backend" "%PYTHON_EXE%" "%SCRIPT_DIR%npu_card_monitor.py"

rem Wait up to 15 seconds for the service to start.
for /l %%i in (1,1,15) do (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 '%PAGE_URL%'; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
    if not errorlevel 1 goto service_ready
    timeout /t 1 /nobreak >nul
)

echo The backend did not respond at %PAGE_URL%.
echo Keep the backend window open and check the error shown there.
pause
exit /b 1

:service_ready
echo NPU monitor is ready. Opening %PAGE_URL%
start "" "%PAGE_URL%"
exit /b 0
