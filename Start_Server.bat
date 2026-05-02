@echo off
title Bambu2OBS
cd /d "%~dp0"
echo ============================================
echo  Bambu2OBS
echo ============================================
echo  Starting Bambu2OBS (MQTT writer + Flask server)...
echo  Press Ctrl+C to stop.
echo ============================================
echo.
python src\bambu2obs.py
echo.
echo Server stopped. Press any key to close.
pause >nul
