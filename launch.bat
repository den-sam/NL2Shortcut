@echo off
chcp 65001 >nul
title nl2shortcut GUI
cd /d "%~dp0"
python -m nl2shortcut.gui
if %errorlevel% neq 0 (
    echo.
    echo Failed to start. Install with: pip install PyQt5
    pause
)
