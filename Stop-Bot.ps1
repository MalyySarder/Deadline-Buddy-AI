$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot '.runtime\bot.pid'
$runner = Join-Path $PSScriptRoot 'backend\runner.py'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No bot PID file.'; exit 0 }
$botPid = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$botPid"
if ($process -and $process.CommandLine.Contains($runner)) {
    Stop-Process -Id $botPid
    Write-Output 'Bot stopped.'
} else {
    Write-Output 'Bot is not running; no unrelated process was stopped.'
}
