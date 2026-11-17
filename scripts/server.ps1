# Start / stop / restart the Job Agent server as a hidden background process.
# Usage: server.ps1 start | stop | restart | status | open
#   open = what the desktop icon runs: make sure the server is running the latest code
#          (restart it if any code changed since it started), then show the dashboard in Chrome.
param([ValidateSet("start", "stop", "restart", "status", "open")][string]$Action = "start")

$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "data\server.pid"
$log = Join-Path $root "data\server.log"
$python = Join-Path $root ".venv\Scripts\python.exe"

function Get-Server {
    if (Test-Path $pidFile) {
        $id = [int](Get-Content $pidFile -ErrorAction SilentlyContinue)
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($p) { return $p }
    }
    # also catch a server started some other way on port 8765
    $conn = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue }
    return $null
}

function Stop-Server {
    $p = Get-Server
    if ($p) { Stop-Process -Id $p.Id -Force; Start-Sleep -Seconds 1; "Stopped the Job Agent server." }
    else { "The Job Agent server isn't running." }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

function Start-Server {
    if (Get-Server) { "The Job Agent server is already running at http://127.0.0.1:8765"; return }
    New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
    $p = Start-Process -FilePath $python -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765" `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    $p.Id | Set-Content $pidFile
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-WebRequest "http://127.0.0.1:8765/api/settings" -UseBasicParsing -TimeoutSec 2 | Out-Null; "Job Agent is running at http://127.0.0.1:8765"; return } catch {}
    }
    "The server didn't answer yet. Check $log.err"
}

function Test-CodeChanged($proc) {
    # Python and config are loaded once at startup; web files are served fresh on each request.
    $newest = Get-ChildItem -Path (Join-Path $root "app"), (Join-Path $root "requirements.txt"), (Join-Path $root ".env") `
        -Recurse -File -Include *.py, requirements.txt, .env -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    return $newest -and $newest.LastWriteTime -gt $proc.StartTime
}

function Test-Busy {
    # Never restart in the middle of a search or an application run; the new code loads next time.
    # If the server can't say, assume busy rather than risk killing a run.
    try {
        $o = Invoke-RestMethod "http://127.0.0.1:8765/api/overview" -TimeoutSec 5
        return [bool]($o.batch.running -or $o.run.running)
    } catch { return $true }
}

function Open-Dashboard {
    $p = Get-Server
    if ($p -and (Test-CodeChanged $p) -and -not (Test-Busy)) { Stop-Server | Out-Null; $p = $null }
    if (-not $p) { Start-Server | Out-Null }
    # A fresh address on every launch, so Chrome can never show an old saved copy of the dashboard.
    $url = "http://127.0.0.1:8765/?launch=$([DateTimeOffset]::Now.ToUnixTimeSeconds())"
    $chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($chrome) { Start-Process $chrome $url } else { Start-Process $url }
}

switch ($Action) {
    "open"    { Open-Dashboard }
    "start"   { Start-Server }
    "stop"    { Stop-Server }
    "restart" { Stop-Server; Start-Server }
    "status"  { if (Get-Server) { "running" } else { "stopped" } }
}
