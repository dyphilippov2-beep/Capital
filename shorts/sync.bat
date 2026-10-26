@echo off
rem Runs on the home PC: YouTube blocks GitHub servers from downloading videos.
rem 1) transcribes new popular videos so Claude can pick meaningful moments,
rem 2) renders the picked moments into shorts\ready,
rem 3) pushes everything to GitHub, where the schedule publishes the clips.
cd /d "%~dp0.."
set AUTO_STATE=shorts\state\auto_state.json
set WHISPER_MODEL=medium
git pull --rebase -q
set N=%1
if "%N%"=="" set N=2
python shorts\auto.py transcribe --videos %N%
python shorts\auto.py render
git add -A shorts\state shorts\ready
git commit -q -m "Shorts: transcripts and rendered clips from PC"
git push -q
python shorts\auto.py status
pause
