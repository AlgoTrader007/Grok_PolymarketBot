# Restart-on-exit wrapper. Task Scheduler should start this script, not bot.py.
# A logs\KILL file does not stop this loop; the bot keeps running and places nothing.
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Missing $Python. From this folder run .\install.ps1"
    exit 1
}
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "logs") | Out-Null
Set-Content -LiteralPath (Join-Path $PSScriptRoot "logs\run.pid") -Value $PID -Encoding ascii
try {
    while ($true) {
        $proc = Start-Process -FilePath $Python -ArgumentList @("-u", "bot.py") -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru
        Set-Content -LiteralPath (Join-Path $PSScriptRoot "logs\bot.pid") -Value $proc.Id -Encoding ascii
        Wait-Process -Id $proc.Id
        Start-Sleep -Seconds 3
    }
} finally {
    Remove-Item -LiteralPath (Join-Path $PSScriptRoot "logs\run.pid") -ErrorAction SilentlyContinue
}
