@echo off
rem Run the first cut right now and show the queue
cd /d "%~dp0"
python auto.py prepare %*
python auto.py status
pause
