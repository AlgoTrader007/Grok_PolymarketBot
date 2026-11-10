$ErrorActionPreference = "Stop"
Unregister-ScheduledTask -TaskName "GrokPolymarket" -Confirm:$false
Write-Host "Removed scheduled task GrokPolymarket."
