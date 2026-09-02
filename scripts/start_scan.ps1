# One-click launcher for the standalone SKU scan service (phone as camera).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\start_scan.ps1                  # HTTP (default)
#   powershell -ExecutionPolicy Bypass -File scripts\start_scan.ps1 -Https          # HTTPS (auto cert)
#   powershell -ExecutionPolicy Bypass -File scripts\start_scan.ps1 -Port 5070 -Https
#   powershell -ExecutionPolicy Bypass -File scripts\start_scan.ps1 -Install
param(
    [int]$Port = 5070,
    [switch]$Https,
    [switch]$Install
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = "python"
if ($Install) {
    if (-not (Test-Path ".venv")) {
        Write-Host "== creating venv .venv =="
        python -m venv .venv
    }
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) { $Python = "python" }
    Write-Host "== installing deps =="
    & $Python -m pip install -e ".[dev,capture-web,capture-barcode,scan]" --quiet
}

& $Python -c "import flask" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[warn] missing flask. Run:  pip install -e '.[capture-web]'   OR add -Install."
    exit 1
}

if ($Https) {
    Write-Host "== SKU scan service (HTTPS) =="
    & $Python -m pipeline.scan --host 0.0.0.0 --port $Port --https
} else {
    Write-Host "== SKU scan service (HTTP) =="
    & $Python -m pipeline.scan --host 0.0.0.0 --port $Port
}
