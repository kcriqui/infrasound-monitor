# INFRA20 acquisition daemon launcher (Windows).
# Registered by setup.ps1 as the "InfraAcquire" scheduled task (runs at logon, hidden).
# Restarts the daemon if it ever exits. All settings (serial port, archive path, live
# buffer) come from config.toml in the project root -- nothing is hard-coded here.
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent          # project root (parent of deploy\)
$py   = 'python'                                   # change to a full python.exe path if not on PATH
$log  = Join-Path $PSScriptRoot 'acquire.log'
Set-Location $root                                 # so config.toml is found

# keep the log bounded
if (Test-Path $log) {
    $t = Get-Content $log -Tail 5000 -ErrorAction SilentlyContinue
    if ($t) { Set-Content $log $t -Encoding utf8 }
}

while ($true) {
    "$(Get-Date -Format o)  [daemon] starting infra-acquire (config-driven)" | Add-Content $log -Encoding utf8
    # No args: port/archive/live-file default from config.toml. cmd redirection keeps
    # the log flush-through and readable while the (never-ending) process runs.
    cmd /c "$py -m infrasound_monitor.acquire >> `"$log`" 2>&1"
    "$(Get-Date -Format o)  [daemon] exited (code $LASTEXITCODE); restarting in 15s" | Add-Content $log -Encoding utf8
    Start-Sleep -Seconds 15
}
