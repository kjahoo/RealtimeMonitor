# run_pipeline.ps1
# backfill done marker -> universe_filter -> walk_forward expanding -> walk_forward rolling7y

$ROOT = "C:\Projects\RealtimeMonitor"
$LOG  = "$ROOT\pipeline.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Tee-Object -FilePath $LOG -Append
}

Log "=== pipeline start ==="

# ── Step 1: wait for backfill_done.marker ────────────────────────────────
Log "Step 1: waiting for backfill to finish..."
$marker  = "$ROOT\backfill_done.marker"
$timeout = [DateTime]::Now.AddHours(10)

while ($true) {
    if ([DateTime]::Now -gt $timeout) {
        Log "ERROR: backfill timeout (10h) -- pipeline aborted"
        exit 1
    }
    if (Test-Path $marker) {
        Log "backfill marker found -- proceeding"
        break
    }
    Start-Sleep -Seconds 60
}

# ── Step 2: build_universe_filter ────────────────────────────────────────
Log "Step 2: build_universe_filter.py ..."
$p = Start-Process python `
    -ArgumentList "-X utf8 build_universe_filter.py" `
    -WorkingDirectory $ROOT `
    -RedirectStandardOutput "$ROOT\universe_filter.log" `
    -RedirectStandardError  "$ROOT\universe_filter_err.log" `
    -NoNewWindow -PassThru -Wait
if ($p.ExitCode -ne 0) {
    Log "ERROR: build_universe_filter.py failed (exit $($p.ExitCode))"
    exit 1
}
Log "Step 2 done"

# ── Step 3: walk_forward expanding ───────────────────────────────────────
Log "Step 3: walk_forward.py --mode expanding ..."
$p = Start-Process python `
    -ArgumentList "-X utf8 walk_forward.py --data-start 20070101 --exclude-file excluded_stocks.json" `
    -WorkingDirectory $ROOT `
    -RedirectStandardOutput "$ROOT\walk_forward.log" `
    -RedirectStandardError  "$ROOT\walk_forward_err.log" `
    -NoNewWindow -PassThru -Wait
if ($p.ExitCode -ne 0) {
    Log "ERROR: walk_forward expanding failed (exit $($p.ExitCode))"
    exit 1
}
Log "Step 3 done"

# ── Step 4: walk_forward rolling7y ───────────────────────────────────────
Log "Step 4: walk_forward.py --mode rolling7y ..."
$p = Start-Process python `
    -ArgumentList "-X utf8 walk_forward.py --rolling 7 --data-start 20070101 --exclude-file excluded_stocks.json" `
    -WorkingDirectory $ROOT `
    -RedirectStandardOutput "$ROOT\walk_forward_rolling7y.log" `
    -RedirectStandardError  "$ROOT\walk_forward_rolling7y_err.log" `
    -NoNewWindow -PassThru -Wait
if ($p.ExitCode -ne 0) {
    Log "ERROR: walk_forward rolling7y failed (exit $($p.ExitCode))"
    exit 1
}
Log "Step 4 done"

Log "=== pipeline complete ==="