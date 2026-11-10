# Register a Windows Task Scheduler logon task. Standard PowerShell, no editor required.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$run = Join-Path $PSScriptRoot "run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$run`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "GrokPolymarket" -Action $action -Trigger $trigger -Settings $settings -Description "polybtc BTC 5-minute bot. Live orders only if enabled in the local config and environment." -Force | Out-Null
Write-Host "Registered scheduled task GrokPolymarket. It runs at logon. Live trading is not enabled by this task."
