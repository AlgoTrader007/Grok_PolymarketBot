# Remove the kill latch. Does not start the bot and does not enable live trading.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$kill = Join-Path $PSScriptRoot "logs\KILL"
if (Test-Path -LiteralPath $kill) {
    Remove-Item -LiteralPath $kill -Force
    Write-Host "Removed logs\KILL. The bot was not started."
} else {
    Write-Host "logs\KILL was not present. The bot was not started."
}
Write-Host "Entries are still paper-only unless live.enabled and POLYBTC_LIVE_CONFIRM=YES are both set."
