# Запуск бота с автоперезапуском при падении.
# Используется планировщиком задач Windows или вручную.

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$MainScript = Join-Path $ProjectRoot "main.py"
$LogDir = Join-Path $ProjectRoot "logs"
$LogFile = Join-Path $LogDir "bot.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

function Write-Log {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

if (-not (Test-Path $VenvPython)) {
    Write-Log "ERROR: venv not found. Run: python -m venv .venv && pip install -r requirements.txt"
    exit 1
}

if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
    Write-Log "ERROR: .env not found. Copy .env.example to .env and set DISCORD_TOKEN."
    exit 1
}

Set-Location $ProjectRoot
Write-Log "Bot supervisor started."

# Перезапуск через 10 секунд, если бот упал
while ($true) {
    Write-Log "Starting bot..."
    try {
        & $VenvPython $MainScript 2>&1 | ForEach-Object {
            Write-Log $_
            Write-Host $_
        }
    }
    catch {
        Write-Log "ERROR: $($_.Exception.Message)"
    }

    Write-Log "Bot stopped. Restart in 10 seconds..."
    Start-Sleep -Seconds 10
}
