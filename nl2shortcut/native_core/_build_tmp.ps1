$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$candidates = @(
  'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat',
  'C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat'
)
$vcv = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $vcv) { Write-Output 'NO_VCVARS'; exit 1 }
Write-Output "USING: $vcv"

# 导入 vcvars 设置的环境变量到当前 PowerShell 会话
$envDump = & cmd.exe /c "call `"$vcv`" >nul 2>&1 && set"
if (-not $envDump) { Write-Output 'VCVARS_FAILED'; exit 1 }
foreach ($line in $envDump) {
  if ($line -match '^([^=]+)=(.*)$') {
    Set-Item -Path ("Env:" + $matches[1]) -Value $matches[2] -ErrorAction SilentlyContinue
  }
}

$cl = Get-Command cl.exe -ErrorAction SilentlyContinue
if (-not $cl) { Write-Output 'NO_CL'; exit 1 }
Write-Output "CL: $($cl.Source)"

if (-not (Test-Path 'output')) { New-Item -ItemType Directory -Path 'output' | Out-Null }

& cl.exe /nologo /O2 /MT /LD /EHsc /Fe:output\nl2shortcut_native.dll /Fo:output\ `
    src\uia.cpp src\inject.cpp src\window.cpp `
    /link ole32.lib oleaut32.lib user32.lib kernel32.lib
if ($LASTEXITCODE -ne 0) { Write-Output "COMPILE_FAILED($LASTEXITCODE)"; exit 1 }

Copy-Item 'output\nl2shortcut_native.dll' '..\nl2shortcut_native.dll' -Force
Write-Output 'BUILD_OK'
