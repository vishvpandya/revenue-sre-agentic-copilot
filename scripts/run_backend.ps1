$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $python -m uvicorn recovery_orchestrator.api:app --host 127.0.0.1 --port 8017
