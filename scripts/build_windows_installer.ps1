param(
    [string]$Python = "python",
    [string]$Iscc = "",
    [string]$OutputDirectory = "",
    [string]$EvidencePath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "NZ-Coder Windows installer builds require an x64 Windows host."
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Root "dist\installer"
}
if (-not $EvidencePath) {
    $EvidencePath = Join-Path $Root "artifacts\windows-installer-build.json"
}
if (-not $Iscc) {
    $Iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
}
if (-not (Test-Path -LiteralPath $Iscc -PathType Leaf)) {
    throw "ISCC.exe was not found at $Iscc"
}

$IdentityText = & $Python (Join-Path $PSScriptRoot "windows_installer_contract.py") `
    --root $Root --json
if ($LASTEXITCODE -ne 0) { throw "Could not load installer contract." }
$Identity = $IdentityText | ConvertFrom-Json
$BuildRoot = Join-Path $Root "build\windows-installer"
$FrozenRoot = Join-Path $Root "dist\NZ-Coder"
foreach ($Path in @($BuildRoot, $FrozenRoot, $OutputDirectory)) {
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}
New-Item -ItemType Directory -Force $BuildRoot, $OutputDirectory | Out-Null

& $Python -m PyInstaller --noconfirm --clean `
    --workpath $BuildRoot --distpath (Join-Path $Root "dist") `
    (Join-Path $Root "packaging\windows\nz-coder.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

$Validation = & $Python (Join-Path $PSScriptRoot "windows_installer_contract.py") `
    --root $Root --validate-frozen $FrozenRoot --json
if ($LASTEXITCODE -ne 0) { throw "Frozen runtime validation failed: $Validation" }

& $Iscc "/DAppVersion=$($Identity.version)" "/DSourceDir=$FrozenRoot" `
    "/DOutputDir=$OutputDirectory" (Join-Path $Root "packaging\windows\nz-coder.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }

$Artifact = Join-Path $OutputDirectory $Identity.artifact_name
if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
    throw "Expected setup artifact was not produced: $Artifact"
}
$Hash = Get-FileHash -LiteralPath $Artifact -Algorithm SHA256
$Evidence = [ordered]@{
    schema_version = 1
    product = "NZ-Coder"
    version = $Identity.version
    architecture = $Identity.architecture
    artifact = [IO.Path]::GetFileName($Artifact)
    size_bytes = (Get-Item -LiteralPath $Artifact).Length
    sha256 = $Hash.Hash.ToLowerInvariant()
    frozen_validation = "passed"
}
New-Item -ItemType Directory -Force (Split-Path -Parent $EvidencePath) | Out-Null
$Evidence | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
Write-Output $Artifact
