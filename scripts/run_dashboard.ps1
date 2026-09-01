$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $python -m streamlit run dashboard\streamlit_app.py --server.address 127.0.0.1 --server.port 8512
