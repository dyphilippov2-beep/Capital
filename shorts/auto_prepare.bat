@echo off
rem Cut the most popular not-yet-processed channel videos into the queue (run by Task Scheduler)
cd /d "%~dp0"
if not exist output mkdir output
echo ===== %date% %time% prepare >> output\auto.log
python auto.py prepare %* >> output\auto.log 2>&1
