# End-to-end offline demo: capture -> handoff -> batch -> preprocess -> queue(mock) -> validate -> archive.
# Proves the whole data contract chain runs turn-key without Hunyuan keys.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\demo_e2e.ps1
param([string]$Batch = "0821", [string]$Sku = "APP-0821-001")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "== 0. fixture captures =="
python experiments\_make_fixture_captures.py

Write-Host "`n== 1. batch assemble =="
python -m pipeline.capture.batch_main $Batch

Write-Host "`n== 2. preprocess =="
python -m pipeline.preprocess "data\incoming\$Batch\$Sku" --out "data\api\$Batch\$Sku" --sku $Sku

Write-Host "`n== 3. queue (mock) =="
# reset work dir so queue starts at v1
Remove-Item -Recurse -Force "data\work\$Batch" -ErrorAction SilentlyContinue
python -m pipeline.queue $Batch --provider mock --fixture-dir experiments\fixtures

Write-Host "`n== 4. validate =="
$V = Get-ChildItem "data\work\$Batch\$Sku" -Directory | Sort-Object { [int]($_.Name -replace 'v','') } | Select-Object -Last 1
$Model = Join-Path $V.FullName "model.glb"
python -m pipeline.validate $Model --size-mm 120,80,40 --out (Join-Path $V.FullName "report.json")

Write-Host "`n== 5. archive =="
python -m pipeline.archive $Sku $Batch --category appliance --source-work $V.FullName

Write-Host "`n== done. Start board:  powershell scripts\start_webui.ps1 -Batch $Batch -NoCamera -Port 5010 =="
