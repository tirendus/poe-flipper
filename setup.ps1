# PoE Flipper — one-shot setup for a new machine.
# Installs Python 3.12 if needed, builds the venv, compiles the launcher.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
Write-Host "=== PoE Flipper setup ===" -ForegroundColor Yellow

# --- 1. find a usable Python (3.10+), install 3.12 via winget if absent ---
$py = $null
foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                 "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
                 "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
                 "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe")) {
    if (Test-Path $c) { $py = $c; break }
}
if (-not $py) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notmatch "WindowsApps") {
        $v = (& $cmd.Source --version) 2>&1
        if ("$v" -match "Python 3\.1[0-9]") { $py = $cmd.Source }
    }
}
if (-not $py) {
    Write-Host "Installing Python 3.12 (winget)..." -ForegroundColor Cyan
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements --silent
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
}
if (-not (Test-Path $py)) { throw "Python installation not found at $py" }
Write-Host "Python: $py"

# --- 2. virtual env + dependencies ---
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    & $py -m venv "$root\.venv"
}
Write-Host "Installing dependencies (this can take a few minutes)..." -ForegroundColor Cyan
& "$root\.venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r "$root\requirements.txt"

# --- 3. compile the pinnable launcher with the tray icon ---
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (Test-Path $csc) {
    & $csc /nologo /target:winexe /win32icon:"$root\flipper.ico" /out:"$root\PoE Flipper.exe" "$root\launcher.cs"
    Write-Host "Launcher compiled: PoE Flipper.exe"
} else {
    Write-Host "csc.exe not found - use start_flipper.bat instead of the exe." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Done! Start 'PoE Flipper.exe' (pin it to the taskbar if you like)." -ForegroundColor Green
Write-Host "In game: hover the Market Ratio bar holding Alt, press Alt+Q (any resolution; first capture self-calibrates)."
