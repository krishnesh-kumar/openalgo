# Repoint the OpenAlgo-Server scheduled task's log redirect to D:.
#
# WHY A SCRIPT FILE AND NOT AN INLINE COMMAND: passing this argument through
# bash -> powershell mangled '$env:PATH=...' into a bare '$env:PATH;' on
# 2026-10-14, which would have left uv off the PATH and stopped OpenAlgo from
# starting at all. A file has no second layer of shell escaping.
#
# The redirect must live on D: because the HSM reconnect loop writes ~300 MB
# an hour to it, and on 2026-10-14 that filled C: and wedged the trading
# session thirteen minutes before its hard close.

$ErrorActionPreference = 'Stop'

$inner = @'
$env:PATH='C:\Users\krish\.local\bin;'+$env:PATH; Set-Location 'C:\Users\krish\trading-bot\openalgo'; uv run app.py *>> 'D:\trading-data\openalgo-log\openalgo_task.log'
'@

$arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "' + $inner.Trim() + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
Set-ScheduledTask -TaskName 'OpenAlgo-Server' -Action $action | Out-Null

$now = (Get-ScheduledTask -TaskName 'OpenAlgo-Server').Actions[0].Arguments
Write-Output "task argument now:"
Write-Output "  $now"

# verify the three things that must be true
$ok = $true
if ($now -notmatch [regex]::Escape(".local\bin;'+")) { Write-Output "  FAIL: PATH prefix missing"; $ok = $false }
if ($now -notmatch 'uv run app\.py')                 { Write-Output "  FAIL: app.py launch missing"; $ok = $false }
if ($now -notmatch [regex]::Escape('D:\trading-data\openalgo-log')) { Write-Output "  FAIL: redirect not on D:"; $ok = $false }
if ($ok) {
    Write-Output "  VERIFIED: PATH prefix, app.py launch, D: redirect all present"
    exit 0
}
exit 1
