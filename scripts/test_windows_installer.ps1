param(
    [Parameter(Mandatory = $true)][string]$Artifact,
    [string]$EvidencePath = "",
    [string]$TemporaryRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $EvidencePath) {
    $EvidencePath = Join-Path $Root "artifacts\windows-installer-smoke.json"
}
if (-not $TemporaryRoot) {
    $TemporaryRoot = Join-Path $env:RUNNER_TEMP "nz-coder-installer-smoke"
}
$InstallPath = Join-Path $TemporaryRoot "Install Path With Spaces"
$Workspace = Join-Path $TemporaryRoot "Workspace Preserved Across Uninstall"
$Executable = Join-Path $InstallPath "nz-coder.exe"
$Uninstaller = Join-Path $InstallPath "unins000.exe"
$Steps = [ordered]@{}
$Failure = $null

function Invoke-Setup([string]$SetupPath) {
    $Arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR=`"$InstallPath`""
    $Process = Start-Process -FilePath $SetupPath -ArgumentList $Arguments -Wait -PassThru
    if ($Process.ExitCode -ne 0) { throw "Setup failed with exit code $($Process.ExitCode)." }
}

function Invoke-ProductJson([string[]]$Arguments, [int[]]$AllowedExitCodes) {
    $Output = & $Executable @Arguments
    if ($LASTEXITCODE -notin $AllowedExitCodes) {
        throw "Installed command failed ($LASTEXITCODE): $($Arguments -join ' ')"
    }
    return ($Output -join "`n") | ConvertFrom-Json
}

try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw "x64 Windows is required." }
    if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
        throw "Installer artifact does not exist: $Artifact"
    }
    if (Test-Path -LiteralPath $TemporaryRoot) {
        Remove-Item -LiteralPath $TemporaryRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force $Workspace | Out-Null
    Set-Content -LiteralPath (Join-Path $Workspace ".env") `
        -Value "# workspace.env.sentinel" -Encoding utf8
    $StateDirectory = Join-Path $Workspace ".nz-coder"
    New-Item -ItemType Directory -Force $StateDirectory | Out-Null
    Set-Content -LiteralPath (Join-Path $StateDirectory "sentinel.txt") `
        -Value "workspace.state.sentinel" -Encoding utf8

    Invoke-Setup $Artifact
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Installed nz-coder.exe was not found."
    }
    $Steps.install = "passed"

    Push-Location $Workspace
    try {
        $Help = & $Executable --help
        if ($LASTEXITCODE -ne 0 -or ($Help -join "`n") -notmatch "nz-coder") {
            throw "Installed --help smoke failed."
        }
        $Version = & $Executable --version
        if ($LASTEXITCODE -ne 0 -or -not ($Version -join "")) {
            throw "Installed --version smoke failed."
        }
        $Platform = Invoke-ProductJson -Arguments @("platform", "--json") -AllowedExitCodes @(0)
        $Doctor = Invoke-ProductJson -Arguments @("doctor", "--json") -AllowedExitCodes @(0, 1)
        $Config = Invoke-ProductJson -Arguments @("config", "show", "--json") -AllowedExitCodes @(0)
        $RunHelp = & $Executable run --help
        if ($LASTEXITCODE -ne 0 -or ($RunHelp -join "`n") -notmatch "usage") {
            throw "Installed headless entrypoint smoke failed."
        }
        $Steps.product = "passed"
        $Steps.platform = $Platform.platform
        $Steps.doctor = if ($null -ne $Doctor) { "valid-json" } else { "failed" }
        $Steps.config = if ($null -ne $Config) { "valid-json" } else { "failed" }
    }
    finally {
        Pop-Location
    }

    Invoke-Setup $Artifact
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Upgrade removed the installed executable."
    }
    $Steps.upgrade = "passed"

    if (-not (Test-Path -LiteralPath $Uninstaller -PathType Leaf)) {
        throw "Inno Setup uninstaller was not found."
    }
    $UninstallProcess = Start-Process -FilePath $Uninstaller `
        -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -Wait -PassThru
    if ($UninstallProcess.ExitCode -ne 0) {
        throw "Uninstall failed with exit code $($UninstallProcess.ExitCode)."
    }
    if (Test-Path -LiteralPath $Executable) { throw "Uninstall retained nz-coder.exe." }
    if ((Get-Content -LiteralPath (Join-Path $Workspace ".env") -Raw) -notmatch "workspace.env.sentinel") {
        throw "Uninstall changed the workspace .env sentinel."
    }
    if ((Get-Content -LiteralPath (Join-Path $StateDirectory "sentinel.txt") -Raw) -notmatch "workspace.state.sentinel") {
        throw "Uninstall changed the workspace state sentinel."
    }
    $Steps.uninstall = "passed"
    $Steps.workspace_preserved = $true
}
catch {
    $Failure = $_.Exception.Message
}
finally {
    $Evidence = [ordered]@{
        schema_version = 1
        success = ($null -eq $Failure)
        artifact = [IO.Path]::GetFileName($Artifact)
        steps = $Steps
        failure = if ($null -eq $Failure) { "" } else { $Failure.Substring(0, [Math]::Min(500, $Failure.Length)) }
    }
    New-Item -ItemType Directory -Force (Split-Path -Parent $EvidencePath) | Out-Null
    $Evidence | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $EvidencePath -Encoding utf8
}

if ($null -ne $Failure) { throw $Failure }
Write-Output "Windows installer lifecycle smoke passed."
