$ROOT = "C:\Projects\RealtimeMonitor"
$LOG  = "$ROOT\pipeline.log"
function Log($msg) { $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"; "$ts  $msg" | Tee-Object -FilePath $LOG -Append }

Log "=== pipeline resume from Step 3 ==="
Log "Step 3: walk_forward.py expanding ..."
$p = Start-Process python -ArgumentList "-X utf8 walk_forward.py --data-start 20070101 --exclude-file excluded_stocks.json" -WorkingDirectory $ROOT -RedirectStandardOutput "$ROOT\walk_forward.log" -RedirectStandardError "$ROOT\walk_forward_err.log" -NoNewWindow -PassThru -Wait
if ($p.ExitCode -ne 0) { Log "ERROR: walk_forward expanding failed (exit $($p.ExitCode))"; exit 1 }
Log "Step 3 done"

Log "Step 4: walk_forward.py rolling7y ..."
$p = Start-Process python -ArgumentList "-X utf8 walk_forward.py --rolling 7 --data-start 20070101 --exclude-file excluded_stocks.json" -WorkingDirectory $ROOT -RedirectStandardOutput "$ROOT\walk_forward_rolling7y.log" -RedirectStandardError "$ROOT\walk_forward_rolling7y_err.log" -NoNewWindow -PassThru -Wait
if ($p.ExitCode -ne 0) { Log "ERROR: walk_forward rolling7y failed (exit $($p.ExitCode))"; exit 1 }
Log "Step 4 done"

Log "=== pipeline complete ==="
