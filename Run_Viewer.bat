@echo off
title Stock V3 Viewer (Streamlit)
chcp 65001 >nul
cd /d C:\Projects\RealtimeMonitor
viewer_env\python.exe -m streamlit run viewer_v3_web.py --server.headless true --browser.gatherUsageStats false
pause
