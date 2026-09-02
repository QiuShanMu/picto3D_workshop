# One-click launcher for the production WebUI (batch board + capture wizard).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\start_webui.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start_webui.ps1 -Batch 0821 -Port 5011
#   powershell -ExecutionPolicy Bypass -File scripts\start_webui.ps1 -Batch 0821 -NoCamera
#   powershell -ExecutionPolicy Bypass -File scripts\start_webui.ps1 -Install
param(
    [string]$Batch = "0812",
    [int]$Port = 5010,
    [switch]$NoCamera,
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
    & $Python -m pip install -e ".[dev,capture-web,capture-barcode]" --quiet
}

& $Python -c "import flask" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[warn] missing flask. Run:  pip install -e '.[capture-web]'   OR add -Install."
    exit 1
}

if ($NoCamera) {
    Write-Host "== WebUI (board only, no camera) http://127.0.0.1:$Port =="
    & $Python -m pipeline.webui --batch $Batch --port $Port --no-camera
} else {
    Write-Host "== WebUI (capture + board) http://127.0.0.1:$Port =="
    & $Python -m pipeline.webui --batch $Batch --port $Port
}
