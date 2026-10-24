@echo off
rem Creates Windows Task Scheduler jobs. Change the times below and run again to update them.
cd /d "%~dp0"
set PREPARE_DAY=SUN
set PREPARE_TIME=03:00
set POST_MORNING=10:00
set POST_EVENING=19:00

schtasks /create /f /tn "TitanShorts\Prepare" /sc weekly /d %PREPARE_DAY% /st %PREPARE_TIME% /tr "\"%~dp0auto_prepare.bat\""
schtasks /create /f /tn "TitanShorts\Post morning" /sc daily /st %POST_MORNING% /tr "\"%~dp0auto_post.bat\""
schtasks /create /f /tn "TitanShorts\Post evening" /sc daily /st %POST_EVENING% /tr "\"%~dp0auto_post.bat\""
echo.
echo Done. Cutting: every %PREPARE_DAY% at %PREPARE_TIME%. Posting: every day at %POST_MORNING% and %POST_EVENING%.
echo Log: output\auto.log
echo To view or delete the jobs: Start - Task Scheduler - folder TitanShorts.
pause
