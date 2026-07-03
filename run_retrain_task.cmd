@echo off
REM Task Scheduler launcher: Rolling7y filtered retrain supervisor on GPU (conda).
REM Independent of Claude Code app - survives app close and reboot.
set PYTHONIOENCODING=utf-8
cd /d C:\Projects\RealtimeMonitor
"C:\Users\JH_Signature\miniconda3\Scripts\conda.exe" run -n trading_env --no-capture-output python -u "C:\Projects\RealtimeMonitor\run_retrain_supervised.py" >> "C:\Projects\RealtimeMonitor\logs\retrain.out.log" 2>> "C:\Projects\RealtimeMonitor\logs\retrain.err.log"
