param(
    [switch]$SkipDeploy
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "=== Tournament Draft Bot -> Railway ===" -ForegroundColor Cyan

if (-not (Get-Command railway -ErrorAction SilentlyContinue)) {
    Write-Host "Railway CLI ne ustanovlen. Sm. DEPLOY.md" -ForegroundColor Red
    exit 1
}

$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host ".env ne nayden" -ForegroundColor Red
    exit 1
}

$tokenLine = Get-Content $envFile | Where-Object { $_ -match '^DISCORD_TOKEN=' } | Select-Object -First 1
$token = $tokenLine -replace '^DISCORD_TOKEN=', ''
if (-not $token -or $token -eq 'your_bot_token_here') {
    Write-Host "Ukazhite DISCORD_TOKEN v .env" -ForegroundColor Red
    exit 1
}

railway whoami 2>$null
if ($LASTEXITCODE -ne 0) {
    railway login
}

if (-not (Test-Path (Join-Path $ProjectRoot ".railway"))) {
    railway init --name tournament-draft-bot
}

Write-Host "Ustanovka peremennykh..." -ForegroundColor Yellow
railway variables --set "DISCORD_TOKEN=$token" --set "DATA_DIR=/app/data"

Write-Host ""
Write-Host "V Railway dobavte Volume: Mount Path = /app/data" -ForegroundColor Magenta
Write-Host ""

if ($SkipDeploy) { exit 0 }

Write-Host "Deploy..." -ForegroundColor Green
railway up --detach

Write-Host "Gotovo. Logi: railway logs" -ForegroundColor Green
