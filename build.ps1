param(
    [string]$Version = (Get-Content -Encoding UTF8 "$PSScriptRoot\VERSION").Trim(),
    [switch]$SkipMsi,
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

& $python -m pip install --disable-pip-version-check pyinstaller cyclonedx-bom
& $python -m PyInstaller --noconfirm --clean "$PSScriptRoot\ServiceManager.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 빌드가 실패했습니다." }

$releaseDir = "$PSScriptRoot\release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
& $python -m cyclonedx_py environment --output-format JSON --output-file "$releaseDir\sbom.json" "$PSScriptRoot\.venv"

$signtool = Get-Command signtool.exe -ErrorAction SilentlyContinue
if ($env:SERVICE_MANAGER_SIGNING_PFX -and $signtool) {
    $targets = Get-ChildItem -LiteralPath "$PSScriptRoot\dist\ServiceManager" -Filter *.exe
    foreach ($target in $targets) {
        & $signtool.Source sign /fd SHA256 /td SHA256 /tr "http://timestamp.digicert.com" /f $env:SERVICE_MANAGER_SIGNING_PFX /p $env:SERVICE_MANAGER_SIGNING_PASSWORD $target.FullName
        if ($LASTEXITCODE -ne 0) { throw "서명 실패: $($target.FullName)" }
    }
} else {
    Write-Warning "서명 인증서가 없어 EXE 서명을 건너뜁니다. 상용 배포본은 반드시 서명해야 합니다."
}

if (-not $SkipMsi) {
    $heat = Get-Command heat.exe -ErrorAction SilentlyContinue
    $candle = Get-Command candle.exe -ErrorAction SilentlyContinue
    $light = Get-Command light.exe -ErrorAction SilentlyContinue
    if (-not $heat -or -not $candle -or -not $light) {
        throw "WiX Toolset 3.x(heat/candle/light)가 PATH에 필요합니다."
    }
    & $heat.Source dir "$PSScriptRoot\dist\ServiceManager" -cg ProductFiles -dr INSTALLFOLDER -gg -scom -sreg -sfrag -srd -var var.SourceDir -t "$PSScriptRoot\installer\heat-filter.xslt" -out "$PSScriptRoot\installer\HarvestedFiles.wxs"
    & $candle.Source -nologo -arch x64 -ext WixUtilExtension -dSourceDir="$PSScriptRoot\dist\ServiceManager" -dProductVersion=$Version -out "$PSScriptRoot\installer\obj\" "$PSScriptRoot\installer\Product.wxs" "$PSScriptRoot\installer\HarvestedFiles.wxs"
    & $light.Source -nologo -ext WixUtilExtension -out "$releaseDir\PythonServiceManager-$Version-x64.msi" "$PSScriptRoot\installer\obj\Product.wixobj" "$PSScriptRoot\installer\obj\HarvestedFiles.wixobj"
    if ($LASTEXITCODE -ne 0) { throw "MSI 빌드가 실패했습니다." }
    if ($env:SERVICE_MANAGER_SIGNING_PFX -and $signtool) {
        & $signtool.Source sign /fd SHA256 /td SHA256 /tr "http://timestamp.digicert.com" /f $env:SERVICE_MANAGER_SIGNING_PFX /p $env:SERVICE_MANAGER_SIGNING_PASSWORD "$releaseDir\PythonServiceManager-$Version-x64.msi"
    }
}

Write-Host "빌드 완료: $releaseDir"
