# Signs this PC's GitHub tool in to your GitHub account (you approve it in your browser).
$host.UI.RawUI.WindowTitle = "GitHub sign-in for Job Agent"
Write-Host ""
Write-Host "  GitHub sign-in for Job Agent" -ForegroundColor Cyan
Write-Host "  1. Press Enter when asked. github.com/login/device opens in your browser."
Write-Host "  2. Paste the one-time code shown below (it is already copied), click Continue, then Authorize."
Write-Host ""
& 'C:\Program Files\GitHub CLI\gh.exe' auth login --hostname github.com --git-protocol https --web
Write-Host ""
& 'C:\Program Files\GitHub CLI\gh.exe' auth status
Write-Host ""
Read-Host "  All set. Press Enter to close this window"
