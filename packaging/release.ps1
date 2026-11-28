# Builds the installer on this machine and publishes it as a GitHub release.
#
#   powershell -ExecutionPolicy Bypass -File packaging\release.ps1
#
# For when the friends' API ID is kept only in local files (build.env, or .env
# as a fallback) instead of repository secrets. Run on main, after bumping
# __version__ and merging.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Select-String -Path "src\telegram_mcp\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$tag = "v$version"
if ((git branch --show-current) -ne "main") { throw "Release from main only." }
if (git status --porcelain --untracked-files=no) { throw "Commit or stash your changes first." }
cmd /c "git fetch origin main --quiet 2>&1"
if ((git rev-parse HEAD) -ne (git rev-parse origin/main)) { throw "Local main differs from origin/main: pull or push first." }
# Through cmd: in Windows PowerShell a redirected native stderr line becomes a terminating error.
cmd /c "gh release view $tag >nul 2>&1"
if ($LASTEXITCODE -eq 0) { throw "Release $tag already exists: bump __version__ first." }

$madeBuildEnv = $false
if (-not (Test-Path "build.env")) {
    $values = @{}
    foreach ($line in Get-Content ".env") {
        if ($line -match '^\s*(TELEGRAM_API_ID|TELEGRAM_API_HASH)\s*=\s*(.+?)\s*$') { $values[$Matches[1]] = $Matches[2] }
    }
    if (-not ($values["TELEGRAM_API_ID"] -and $values["TELEGRAM_API_HASH"])) { throw "No build.env and no API ID in .env." }
    "TELEGRAM_BUILD_API_ID=$($values['TELEGRAM_API_ID'])`nTELEGRAM_BUILD_API_HASH=$($values['TELEGRAM_API_HASH'])`nTELEGRAM_BUILD_READ_ONLY_DEFAULT=true" |
        Set-Content -Encoding ascii "build.env"
    $madeBuildEnv = $true
}
try {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
} finally {
    if ($madeBuildEnv) { Remove-Item -Force "build.env" }
}

$setup = "dist\TelegramForClaude-Setup-$version.exe"

# Three steps, each retried: a 24 MB upload over a flaky connection times out
# now and then, and a draft can be finished later without starting over.
function Retry([string]$what, [scriptblock]$step) {
    foreach ($try in 1..4) {
        & $step
        if ($LASTEXITCODE -eq 0) { return }
        Write-Warning "$what failed (try $try of 4)"
        Start-Sleep -Seconds 15
    }
    throw "$what failed. Finish by hand: gh release upload / gh release edit $tag --draft=false"
}
cmd /c "gh release view $tag >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Retry "Creating the draft" { cmd /c "gh release create $tag --draft --target main --title ""Telegram для Claude $tag"" --generate-notes 2>&1" }
}
Retry "Uploading the installer" { cmd /c "gh release upload $tag $setup --clobber 2>&1" }
# The response to publishing can be lost even when publishing worked: check before retrying.
Retry "Publishing" {
    cmd /c "gh release edit $tag --draft=false >nul 2>&1"
    $draft = cmd /c "gh release view $tag --json isDraft --jq .isDraft 2>nul"
    $global:LASTEXITCODE = if ($draft -eq "false") { 0 } else { 1 }
}
Write-Host "Published $tag"
