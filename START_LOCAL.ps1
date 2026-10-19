param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment is missing. See LOCAL_DEMO.md for setup.' }
& $taskPython app/manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Database setup failed.' }
$taskLogs = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
$taskWorker = Start-Process -FilePath $taskPython -ArgumentList @('-u','app/manage.py','worker') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLogs 'worker.log') -RedirectStandardError (Join-Path $taskLogs 'worker-error.log')
Write-Host "WebMonkie local preview: http://127.0.0.1:$Port/register/"
Write-Host 'Press Ctrl+C to stop the preview. Background worker stops with this script.'
try {
    & $taskPython app/manage.py runserver "127.0.0.1:$Port" --noreload --insecure
} finally {
    if (-not $taskWorker.HasExited) { Stop-Process -Id $taskWorker.Id -ErrorAction SilentlyContinue }
}
