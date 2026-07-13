# Daily dashboard publish (Windows). Registered by `setup.ps1 -Dashboard` as the
# "InfraDashboard" task. Extends the PSD grid to now, rebuilds the dashboard +
# interactive waterfall into <project>\site, and (if <project>\site is a git repo with
# a remote) commits and force-pushes so a static host — e.g. GitHub Pages — updates.
#
# Prerequisite: the PSD grid cache must exist once. Build it with:
#   python tools\waterfall.py <archive> --start <YYYY-MM-DD> --end <YYYY-MM-DD> --cache analysis\grid_full.npz
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$py   = 'python'
$log  = Join-Path $PSScriptRoot 'publish.log'
$site = Join-Path $root 'site'
$cache = Join-Path $root 'analysis\grid_full.npz'
$archive = (& $py -c "from infrasound_monitor.config import ARCHIVE_DIR; print(ARCHIVE_DIR)").Trim()

if (Test-Path $log) { $t = Get-Content $log -Tail 2000 -EA SilentlyContinue; if ($t) { Set-Content $log $t -Encoding utf8 } }

"$(Get-Date -Format o)  [publish] rebuilding dashboard ..." | Add-Content $log -Encoding utf8
cmd /c "$py `"$root\tools\refresh.py`" `"$archive`" --cache `"$cache`" --out-dir `"$site`" >> `"$log`" 2>&1"

# publish to a static host if <site> is a git repo with a remote (set that up once, separately)
if (Test-Path (Join-Path $site '.git')) {
    Push-Location $site
    git add -A
    $head = $false; git rev-parse --verify HEAD 2>$null | Out-Null; if ($?) { $head = $true }
    if ($head) { git commit --amend -m "site update $(Get-Date -Format o)" --quiet 2>$null }
    else { git commit -m "initial site" --quiet 2>$null }
    if (git remote 2>$null) {
        git push --force --quiet 2>$null
        "$(Get-Date -Format o)  [publish] pushed" | Add-Content $log -Encoding utf8
    } else {
        "$(Get-Date -Format o)  [publish] rebuilt; no git remote (skipped push)" | Add-Content $log -Encoding utf8
    }
    Pop-Location
} else {
    "$(Get-Date -Format o)  [publish] rebuilt -> $site (git not set up; local only)" | Add-Content $log -Encoding utf8
}
