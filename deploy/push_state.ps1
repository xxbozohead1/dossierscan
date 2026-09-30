# Moves the running state (data\ and .env) from this Windows machine to the server. It stops and disables the
# Windows copies first: two watchers would both poll X (CREDIT twice) and both post to Telegram.
# Run after deploy/setup.sh has run on the server:
#   powershell -ExecutionPolicy Bypass -File deploy\push_state.ps1 -Server root@203.0.113.7
# -Dir is the checkout on the server (default /opt/orbio). Needs ssh/scp (built into Windows 11).
param([Parameter(Mandatory = $true)][string]$Server, [string]$Dir = "/opt/orbio")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

# 1. the log-on tasks, then any runner or Python they leave behind (stopping a task can orphan its child)
foreach ($t in "orbio-watch", "orbio-proof", "orbio-bot") {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $t
        Disable-ScheduledTask -TaskName $t | Out-Null
        Write-Host "stopped and disabled task $t"
    }
}
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='powershell.exe'" |
    Where-Object { $_.CommandLine -match 'orbio_watch\.py|proof_index\.py|dossier_bot\.py|run_(watch|proof|bot)\.ps1' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "stopped $($_.ProcessId): $($_.CommandLine)" }
Start-Sleep -Seconds 3

# 2. fold SQLite's write-ahead logs into the database files, so the copies are complete
python -c "import pathlib, sqlite3; [sqlite3.connect(p).execute('PRAGMA wal_checkpoint(TRUNCATE)').connection.close() for p in pathlib.Path('data').glob('*.db')]"
if ($LASTEXITCODE -ne 0) { throw "checkpoint failed" }

# 3. pack, upload, unpack. The site is rebuilt on the server; logs and heartbeats stay here.
$pack = "orbio-state.tgz"
tar -czf $pack --exclude=data/site --exclude=data/site.new --exclude=data/site.old --exclude=data/site_deploy `
    --exclude=*.log --exclude=*.heartbeat --exclude=*.lock data .env
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
scp $pack "${Server}:/tmp/$pack"
if ($LASTEXITCODE -ne 0) { throw "upload failed" }
ssh $Server "tar -xzf /tmp/$pack -C $Dir && rm /tmp/$pack && chown -R orbio:orbio $Dir/data $Dir/.env && chmod 600 $Dir/.env && chmod 711 $Dir/data"
if ($LASTEXITCODE -ne 0) { throw "unpacking on the server failed" }
Remove-Item $pack
Write-Host "Done. The Windows tasks stay disabled; start the services on the server (deploy/HETZNER.md, step 6)."
