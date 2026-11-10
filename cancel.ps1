# Ask the official CLOB client to cancel resting orders for the configured account.
# This places no new order. It also latches logs\KILL.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Missing $Python. Run .\install.ps1 first."
}
& $Python -u bot.py --cancel-all
exit $LASTEXITCODE
