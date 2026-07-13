# One-shot Windows setup for the INFRA20 tool.
#
#   powershell -ExecutionPolicy Bypass -File deploy\setup.ps1
#   ...add -Dashboard to also register the daily dashboard-publish task.
#
# Per-user scheduled tasks don't need admin. It installs the package, creates your
# config.toml, and registers the acquisition daemon to run at logon.
param([switch]$Dashboard)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py = 'python'

Write-Host "== INFRA20 setup ==  project: $root"

# 1. Python 3.10+ on PATH
try { & $py -c "import sys; assert sys.version_info >= (3,10), sys.version" | Out-Null }
catch { Write-Error "Python 3.10+ not found on PATH. Install it (add to PATH) and re-run."; return }
Write-Host "  Python OK: $(& $py --version)"

# 2. Install the package (editable) + its dependencies
Write-Host "  Installing the package (pip install -e .) ..."
& $py -m pip install -e . --quiet --disable-pip-version-check

# 3. Config: create config.toml from the example if missing
if (-not (Test-Path "$root\config.toml")) {
    Copy-Item "$root\config.example.toml" "$root\config.toml"
    Write-Warning "Created config.toml from the example — EDIT IT for your station:"
    Write-Warning "  serial port, coordinates, sample rate, UTC offset  ($root\config.toml)"
} else {
    Write-Host "  config.toml already present."
}

# 4. Register the acquisition daemon (runs at logon, restarts on failure)
$daemon = Join-Path $PSScriptRoot 'acquire-daemon.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File `"$daemon`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName 'InfraAcquire' -Action $action -Trigger $trigger `
    -Settings $settings -Force -Description 'INFRA20 acquisition -> miniSEED SDS' | Out-Null
Write-Host "  Registered scheduled task 'InfraAcquire' (starts at logon)."

# 5. Optional: daily dashboard publish
if ($Dashboard) {
    $pub = Join-Path $PSScriptRoot 'publish.ps1'
    $act2 = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File `"$pub`""
    $trig2 = New-ScheduledTaskTrigger -Daily -At 6:15am
    $set2 = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName 'InfraDashboard' -Action $act2 -Trigger $trig2 `
        -Settings $set2 -Force -Description 'INFRA20 daily dashboard rebuild' | Out-Null
    Write-Host "  Registered scheduled task 'InfraDashboard' (daily 06:15)."
}

Write-Host ""
Write-Host "Done. Next:"
Write-Host "  1. Edit config.toml (serial port + station details) if you haven't."
Write-Host "  2. Start acquiring now:   Start-ScheduledTask -TaskName InfraAcquire"
Write-Host "  3. Watch it live:         python tools\live.py"
