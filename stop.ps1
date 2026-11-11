# Stop the bot and latch the kill switch so a Task Scheduler restart cannot place orders.
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "logs") | Out-Null
Set-Content -LiteralPath (Join-Path $PSScriptRoot "logs\KILL") -Value "kill" -Encoding ascii
foreach ($name in @("run.pid", "bot.pid")) {
    $path = Join-Path $PSScriptRoot "logs\$name"
    if (Test-Path -LiteralPath $path) {
        $raw = Get-Content -LiteralPath $path -ErrorAction SilentlyContinue | Select-Object -First 1
        $procId = 0
        if ([int]::TryParse([string]$raw, [ref]$procId) -and $procId -gt 0) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
}
Write-Host "Stopped. logs\KILL is set, so new entries stay off after a restart."
Write-Host "Remove that latch with .\clear-kill.ps1 only when you intend to allow entries again."
Write-Host "Live mode is not enabled by removing the latch."
