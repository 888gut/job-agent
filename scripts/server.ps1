# Start / stop / restart the Job Agent server as a hidden background process.
# Usage: server.ps1 start | stop | restart | status
param([ValidateSet("start", "stop", "restart", "status")][string]$Action = "start")

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

switch ($Action) {
    "start"   { Start-Server }
    "stop"    { Stop-Server }
    "restart" { Stop-Server; Start-Server }
    "status"  { if (Get-Server) { "running" } else { "stopped" } }
}
