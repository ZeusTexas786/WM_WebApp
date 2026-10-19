$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPidFile = Join-Path $taskRoot '.local\preview-pids.json'
if (Test-Path -LiteralPath $taskPidFile) {
    $taskPids = Get-Content -LiteralPath $taskPidFile | ConvertFrom-Json
    foreach ($taskProcessId in $taskPids) {
        $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$taskProcessId" -ErrorAction SilentlyContinue
        if ($taskProcess -and $taskProcess.CommandLine -like '*app/manage.py*' -and $taskProcess.ExecutablePath -eq (Join-Path $taskRoot '.venv\Scripts\python.exe')) {
            Stop-Process -Id $taskProcessId
        }
    }
}
