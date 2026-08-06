@echo off
chcp 65001 >nul
echo ============================================================
echo   NL2Shortcut Native Core (C++) — Build Script
echo ============================================================
echo.

set "SRC_DIR=%~dp0"
set "OUT_DIR=%SRC_DIR%output"
set "PYTHON_DIR=%SRC_DIR%.."

REM --- Check MSVC ---
echo [1/3] Checking MSVC compiler...
where cl.exe >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   MSVC not found on PATH. Trying to locate...
    for /f "usebackq tokens=*" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do (
        set "VS_PATH=%%i"
    )
    if not defined VS_PATH (
        for /f "usebackq tokens=*" %%i in (`"%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do (
            set "VS_PATH=%%i"
        )
    )
    if defined VS_PATH (
        echo   Found VS at: !VS_PATH!
        call "!VS_PATH!\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
    ) else (
        echo.
        echo [ERROR] Visual Studio with C++ tools not found.
        echo   Install: https://visualstudio.microsoft.com/downloads/
        echo   Select: "Desktop development with C++" workload.
        echo   OR: Install Visual Studio Build Tools (lighter).
        pause
        exit /b 1
    )
)
echo [OK] cl.exe found

REM --- Check CMake ---
echo.
echo [2/3] Checking CMake...
where cmake.exe >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] CMake not found on PATH. Trying direct cl.exe compile...
    goto COMPILE_DIRECT
)
echo [OK] cmake found. Using CMake build.

REM --- CMake Build ---
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
cd /d "%OUT_DIR%"
cmake "%SRC_DIR%" -DCMAKE_BUILD_TYPE=Release
if %errorlevel% neq 0 (
    echo [ERROR] CMake configure failed.
    pause
    exit /b 1
)
cmake --build . --config Release
if %errorlevel% neq 0 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

REM Copy DLL to Python package
set "DLL_SRC=%OUT_DIR%\Release\nl2shortcut_native.dll"
goto COPY_DLL

:COMPILE_DIRECT
echo [INFO] Using direct MSVC compile...
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"
set "DLL_SRC=%OUT_DIR%\nl2shortcut_native.dll"
cl.exe /nologo /std:c++17 /O2 /MT /LD /EHsc /Fe:"%DLL_SRC%" ^
    "%SRC_DIR%src\uia.cpp" ^
    "%SRC_DIR%src\uia_diff.cpp" ^
    "%SRC_DIR%src\inject.cpp" ^
    "%SRC_DIR%src\window.cpp" ^
    "%SRC_DIR%src\fuzzy_match.cpp" ^
    "%SRC_DIR%src\scache.cpp" ^
    "%SRC_DIR%src\context_proc.cpp" ^
    "%SRC_DIR%src\pattern_cluster.cpp" ^
    "%SRC_DIR%src\executor.cpp" ^
    /link ole32.lib oleaut32.lib user32.lib kernel32.lib
if %errorlevel% neq 0 (
    echo [ERROR] Direct compile failed.
    echo Try running from "Developer Command Prompt for VS":
    echo   cd nl2shortcut\native_core ^&^& build.bat
    pause
    exit /b 1
)

:COPY_DLL
if not exist "%DLL_SRC%" (
    echo [ERROR] DLL not found at: %DLL_SRC%
    pause
    exit /b 1
)

copy /Y "%DLL_SRC%" "%PYTHON_DIR%\nl2shortcut_native.dll" >nul
echo [OK] DLL copied to: %PYTHON_DIR%\nl2shortcut_native.dll

echo.
echo [3/3] Verifying...
python -c "import ctypes; dll = ctypes.WinDLL(r'%PYTHON_DIR%\nl2shortcut_native.dll'); print('  DLL loaded successfully')"
if %errorlevel% neq 0 (
    echo [WARN] Python import test failed, but DLL may be OK.
) else (
    echo [OK] DLL import test passed.
)

echo.
echo ============================================================
echo   Build Complete!   nl2shortcut_native.dll
echo ============================================================
pause
