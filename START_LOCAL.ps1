param([ValidateRange(1024,65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python environment is missing. See LOCAL_DEMO.md for setup.' }
# A local preview always uses fictional fixtures and cannot spend API credits.
$env:WEBMONKIE_LOCAL = '1'
$env:DJANGO_SETTINGS_MODULE = 'config.preview'
$env:AUTOMATIC_WORKFLOW_ENABLED = '1'
$env:AUTOMATIC_TRANSPORT = 'fixtures'
$env:AI_MAX_DAILY_REQUESTS = '0'
$env:AI_API_KEY = ''
$env:AUTOMATIC_REMOTE_EXECUTOR = ''
$env:LIFECYCLE_EXECUTION_ENABLED = '0'
$env:SMTP_HOST = ''
$env:WEBMONKIE_PREVIEW_PORT = "$Port"
& $taskPython app/manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Database setup failed.' }
$taskLogs = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
$taskWorker = Start-Process -FilePath $taskPython -ArgumentList @('-u','app/manage.py','cloud_dispatcher','--limit','1','--sleep','5') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskLogs 'worker.log') -RedirectStandardError (Join-Path $taskLogs 'worker-error.log')
Write-Host "WebMonkie local preview: http://127.0.0.1:$Port/register/"
Write-Host 'Press Ctrl+C to stop the preview. Background worker stops with this script.'
Write-Host 'Fictional sample only. Paid AI, outbound email and retention execution are disabled.'
try {
    & $taskPython app/manage.py runserver "127.0.0.1:$Port" --noreload --insecure
} finally {
    if (-not $taskWorker.HasExited) { Stop-Process -Id $taskWorker.Id -ErrorAction SilentlyContinue }
}
