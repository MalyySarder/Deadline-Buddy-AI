$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runner = Join-Path $projectRoot 'backend\runner.py'
$runtime = Join-Path $projectRoot '.runtime'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install backend/requirements.lock.txt first. See README.md.' }
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$pidFile = Join-Path $runtime 'bot.pid'
if (Test-Path -LiteralPath $pidFile) {
    $botPid = [int](Get-Content -LiteralPath $pidFile)
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$botPid"
    if ($existing -and $existing.CommandLine.Contains($runner)) {
        Write-Output "Bot already running, PID $botPid"
        exit 0
    }
}
Start-Process -FilePath $python -ArgumentList @('-u', ('"'+$runner+'"')) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'bot.log') -RedirectStandardError (Join-Path $runtime 'bot-error.log') | Out-Null
Write-Output 'Bot launched in background. Check .runtime/bot.log for Polling started. Stop: .\Stop-Bot.ps1'
