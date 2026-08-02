param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$python = "$PSScriptRoot\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "가상환경이 없습니다. 먼저 py -m venv .venv 를 실행하십시오."
}

if (-not $SkipTests) {
    & $python -m unittest discover -s "$PSScriptRoot\tests" -v
    if ($LASTEXITCODE -ne 0) { throw "테스트가 실패했습니다." }
}

$legacyBundle = "$PSScriptRoot\dist\ServiceManager"
if (Test-Path -LiteralPath $legacyBundle) {
    Remove-Item -LiteralPath $legacyBundle -Recurse -Force
}

& $python -m PyInstaller --noconfirm --clean "$PSScriptRoot\ServiceManager.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 빌드가 실패했습니다." }

Write-Host "빌드 완료: $PSScriptRoot\dist\PythonServiceManager.exe"
