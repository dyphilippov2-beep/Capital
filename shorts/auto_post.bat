@echo off
rem Publish the next clip from the queue (run by Task Scheduler)
cd /d "%~dp0"
if not exist output mkdir output
echo ===== %date% %time% post >> output\auto.log
python auto.py post %* >> output\auto.log 2>&1
