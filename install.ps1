# Create the local virtualenv and install dependencies.
# Intended VPS directory: D:\python\Grok_Polymarket
# Does not enable live trading and does not read or print secret values.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$Expected = "D:\python\Grok_Polymarket"
if ($PSScriptRoot.TrimEnd("\") -ne $Expected) {
    Write-Warning "This copy is at $PSScriptRoot. The documented VPS path is $Expected."
}
if (Test-Path -LiteralPath ".venv") {
    Write-Host "Using the existing .venv"
} else {
    $created = $false
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @("-3.12", "-3.13", "-3.11")) {
            & py $version -m venv .venv
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
                $created = $true
                break
            }
        }
    }
    if (-not $created) {
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
            throw "Python 3.11 or newer is required. Install Python 3.12 from python.org and enable the py launcher."
        }
    }
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }
if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "Created .env from .env.example. Live trading is still off."
}
Write-Host "Installed. Live orders stay off until you set live.enabled and POLYBTC_LIVE_CONFIRM locally."
