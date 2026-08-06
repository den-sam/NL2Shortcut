@echo off
:: NL2Shortcut Agent API — Production Launcher
:: Place in shell:startup for auto-launch on Windows boot

cd /d "%~dp0"

:: Ensure Python is available
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Python not found on PATH — aborting
    exit /b 1
)

echo [%date% %time%] Starting NL2Shortcut Agent API on :7770

:: Start the server. Backgrounded via pythonw to avoid console window.
start "" /B pythonw -m nl2shortcut start-server --host 127.0.0.1 --port 7770

:: Wait for it to be ready
timeout /t 3 /nobreak >nul
curl -s http://127.0.0.1:7770/v1/health >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo NL2Shortcut ready: http://127.0.0.1:7770
) else (
    echo Health check failed — check logs at %%USERPROFILE%%\.nl2shortcut\logs\
)
