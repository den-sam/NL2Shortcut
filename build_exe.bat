@echo off
chcp 65001 >nul
title NL2Shortcut 打包工具

echo ════════════════════════════════════════════
echo   NL2Shortcut 一键打包为 .exe
echo ════════════════════════════════════════════
echo.

cd /d "%~dp0"

REM ── 1. 检查 Python 环境 ──────────────────────────────
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python。请先安装 Python 3.8+ 并加入 PATH。
    echo 下载：https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ── 2. 检查 PyInstaller ─────────────────────────────
python -c "import PyInstaller" 2>nul
if %errorlevel% neq 0 (
    echo [提示] 未安装 PyInstaller，正在自动安装...
    pip install pyinstaller
    if %errorlevel% neq 0 (
        echo [错误] PyInstaller 安装失败。
        pause
        exit /b 1
    )
)

REM ── 3. 检查项目依赖 ─────────────────────────────────
python -c "import PyQt5, pyautogui, yaml" 2>nul
if %errorlevel% neq 0 (
    echo [提示] 缺少依赖，正在安装完整依赖...
    pip install -e ".[all]"
    if %errorlevel% neq 0 (
        echo [警告] 依赖安装失败，可能影响打包。
    )
)

REM ── 4. 检查 C++ 加速模块 ────────────────────────────
if not exist "nl2shortcut\native_core\output\nl2shortcut_native.dll" (
    if exist "nl2shortcut\nl2shortcut_native.dll" (
        echo [提示] 使用根目录的 native dll。
    ) else (
        echo [警告] 未找到 nl2shortcut_native.dll，C++ 加速模块将不可用。
        echo        程序仍可运行，但会自动降级到纯 Python 模式。
    )
) else (
    echo [OK] 已找到 C++ 加速模块。
)

echo.
echo ── 开始打包 ────────────────────────────────────
echo.

REM ── 5. 清理旧产物 ──────────────────────────────────
if exist "build\NL2Shortcut" rmdir /s /q "build\NL2Shortcut"
if exist "dist\NL2Shortcut" rmdir /s /q "dist\NL2Shortcut"

REM ── 6. 执行 PyInstaller ─────────────────────────────
python -m PyInstaller nl2shortcut_exe.spec --noconfirm

if %errorlevel% neq 0 (
    echo.
    echo [错误] 打包失败。请查看上方错误信息。
    pause
    exit /b 1
)

echo.
echo ════════════════════════════════════════════
echo   打包成功！
echo ════════════════════════════════════════════
echo.
echo  产物位置：dist\NL2Shortcut\NL2Shortcut.exe
echo.
echo  使用方法：
echo    1. 双击 dist\NL2Shortcut\NL2Shortcut.exe
echo    2. 或将整个 dist\NL2Shortcut 文件夹复制给用户
echo.
echo  注意：
echo    - 整个 NL2Shortcut 文件夹必须一起分发（含依赖 DLL）
echo    - 首次启动可能稍慢（解压依赖）
echo    - 用户无需安装 Python 即可使用
echo.

REM ── 7. 询问是否打开产物目录 ────────────────────────
set /p open_dir="是否打开产物目录？(y/n): "
if /i "%open_dir%"=="y" (
    explorer "dist\NL2Shortcut"
)

pause
