@echo off
cd /d "%~dp0"
set /p URL=Paste YouTube link and press Enter: 
python shorts.py "%URL%" %*
if exist output start "" output
pause
