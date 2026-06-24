# Удаляет автозапуск бота из планировщика Windows.

$TaskName = "TournamentDraftBot"

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Autostart removed: $TaskName" -ForegroundColor Green
}
catch {
    Write-Host "Task not found or already removed." -ForegroundColor Yellow
}
