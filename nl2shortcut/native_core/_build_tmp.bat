@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VCV="
for %%V in (
  "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
  "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
) do (
  if not defined VCV if exist %%V set "VCV=%%~V"
)
if not defined VCV (echo NO_VCVARS & exit /b 1)
echo USING: !VCV!

call "!VCV!"
if errorlevel 1 (echo VCVARS_FAILED & exit /b 1)

where cl.exe
if errorlevel 1 (echo NO_CL & exit /b 1)

if not exist "output" mkdir "output"
cl.exe /nologo /std:c++17 /O2 /MT /LD /EHsc /Fe:"output\nl2shortcut_native.dll" /Fo:"output\\" src\uia.cpp src\inject.cpp src\window.cpp src\fuzzy_match.cpp src\scache.cpp src\context_proc.cpp src\pattern_cluster.cpp /link ole32.lib oleaut32.lib user32.lib kernel32.lib
if errorlevel 1 (echo COMPILE_FAILED & exit /b 1)

copy /Y "output\nl2shortcut_native.dll" "..\nl2shortcut_native.dll" >nul
echo BUILD_OK
