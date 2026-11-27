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
git fetch origin main --quiet
if ((git rev-parse HEAD) -ne (git rev-parse origin/main)) { throw "Local main differs from origin/main: pull or push first." }
gh release view $tag *> $null
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
gh release create $tag $setup --target main --title "Telegram для Claude $tag" --generate-notes
if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
Write-Host "Published $tag"
