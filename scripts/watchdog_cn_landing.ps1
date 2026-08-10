# Resume CN full landing if orchestrator died without done status.
# Usage: powershell -File scripts/watchdog_cn_landing.ps1
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$env:PYTHONPATH = $Root
$env:PYTHONUNBUFFERED = "1"
$progress = Join-Path $Root "reports\cn_full_landing_progress.json"
$log = Join-Path $Root "reports\cn_full_landing_watchdog.log"

function Write-Log($m) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
  Add-Content -Path $log -Value $line -Encoding UTF8
  Write-Host $line
}

function OrchestratorAlive {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*run_full_landing*' } |
    Measure-Object | Select-Object -ExpandProperty Count
}

function IsDone {
  if (-not (Test-Path $progress)) { return $false }
  $t = Get-Content $progress -Raw -ErrorAction SilentlyContinue
  return ($t -match '"status":\s*"(done|done_with_errors)"')
}

Write-Log "watchdog start root=$Root"
while ($true) {
  if (IsDone) {
    Write-Log "goal complete, watchdog exit"
    break
  }
  $n = OrchestratorAlive
  if ($n -lt 1) {
    Write-Log "orchestrator missing, restarting run_full_landing"
    Start-Process -FilePath "python" -ArgumentList @("-u","-m","crawlers.cn.run_full_landing") -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep -Seconds 15
  }
  Start-Sleep -Seconds 120
}
