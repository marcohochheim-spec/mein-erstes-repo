# Daily collection of 1-minute candles into the CSV archive (data/<SYMBOL>_1m.csv).
# DXLink only serves the latest ~8000 1m candles (~20 trading days), so this must run at least
# every couple of weeks; the Windows task "strategy-tests 1m-Archiv" runs it on weekdays at 23:00.
# Credentials come from the user environment (TASTYTRADE_CLIENT_ID / _SECRET / _REFRESH_TOKEN).

param(
    [string[]]$Symbols = @("SPY", "QQQ"),
    [string]$Period = "1m"
)

# Not "Stop": in Windows PowerShell 5.1 any stderr line of python would otherwise abort the run.
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
$dataDir = Join-Path $root "data"
New-Item -ItemType Directory -Force $logDir, (Join-Path $dataDir "backup") | Out-Null
$log = Join-Path $logDir ("collect_{0:yyyy-MM}.log" -f (Get-Date))

function Write-Log([string]$text) {
    Add-Content -Path $log -Value $text -Encoding UTF8
}

Write-Log ("=== {0:yyyy-MM-dd HH:mm:ss} Start: {1} {2}" -f (Get-Date), ($Symbols -join " "), $Period)

# One-generation backup, so a failed write can never cost the whole archive.
foreach ($s in $Symbols) {
    $csv = Join-Path $dataDir "$($s)_$Period.csv"
    if (Test-Path $csv) { Copy-Item $csv (Join-Path $dataDir "backup\$($s)_$Period.csv.bak") -Force }
}

$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8  # decode python's UTF-8 output correctly
$exitCode = 1
try {
    $output = & $python (Join-Path $root "fetch_candles.py") @Symbols --period $Period --append 2>&1
    $exitCode = $LASTEXITCODE
    $output | ForEach-Object { Write-Log "$_" }
} catch {
    Write-Log "FEHLER: $_"
}

Write-Log ("=== {0:yyyy-MM-dd HH:mm:ss} Ende, Exit-Code {1}" -f (Get-Date), $exitCode)
exit $exitCode
