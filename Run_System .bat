@echo off
title RealtimeMonitor - Auto Scheduler
chcp 65001 >nul

set PYTHON_EXE=C:\Users\JH_Signature\miniconda3\envs\trading_env\python.exe
set PROJECT_DIR=C:\Projects\RealtimeMonitor

echo.
echo  ============================================
echo   RealtimeMonitor Auto Scheduler
echo   08:00 NXT / 09:00 KRX / 20:00 shutdown
echo  ============================================
echo.

cd /d %PROJECT_DIR%
"%PYTHON_EXE%" scheduler.py

echo.
echo Scheduler stopped.
pause