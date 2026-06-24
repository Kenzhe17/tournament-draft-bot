# Регистрирует автозапуск бота при входе в Windows.
# Запустите PowerShell от имени администратора (один раз).

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RunScript = Join-Path $ProjectRoot "scripts\run-bot.ps1"
$TaskName = "TournamentDraftBot"

if (-not (Test-Path $RunScript)) {
    Write-Host "ERROR: run-bot.ps1 not found." -ForegroundColor Red
    exit 1
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`"" `
    -WorkingDirectory $ProjectRoot

# Запуск при входе пользователя в систему
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Discord Tournament Draft Bot — автозапуск" `
    -Force | Out-Null

Write-Host ""
Write-Host "Autostart installed: $TaskName" -ForegroundColor Green
Write-Host "Bot will start automatically when you log in to Windows."
Write-Host ""
Write-Host "Start now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop:       Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Remove:     .\scripts\uninstall-autostart.ps1"
Write-Host "Logs:       $ProjectRoot\logs\bot.log"
Write-Host ""

$startNow = Read-Host "Start the bot now? (y/n)"
if ($startNow -eq "y" -or $startNow -eq "Y") {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Task started. Check logs in a few seconds." -ForegroundColor Green
}
