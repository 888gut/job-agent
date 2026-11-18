# Puts an "Offer Ledger" icon on your desktop (and in the Start menu) for this copy of the project.
# The icon always runs the latest code in this folder: it restarts the server if the code changed
# (never in the middle of a run) and opens the dashboard in Chrome.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\install-desktop-icon.ps1
#        (start.bat runs this for you the first time.)

$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot "server.ps1"
$icon = Join-Path $root "web\offer-ledger.ico"
$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$places = @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $places) {
    if (-not (Test-Path $dir)) { continue }
    # The app used to be called Job Agent: replace that older shortcut.
    $old = Join-Path $dir "Job Agent.lnk"
    if (Test-Path $old) { Remove-Item $old -Force }
    $lnk = $shell.CreateShortcut((Join-Path $dir "Offer Ledger.lnk"))
    $lnk.TargetPath = $ps
    $lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" open"
    $lnk.WorkingDirectory = $root
    $lnk.WindowStyle = 7   # minimized: no console flash
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Description = "Open Offer Ledger (always the latest code in $root)"
    $lnk.Save()
}
"Offer Ledger icon added to your desktop and Start menu."
